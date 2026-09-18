"""Postgres full-text search. One function per query, SQL inside the function.

Every statement here runs as-is in psql:

    psql postgresql://postgres:postgres@localhost:55433/search

Run this file on its own to index a small corpus and see all eleven queries:

    uv run python -m engines.postgres
"""

from __future__ import annotations

import time
from typing import Iterable

import psycopg2
import psycopg2.extras

from core.common import PG_DSN, Doc, Hit, IndexStats

class PostgresEngine:
    name = "postgres"

    # ---------------------------------------------------------------- queries

    def q1_term(self):
        # `@@` is the match operator and the only thing the GIN index serves.
        # ORDER BY ts_rank runs afterwards, on every row that matched.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, plainto_tsquery('english', 'retrieval') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def q1_term_count(self) -> int:
        # The same match with no row fetched: matching cost alone.
        return self.count("""
            SELECT count(*)
            FROM docs, plainto_tsquery('english', 'retrieval') AS q
            WHERE tsv @@ q
        """)

    def q2_phrase(self):
        # phraseto_tsquery builds 'attention <-> mechanism' (FOLLOWED BY),
        # which needs the positions to_tsvector already stores.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, phraseto_tsquery('english', 'attention mechanism') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def q3_boolean(self):
        # & is AND, | is OR, ! is NOT. to_tsquery takes operators;
        # plainto_tsquery would escape them into literal terms.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, to_tsquery('english', 'retrieval & (dense | sparse) & !image') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def q4_prefix(self):
        # ':*' matches the term dictionary by prefix -- the only autocomplete
        # mechanism here. No n-grams, no suggester.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, to_tsquery('english', 'quant:*') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def q5_fuzzy(self):
        # Trigrams, not lexemes: tsvector cannot do edit distance at all,
        # because a misspelling is simply not a lexeme in the dictionary.
        # '<%' is word similarity -- does 'transfomer' closely match some word
        # inside the title? Plain '%' compares against the whole title string,
        # and one word is never 30% similar to a ten-word title.
        return self.search("""
            SELECT id, word_similarity('transfomer', title) AS score, title, '' AS hl
            FROM docs
            WHERE 'transfomer' <% title
            ORDER BY score DESC
            LIMIT 10
        """)

    def q6_filter_text(self):
        # One WHERE clause for both halves. Postgres has no filter context and
        # no filter cache -- the planner just picks which index to lead with.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, plainto_tsquery('english', 'graph neural') AS q
            WHERE tsv @@ q
              AND update_date >= DATE '2023-01-01'
            ORDER BY score DESC
            LIMIT 10
        """)

    def q7_facet(self, top: int = 10) -> list[tuple[str, int]]:
        # Not faceting: a runtime unnest + GROUP BY over every matching row.
        # Correct, and the cost grows with the match count, where Lucene reads
        # a precomputed doc-values column and stays flat.
        rows = self._rows("""
            SELECT f.v AS value, count(*) AS n
            FROM docs, plainto_tsquery('english', 'retrieval') AS q, unnest(categories) AS f(v)
            WHERE tsv @@ q
            GROUP BY f.v
            ORDER BY n DESC
            LIMIT 10
        """)
        return [(r[0], int(r[1])) for r in rows][:top]

    def q8_sort_date(self):
        # ORDER BY a btree column instead of by score: nothing is ranked.
        return self.search("""
            SELECT id, 0.0 AS score, title, '' AS hl
            FROM docs, plainto_tsquery('english', 'retrieval') AS q
            WHERE tsv @@ q
            ORDER BY update_date DESC
            LIMIT 10
        """)

    def q9_deep_page(self):
        # OFFSET 4990 ranks every matching row and throws 4,990 away. There is
        # no search_after here; a keyset predicate is the closest equivalent
        # and needs a stable unique sort key.
        return self.search(f"""
            SELECT id, ts_rank(tsv, q) AS score, title, '' AS hl
            FROM docs, plainto_tsquery('english', 'learning') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10 OFFSET 4990
        """)

    def q10_highlight(self):
        # ts_headline re-analyzes the original abstract at query time, per
        # returned row, with no index involved. That is why it dominates
        # latency here.
        return self.search(f"""
            SELECT id,
                   ts_rank(tsv, q) AS score,
                   title,
                   ts_headline('english', abstract, q, 'MaxWords=25,MinWords=10') AS hl
            FROM docs, plainto_tsquery('english', 'knowledge distillation') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def q11_boosted(self):
        # title^5 / abstract^1 as a weight array: B (abstract) 0.2 against
        # A (title) 1.0 is the 5x. The labels are baked in at index time, the
        # numbers are chosen here, so a boost change needs no reindex.
        return self.search("""
            SELECT id, ts_rank('{0.1, 0.2, 0.2, 1.0}', tsv, q) AS score, title, '' AS hl
            FROM docs, plainto_tsquery('english', 'language model') AS q
            WHERE tsv @@ q
            ORDER BY score DESC
            LIMIT 10
        """)

    def all_queries(self):
        return [
            ("1-term", self.q1_term),
            ("2-phrase", self.q2_phrase),
            ("3-boolean", self.q3_boolean),
            ("4-prefix", self.q4_prefix),
            ("5-fuzzy", self.q5_fuzzy),
            ("6-filter-text", self.q6_filter_text),
            ("8-sort-date", self.q8_sort_date),
            ("9-deep-page", self.q9_deep_page),
            ("10-highlight", self.q10_highlight),
            ("11-boosted", self.q11_boosted),
        ]

    # ------------------------------------------------------ what is on disk

    def stored(self, doc_id: str) -> str:
        """One row, both of its representations.

        `title` / `abstract` hold the original text, untouched. `tsv` is a
        GENERATED column (see SCHEMA at the bottom) that Postgres rebuilds on
        every write. Queries never look at the text columns -- `@@` matches
        against `tsv` alone, and the text is only read back to display.
        """
        rows = self._rows(
            "SELECT title, abstract, categories, update_date, tsv FROM docs WHERE id = %s",
            (doc_id,),
        )
        if not rows:
            return f"no such doc: {doc_id}"
        title, abstract, cats, date, tsv = rows[0]
        return (
            f"title       {title}\n"
            f"abstract    {abstract[:96]}...\n"
            f"categories  {cats}          <- text[], a real array type\n"
            f"update_date {date}          <- date, not a string\n"
            f"tsv         {tsv[:220]}..."
        )

    # ------------------------------------------------------------- the runner

    def search(self, sql: str, params=None) -> list[Hit]:
        rows = self._rows(sql, params)
        return [Hit(id=r[0], score=float(r[1]), title=r[2], highlight=r[3]) for r in rows]

    def count(self, sql: str, params=None) -> int:
        return int(self._rows(sql, params)[0][0])

    def _rows(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def __init__(self, dsn: str = PG_DSN):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

            # Load first, index after: building GIN once beats maintaining it per row.
            t0 = time.perf_counter()
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO docs (id, title, abstract, authors, categories, "
                "update_date, version_count, doi) VALUES %s",
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
            cur.execute(INDEXES)
            cur.execute("ANALYZE docs")
            index_s = time.perf_counter() - t0

            cur.execute("SELECT pg_indexes_size('docs'), pg_total_relation_size('docs')")
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

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = 384) -> float:
        t0 = time.perf_counter()
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE EXTENSION IF NOT EXISTS vector;
                ALTER TABLE docs DROP COLUMN IF EXISTS embedding;
                ALTER TABLE docs ADD COLUMN embedding vector(384);
            """)
            psycopg2.extras.execute_values(
                cur,
                "UPDATE docs SET embedding = v.emb::vector "
                "FROM (VALUES %s) AS v(id, emb) WHERE docs.id = v.id",
                [(k, str(list(map(float, vec)))) for k, vec in vectors.items()],
                page_size=500,
            )
            cur.execute("CREATE INDEX docs_emb_hnsw ON docs USING hnsw (embedding vector_cosine_ops)")
        return time.perf_counter() - t0

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        # '<=>' is cosine distance, so 1 - distance is a similarity.
        return self.search(
            """
            SELECT id, 1 - (embedding <=> %(vec)s::vector) AS score, title, '' AS hl
            FROM docs
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %(vec)s::vector
            LIMIT 10
            """,
            {"vec": str(list(map(float, vector)))},
        )[:k]

    def close(self) -> None:
        self.conn.close()


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DROP TABLE IF EXISTS docs;
CREATE TABLE docs (
    id            text PRIMARY KEY,
    title         text NOT NULL,
    abstract      text NOT NULL,
    authors       text[] NOT NULL DEFAULT '{}',
    categories    text[] NOT NULL DEFAULT '{}',
    update_date   date NOT NULL,
    version_count int NOT NULL,
    doi           text NOT NULL DEFAULT '',
    -- setweight labels lexemes A (title) and B (abstract). What those labels
    -- are worth comes from ts_rank at query time.
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(abstract, '')), 'B')
    ) STORED
);
"""

INDEXES = """
-- GIN over the tsvector: slower to build, faster to search, exact.
-- GiST would be lossy and recheck every candidate.
CREATE INDEX docs_tsv_gin ON docs USING GIN (tsv);
-- A separate trigram index, for q5_fuzzy.
CREATE INDEX docs_title_trgm ON docs USING GIN (title gin_trgm_ops);
CREATE INDEX docs_date ON docs (update_date DESC);
CREATE INDEX docs_cats ON docs USING GIN (categories);
"""


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(PostgresEngine())
