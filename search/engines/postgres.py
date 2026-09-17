"""Postgres FTS: tsvector + GIN, pg_trgm for fuzzy, pgvector for phase 2.

A genuinely separate implementation, not Lucene with a SQL face on it:

  tsvector        the document, normalized into *lexemes* with positions and a
                  weight label A-D. `to_tsvector('english', ...)` stems.
  tsquery         the query, in the same lexeme space. plainto_tsquery ANDs the
                  terms, phraseto_tsquery builds a <-> (FOLLOWED BY) query,
                  websearch_to_tsquery also understands quotes and a leading -.
  GIN vs GiST     GIN is slower to build and update, faster to search, exact.
                  GiST is lossy and rechecks. GIN is the read-heavy default.
  ts_rank         frequency-based, and that is the whole story: **no IDF term**,
                  so a rare word and a common word count the same, and no
                  length norm, so long abstracts are not penalized. ts_rank_cd
                  additionally rewards matched terms appearing close together.
                  Measured: 0.36 nDCG@10 against BM25's 0.67.
  pg_trgm         a *separate* index over character trigrams. tsvector cannot
                  do edit distance at any query syntax, because a misspelling
                  is simply not a lexeme in the dictionary.
  no faceting     facet() below is a runtime unnest + GROUP BY over every
                  matching row -- correct, and linear in the match count where
                  Lucene walks a precomputed doc-values column.
"""

from __future__ import annotations

import re
import time
from typing import Iterable

import psycopg2
import psycopg2.extras

from core.common import EMBED_DIMS, PG_DSN, Doc, Hit, IndexStats, Query, Unsupported

