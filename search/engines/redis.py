"""Redis query engine. One function per query, the command inside the function.

Every command here runs as-is in redis-cli:

    docker exec -it search-redis redis-cli

Syntax that catches everyone:
    space   AND   -- yes, even inside @field:(...)
    |       OR
    -term   NOT
    @f:{v}  TAG match, braces not parens
    %term%  fuzzy, one % per edit-distance step
    DIALECT 2 is pinned everywhere: parsing semantics change between versions.

The query argument is single-quoted for the same reason redis-cli needs it
quoted: '{$weight: 5.0}' contains a space and would otherwise arrive as two
arguments.

    uv run python -m engines.redis
"""

from __future__ import annotations

import shlex
import time
from datetime import date
from typing import Iterable

import numpy as np
import redis

from core.common import REDIS_HOST, REDIS_PORT, Doc, Hit, IndexStats

PREFIX = "doc:"
EPOCH = date(1970, 1, 1)


class RedisEngine:
    name = "redis"

    # ---------------------------------------------------------------- queries

    def q1_term(self):
        # SCORER is explicit everywhere in this file. Redis defaults to TFIDF;
        # BM25STD is the standard BM25 every other engine here uses. Same index,
        # same postings -- only the ranking function changes.
        return self.search(
            "FT.SEARCH idx:docs 'retrieval' SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q1_term_count(self) -> int:
        # LIMIT 0 0 returns the total with no document materialized.
        return self.count("FT.SEARCH idx:docs 'retrieval' LIMIT 0 0 DIALECT 2")

    def q2_phrase(self):
        # Quoted = exact phrase. Unquoted these are two ANDed terms and the
        # positions are never consulted.
        return self.search(
            'FT.SEARCH idx:docs \'"attention mechanism"\' '
            "SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q3_boolean(self):
        return self.search(
            "FT.SEARCH idx:docs 'retrieval (dense|sparse) -image' "
            "SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q4_prefix(self):
        return self.search(
            "FT.SEARCH idx:docs 'quant*' SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q5_fuzzy(self):
        # Two %, not one. Redis matches the fuzzy term against the *stemmed*
        # dictionary without stemming the query term: the index holds
        # 'transform', 3 edits from 'transfomer'. %transfomer% matches nothing.
        return self.search(
            "FT.SEARCH idx:docs '%%transfomer%%' SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q6_filter_text(self):
        # Numeric filter and text query in one expression. update_date is epoch
        # days because Redis has no date type, only NUMERIC; 19358 = 2023-01-01.
        # The terms are OR'd: ANDed, they matched 11 documents against every
        # other engine's 1,430.
        return self.search(
            "FT.SEARCH idx:docs '@update_date:[19358 +inf] (graph|neural)' "
            "SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q7_facet(self, top: int = 10) -> list[tuple[str, int]]:
        # FT.AGGREGATE is a different pipeline from FT.SEARCH: it loads field
        # values and runs GROUPBY/REDUCE stages.
        #
        # The catch: on a HASH this groups by the whole stored TAG string, so
        # 'cs.IR|cs.LG' is ONE bucket, not two. The split and re-sum below is
        # client-side work Lucene engines do not need -- they read a per-value
        # doc-values column.
        reply = self.run(
            "FT.AGGREGATE idx:docs 'retrieval' GROUPBY 1 @categories "
            "REDUCE COUNT 0 AS n SORTBY 2 @n DESC LIMIT 0 10000 DIALECT 2"
        )
        rows = (
            [row.get("extra_attributes", {}) for row in reply["results"]]
            if isinstance(reply, dict)  # RESP3
            else [dict(zip(r[::2], r[1::2])) for r in reply[1:]]
        )
        counts: dict[str, int] = {}
        for kv in rows:
            for value in filter(None, kv.get("categories", "").split("|")):
                counts[value] = counts.get(value, 0) + int(kv.get("n", 0))
        return sorted(counts.items(), key=lambda kv: -kv[1])[:top]

    def q8_sort_date(self):
        # SORTBY reads the SORTABLE column store, not the postings.
        return self.search(
            "FT.SEARCH idx:docs 'retrieval' SORTBY update_date DESC WITHSCORES LIMIT 0 10 DIALECT 2"
        )

    def q9_deep_page(self):
        # MAXSEARCHRESULTS (default 10,000) is the hard ceiling on this.
        return self.search(
            "FT.SEARCH idx:docs 'learning' SCORER BM25STD WITHSCORES LIMIT 4990 10 DIALECT 2"
        )

    def q10_highlight(self):
        # SUMMARIZE picks fragments, HIGHLIGHT wraps the matches. LEN is in
        # words either side of the match, not characters.
        return self.search(
            'FT.SEARCH idx:docs \'"knowledge distillation"\' SCORER BM25STD WITHSCORES '
            "SUMMARIZE FIELDS 1 abstract FRAGS 1 LEN 25 HIGHLIGHT FIELDS 1 abstract "
            "LIMIT 0 10 DIALECT 2"
        )

    def q11_boosted(self):
        # Query-time field weighting, DIALECT 2 attribute syntax. Note the |
        # inside each field clause: a space there means AND, and
        # '@title:(language model)' matched 6 documents where every other
        # engine matched 74,902.
        return self.search(
            "FT.SEARCH idx:docs '((@title:(language|model))=>{$weight: 5.0} | "
            "(@abstract:(language|model))=>{$weight: 1.0})' "
            "SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2"
        )

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
        """A Redis hash, and nothing else.

        Redis stores the document as a plain HASH at `doc:<id>`. The index is
        separate: FT.CREATE told Redis to watch that key prefix, and the
        inverted index it maintains is not readable as data -- FT.INFO only
        reports its size. So this shows the stored side; the indexed side is
        visible only through FT.EXPLAIN.

        Note what the schema forced: categories is a single pipe-joined string
        (no array type), and update_date is an integer of epoch days (no date
        type). Both conversions happen in index(), below.
        """
        fields = self.r.hgetall(PREFIX + doc_id)
        if not fields:
            return f"no such doc: {doc_id}"
        out = [f"HGETALL {PREFIX}{doc_id}"]
        for k, v in fields.items():
            if k == "vec":
                v = f"<{len(v)} bytes of float32>"
            out.append(f"  {k:<12} {str(v)[:96]}")
        out.append(f"\n  FT.EXPLAIN -> {self.r.execute_command('FT.EXPLAIN', 'idx:docs', 'retrieval').strip()}")
        return "\n".join(out)

    # ------------------------------------------------------------- the runner

    def run(self, command: str):
        """Split like a shell would, send like redis-cli would."""
        return self.r.execute_command(*shlex.split(command))

    def search(self, command: str) -> list[Hit]:
        return self._hits(self.run(command))

    def count(self, command: str) -> int:
        reply = self.run(command)
        return int(reply["total_results"] if isinstance(reply, dict) else reply[0])

    @staticmethod
    def _hits(reply) -> list[Hit]:
        """FT.SEARCH replies in two shapes depending on the protocol.

        RESP3 (Redis 8 + redis-py 6, what runs here) returns a dict:
            {"total_results": N, "results": [{"id":..., "score":...,
                                              "extra_attributes": {...}}, ...]}
        RESP2 returns a flat list: [total, key, [f, v, ...], key, ...] with the
        score spliced in after each key when WITHSCORES is set.
        """
        if isinstance(reply, dict):  # RESP3
            out = []
            for row in reply.get("results", []):
                fields = row.get("extra_attributes") or {}
                key = row.get("id", "")
                out.append(
                    Hit(
                        id=key[len(PREFIX):] if key.startswith(PREFIX) else key,
                        score=float(row.get("score", 0.0) or 0.0),
                        title=fields.get("title", ""),
                        highlight=fields.get("abstract", ""),
                    )
                )
            return out

        hits = []  # RESP2
        i = 1
        while i < len(reply):
            key = reply[i]
            score, i = (float(reply[i + 1]), i + 2) if _is_number(reply[i + 1]) else (0.0, i + 1)
            fields = dict(zip(reply[i][::2], reply[i][1::2])) if i < len(reply) else {}
            hits.append(
                Hit(
                    id=key[len(PREFIX):] if key.startswith(PREFIX) else key,
                    score=score,
                    title=fields.get("title", ""),
                    highlight=fields.get("abstract", ""),
                )
            )
            i += 1
        return hits

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)
        self.host, self.port = host, port

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        self.r.flushdb()
        self.run(SCHEMA_WITH_VECTORS if with_vectors else SCHEMA)

        t0 = time.perf_counter()
        pipe = self.r.pipeline(transaction=False)
        for i, d in enumerate(docs, 1):
            pipe.hset(
                PREFIX + d.id,
                mapping={
                    "id": d.id,
                    "title": d.title,
                    "abstract": d.abstract,
                    "authors": "|".join(d.authors),
                    "categories": "|".join(d.categories),
                    "update_date": epoch_days(d.update_date),
                    "version_count": d.version_count,
                    "doi": d.doi,
                },
            )
            if i % 2000 == 0:
                pipe.execute()
        pipe.execute()

        while True:  # the last batch is still folding in when the pipeline returns
            info = self.r.ft("idx:docs").info()
            if float(info.get("percent_indexed", 1)) >= 1:
                break
            time.sleep(0.05)
        build_s = time.perf_counter() - t0

        info = self.r.ft("idx:docs").info()
        inverted_mb = float(info.get("inverted_sz_mb", 0))
        total_mb = float(info.get("vector_index_sz_mb", 0)) + inverted_mb
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=int(total_mb * 1024 * 1024),
            notes=f"in RAM; inverted {inverted_mb:.1f} MB, records {info.get('num_records')}",
        )

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = 384) -> float:
        t0 = time.perf_counter()
        pipe = self.r.pipeline(transaction=False)
        for i, (doc_id, vec) in enumerate(vectors.items(), 1):
            pipe.hset(PREFIX + doc_id, "vec", np.asarray(vec, dtype=np.float32).tobytes())
            if i % 500 == 0:
                pipe.execute()
        pipe.execute()
        return time.perf_counter() - t0

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        # Prefiltered KNN in one command: the '*' can be any filter expression,
        # and it is applied BEFORE the vector search runs over the survivors.
        # Every vector database has to solve filter-then-search; Redis makes it
        # syntax. $blob goes through PARAMS as raw bytes, so this needs a
        # bytes-mode client -- decode_responses would mangle the float32s.
        raw = redis.Redis(host=self.host, port=self.port)
        command = (
            "FT.SEARCH idx:docs '*=>[KNN 10 @vec $blob AS dist]' PARAMS 2 blob %BLOB% "
            "SORTBY dist RETURN 3 id title dist LIMIT 0 10 DIALECT 2"
        )
        parts = [
            np.asarray(vector, dtype=np.float32).tobytes() if p == "%BLOB%" else p
            for p in shlex.split(command)
        ]
        reply = raw.execute_command(*parts)
        hits = []
        for i in range(1, len(reply), 2):
            key = reply[i].decode()
            fields = {k.decode(): v.decode() for k, v in zip(reply[i + 1][::2], reply[i + 1][1::2])}
            hits.append(
                Hit(
                    id=key[len(PREFIX):] if key.startswith(PREFIX) else key,
                    score=1.0 - float(fields.get("dist", 1.0)),
                    title=fields.get("title", ""),
                )
            )
        raw.close()
        return hits[:k]

    def close(self) -> None:
        self.r.close()


# The index is a VIEW over hashes that already exist under the prefix doc:.
# You keep writing plain hashes; the index updates inside the same command.
# doi is deliberately absent: stored in the hash, never indexed.
SCHEMA = (
    "FT.CREATE idx:docs ON HASH PREFIX 1 doc: SCHEMA "
    "title TEXT WEIGHT 1.0 "
    "abstract TEXT WEIGHT 1.0 "
    "authors TAG SEPARATOR | "
    "categories TAG SEPARATOR | "
    "update_date NUMERIC SORTABLE "
    "version_count NUMERIC SORTABLE"
)

SCHEMA_WITH_VECTORS = SCHEMA + " vec VECTOR HNSW 6 TYPE FLOAT32 DIM 384 DISTANCE_METRIC COSINE"


def epoch_days(iso: str) -> int:
    """Redis has no date type, only NUMERIC. 2023-01-01 is 19358."""
    try:
        return (date.fromisoformat(iso) - EPOCH).days
    except ValueError:
        return 0


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(RedisEngine())