TABLE = "docs"

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DROP TABLE IF EXISTS {TABLE};
CREATE TABLE {TABLE} (
    id            text PRIMARY KEY,
    title         text NOT NULL,
    abstract      text NOT NULL,
    authors       text[] NOT NULL DEFAULT '{{}}',
    categories    text[] NOT NULL DEFAULT '{{}}',
    update_date   date NOT NULL,
    version_count int NOT NULL,
    doi           text NOT NULL DEFAULT '',
    -- setweight labels A/B; the numeric meaning comes from ts_rank at query
    -- time, so field boosts change without a reindex (unlike Lucene norms).
    -- GENERATED STORED rather than an expression index: materialized once at
    -- write time and readable back.
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(abstract, '')), 'B')
    ) STORED
);
"""

INDEXES = [
    # GIN not GiST: slower to build, faster to search, right for read-heavy.
    f"CREATE INDEX {TABLE}_tsv_gin ON {TABLE} USING GIN (tsv)",
    # Separate index for fuzzy: tsvector knows nothing about edit distance.
    f"CREATE INDEX {TABLE}_title_trgm ON {TABLE} USING GIN (title gin_trgm_ops)",
    f"CREATE INDEX {TABLE}_date ON {TABLE} (update_date DESC)",
    f"CREATE INDEX {TABLE}_cats ON {TABLE} USING GIN (categories)",
]

DEFAULT_WEIGHTS = "{0.1, 0.2, 0.4, 1.0}"  # ts_rank weight array is {D, C, B, A}


class PostgresEngine:
    name = "postgres"

    def __init__(self, dsn: str = PG_DSN):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        # with_vectors is accepted for symmetry; the column is added later.
        docs = list(docs)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

            # Load first, index after: building GIN once beats maintaining it per row.
            t0 = time.perf_counter()
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {TABLE} "
                "(id, title, abstract, authors, categories, update_date, version_count, doi) "
                "VALUES %s",
                [
                    (
                        d.id, d.title, d.abstract, list(d.authors), list(d.categories),
                        d.update_date, d.version_count, d.doi,
                    )
                    for d in docs
                ],
                page_size=1000,
            )
            insert_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            for stmt in INDEXES:
                cur.execute(stmt)
            cur.execute(f"ANALYZE {TABLE}")
            index_s = time.perf_counter() - t0

            cur.execute(f"SELECT pg_indexes_size('{TABLE}'), pg_total_relation_size('{TABLE}')")
            idx_bytes, total_bytes = cur.fetchone()

        return IndexStats(
            docs=len(docs),
            build_s=insert_s + index_s,
            size_bytes=idx_bytes,
            notes=(
                f"insert {insert_s:.1f}s + index {index_s:.1f}s; "
                f"indexes {idx_bytes / 1e6:.0f} MB of {total_bytes / 1e6:.0f} MB total"
            ),
        )

    def _tsquery(self, q: Query) -> tuple[str, list]:
        if q.kind == "phrase":
            return "phraseto_tsquery('english', %s)", [q.text]  # builds a <-> query
        if q.kind == "boolean":
            parts = [*q.must]
            if q.should:
                parts.append("(" + " | ".join(q.should) + ")")
            parts += [f"!{t}" for t in q.must_not]
            return "to_tsquery('english', %s)", [" & ".join(parts)]
        if q.kind == "prefix":
            return "to_tsquery('english', %s)", [f"{q.text}:*"]
        # plainto_tsquery ANDs every term, so a sentence-long query matches
        # nothing. Every other engine's default is OR plus a ranker.
        terms = [t for t in re.split(r"\W+", q.text.lower()) if len(t) > 1]
        if not terms:
            return "plainto_tsquery('english', %s)", [q.text]
        return "to_tsquery('english', %s)", [" | ".join(terms)]

    def _rank(self, q: Query) -> str:
        if q.kind == "boosted":
            weights = dict(q.boosts)
            a, b = weights.get("title", 1.0), weights.get("abstract", 1.0)
            total = max(a, b) or 1.0
            return f"ts_rank('{{0.1, 0.2, {b / total:.3f}, {a / total:.3f}}}', tsv, q)"
        # ts_rank_cd rewards matched terms appearing close together. Neither
        # ts_rank nor ts_rank_cd has an IDF term.
        return f"ts_rank_cd('{DEFAULT_WEIGHTS}', tsv, q)"

    def _sql(self, q: Query) -> tuple[str, list]:
        if q.kind == "fuzzy":
            # `<%` is word similarity. Plain `%` compares the query to the whole
            # title, and one word is never 30% similar to a ten-word title.
            return (
                f"SELECT id, word_similarity(%s, title) AS score, title, '' AS hl "
                f"FROM {TABLE} WHERE %s <%% title "
                "ORDER BY score DESC LIMIT %s OFFSET %s",
                [q.text, q.text, q.limit, q.offset],
            )

        frag, params = self._tsquery(q)
        where = ["tsv @@ q"]
        extra: list = []
        if q.date_from:
            where.append("update_date >= %s")
            extra.append(q.date_from)

        # ts_headline re-analyzes the original text at query time; it cannot
        # use the index, which is why highlighting dominates latency.
        select_hl = (
            "ts_headline('english', abstract, q, 'MaxWords=25,MinWords=10')"
            if q.kind == "highlight"
            else "''"
        )
        order = f"{q.sort_field} DESC" if q.sort_field else f"{self._rank(q)} DESC"
        sql = (
            f"WITH q AS (SELECT {frag} AS q) "
            f"SELECT id, {self._rank(q)} AS score, title, {select_hl} AS hl "
            f"FROM {TABLE}, q WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT %s OFFSET %s"
        )
        return sql, [*params, *extra, q.limit, q.offset]

    def search(self, q: Query) -> list[Hit]:
        sql, params = self._sql(q)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [Hit(id=r[0], score=float(r[1]), title=r[2], highlight=r[3]) for r in cur]

    def count(self, q: Query) -> int:
        """Matching cost without fetching cost."""
        if q.kind == "fuzzy":
            sql = f"SELECT count(*) FROM {TABLE} WHERE %s <%% title"
            params: list = [q.text]
        else:
            frag, params = self._tsquery(q)
            where = ["tsv @@ q"]
            if q.date_from:
                where.append("update_date >= %s")
                params = [*params, q.date_from]
            sql = (
                f"WITH q AS (SELECT {frag} AS q) "
                f"SELECT count(*) FROM {TABLE}, q WHERE {' AND '.join(where)}"
            )
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()[0])

    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]:
        """Runtime aggregation, not faceting: no precomputed structure, cost
        grows with the match count rather than with `top`."""
        if q.facet_field not in ("categories", "authors"):
            raise Unsupported(f"facet field {q.facet_field}")
        frag, params = self._tsquery(q)
        sql = (
            f"WITH q AS (SELECT {frag} AS q) "
            f"SELECT f.v, count(*) FROM {TABLE}, q, unnest({q.facet_field}) AS f(v) "
            "WHERE tsv @@ q GROUP BY f.v ORDER BY count(*) DESC LIMIT %s"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, [*params, top])
            return [(r[0], int(r[1])) for r in cur]

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = EMBED_DIMS) -> float:
        t0 = time.perf_counter()
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS embedding")
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN embedding vector({dims})")
            psycopg2.extras.execute_values(
                cur,
                f"UPDATE {TABLE} SET embedding = v.emb::vector "
                "FROM (VALUES %s) AS v(id, emb) WHERE docs.id = v.id",
                [(k, str(list(map(float, vec)))) for k, vec in vectors.items()],
                page_size=500,
            )
            cur.execute(
                f"CREATE INDEX {TABLE}_emb_hnsw ON {TABLE} "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        return time.perf_counter() - t0

    def vector_search(self, vector: list[float], k: int = 10) -> list[Hit]:
        with self.conn.cursor() as cur:  # <=> is cosine distance
            cur.execute(
                f"SELECT id, 1 - (embedding <=> %s::vector) AS score, title "
                f"FROM {TABLE} WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                [str(list(map(float, vector)))] * 2 + [k],
            )
            return [Hit(id=r[0], score=float(r[1]), title=r[2]) for r in cur]

    def close(self) -> None:
        self.conn.close()
