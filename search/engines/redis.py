"""Redis query engine: its own in-memory inverted index, no Lucene anywhere.

The mental model to get right: `FT.CREATE` does not create a container you
write documents into. It creates a **view over documents that already exist**
under a key prefix. You keep writing plain hashes; the index updates
synchronously, inside the same command.

    FT.CREATE idx ON HASH PREFIX 1 doc: SCHEMA title TEXT WEIGHT 5.0 ...

Field types map onto the query suite directly:

    TEXT     @title:(rust database)        stemmed and analyzed; WEIGHT is an
                                           index-time field boost
    TAG      @categories:{cs.IR}           exact, unanalyzed -- Redis' `keyword`.
                                           Braces, not parens
    NUMERIC  @version_count:[2 +inf]       range filter; SORTABLE to sort on it
    GEO      @loc:[77.59 12.97 5 km]       the geo POC's other half
    VECTOR   *=>[KNN 10 @vec $blob]        HNSW or FLAT, same index as the text

Three things worth knowing before using it in anger:

  * **Scoring is a choice, and the default is the wrong one.** Same index, same
    postings, one flag apart on SciFact: TFIDF 0.076 (the default), BM25 0.072
    (Redis' legacy variant), BM25STD 0.655 (standard BM25, what Lucene
    implements), DISMAX 0.062 nDCG@10. Use BM25STD.
  * **DIALECT changes parsing semantics** between versions. Everything here
    pins DIALECT 2; relying on the server default is a real footgun when the
    server is upgraded under you.
  * **The index is RAM**, rebuilt from RDB/AOF on restart, and MAXSEARCHRESULTS
    (default 10k) caps deep paging. There is no sharding in open-source Redis.

The tradeoff: BM25, faceting, filtering and vector search in one round trip
against data already in memory -- sitting precisely between Postgres FTS and
Elasticsearch.
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Iterable

import numpy as np
import redis
from redis.commands.search.aggregation import AggregateRequest, Desc
from redis.commands.search.field import NumericField, TagField, TextField, VectorField
from redis.commands.search.query import Query as RQuery
import redis.commands.search.reducers as reducers

try:  # redis-py moved this module between versions
    from redis.commands.search.index_definition import IndexDefinition, IndexType
except ImportError:  # pragma: no cover
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType

from core.common import (
    EMBED_DIMS,
    REDIS_HOST,
    REDIS_PORT,
    Doc,
    Hit,
    IndexStats,
    Query,
)

INDEX = "idx:docs"
PREFIX = "doc:"
EPOCH = date(1970, 1, 1)


def _days(iso: str) -> int:
    """Redis has no date type, only NUMERIC."""
    try:
        return (date.fromisoformat(iso) - EPOCH).days
    except ValueError:
        return 0


def terms_of(text: str) -> str:
    """Strip user text to bare terms: '-' is NOT, '|' is OR, '@' opens a field."""
    return " ".join(t for t in re.split(r"\W+", text) if len(t) > 1)


class RedisEngine:
    name = "redis"

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT, scorer: str = "BM25STD"):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)
        self.scorer = scorer  # TFIDF | BM25 | BM25STD | DISMAX | DOCSCORE
        self.name = f"redis[{scorer.lower()}]"

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        self.r.flushdb()

        schema = [
            TextField("title", weight=1.0),  # WEIGHT is an index-time boost
            TextField("abstract", weight=1.0),
            TagField("authors", separator="|"),  # TAG = exact, unanalyzed
            TagField("categories", separator="|"),
            NumericField("update_date", sortable=True),
            NumericField("version_count", sortable=True),
            # doi is absent on purpose: stored in the hash, never indexed.
        ]
        if with_vectors:
            schema.append(
                VectorField(
                    "vec",
                    "HNSW",
                    {"TYPE": "FLOAT32", "DIM": EMBED_DIMS, "DISTANCE_METRIC": "COSINE"},
                )
            )

        self.r.ft(INDEX).create_index(
            schema,
            definition=IndexDefinition(prefix=[PREFIX], index_type=IndexType.HASH),
        )

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
                    "update_date": _days(d.update_date),
                    "version_count": d.version_count,
                    "doi": d.doi,
                },
            )
            if i % 2000 == 0:
                pipe.execute()
        pipe.execute()

        while True:  # the last batch is still folding in when the pipeline returns
            info = self.r.ft(INDEX).info()
            if float(info.get("percent_indexed", 1)) >= 1:
                break
            time.sleep(0.05)
        build_s = time.perf_counter() - t0

        info = self.r.ft(INDEX).info()
        inverted_mb = float(info.get("inverted_sz_mb", 0))
        total_mb = float(info.get("vector_index_sz_mb", 0)) + inverted_mb
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=int(total_mb * 1024 * 1024),
            notes=f"in RAM; inverted {inverted_mb:.1f} MB, records {info.get('num_records')}",
        )

    def _query_string(self, q: Query) -> str:
        text = terms_of(q.text)
        if q.kind == "phrase":
            return f'"{text}"'  # quoted = exact phrase, needs DIALECT 2
        if q.kind == "boolean":
            parts = list(q.must)  # space = AND, | = OR, - = NOT
            if q.should:
                parts.append("(" + "|".join(q.should) + ")")
            parts += [f"-{t}" for t in q.must_not]
            return " ".join(parts)
        if q.kind == "prefix":
            return f"{text}*"
        if q.kind == "fuzzy":
            # One % per edit step. Two, not one: Redis matches against the
            # stemmed dictionary without stemming the query term, so the index
            # holds 'transform', 3 edits from 'transfomer'. On SciFact
            # %transfomer% matches 0 docs and %%transfomer%% matches 138.
            return f"%%{text}%%"
        if q.kind == "filtered":
            # The text terms need '|' here too, for the same reason as below:
            # ANDed, 'graph neural' matched 11 docs against everyone else's 1,430.
            return f"@update_date:[{_days(q.date_from)} +inf] ({'|'.join(text.split())})"
        if q.kind == "boosted":
            # '|' inside the field clause too: a space there means AND, and
            # '@title:(language model)' matched 6 docs vs everyone else's 1,014.
            or_text = "|".join(text.split())
            clauses = [f"(@{fld}:({or_text}))=>{{$weight: {w}}}" for fld, w in q.boosts]
            return "(" + " | ".join(clauses) + ")"
        # Space means AND, so a sentence-long query matches nothing.
        return "(" + "|".join(text.split()) + ")" if " " in text else text

    def _build(self, q: Query) -> RQuery:
        rq = RQuery(self._query_string(q)).paging(q.offset, q.limit).dialect(2)
        rq = rq.scorer(self.scorer).with_scores()
        if q.sort_field:
            # SORTABLE numerics live in a column store, not the postings.
            rq = rq.sort_by("update_date", asc=False)
        if q.kind == "highlight":
            # context_len is in words either side of the match.
            rq = rq.summarize(fields=["abstract"], context_len=25, num_frags=1).highlight(
                fields=["abstract"]
            )
        return rq

    @staticmethod
    def _doc_id(doc) -> str:
        """Document.id is the key and shadows the stored `id` field."""
        raw = doc.id
        return raw[len(PREFIX):] if raw.startswith(PREFIX) else raw

    def search(self, q: Query) -> list[Hit]:
        res = self.r.ft(INDEX).search(self._build(q))
        return [
            Hit(
                id=self._doc_id(d),
                score=float(getattr(d, "score", 0.0) or 0.0),
                title=getattr(d, "title", ""),
                highlight=getattr(d, "abstract", "") if q.kind == "highlight" else "",
            )
            for d in res.docs
        ]

    def count(self, q: Query) -> int:
        """LIMIT 0 0: a total with no document materialized."""
        rq = RQuery(self._query_string(q)).paging(0, 0).dialect(2)
        return int(self.r.ft(INDEX).search(rq).total)

    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]:
        """FT.AGGREGATE is a separate pipeline (GROUPBY/REDUCE over loaded values).

        GROUPBY over a multi-valued TAG on a HASH groups by the whole stored
        string -- 'cs.IR|cs.LG' is one bucket -- so combinations are counted
        server-side and expanded here. Lucene engines read per-value doc values.
        """
        req = (
            AggregateRequest(self._query_string(q))
            .group_by([f"@{q.facet_field}"], reducers.count().alias("n"))
            .sort_by(Desc("@n"))
            .limit(0, 10_000)
            .dialect(2)
        )
        res = self.r.ft(INDEX).aggregate(req)
        counts: dict[str, int] = {}
        for row in res.rows:
            kv = {row[i]: row[i + 1] for i in range(0, len(row) - 1, 2)}
            combo, n = kv.get(q.facet_field, ""), int(kv.get("n", 0))
            for value in filter(None, combo.split("|")):
                counts[value] = counts.get(value, 0) + n
        return sorted(counts.items(), key=lambda kv: -kv[1])[:top]

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = EMBED_DIMS) -> float:
        """Vectors go into the same hashes as the text."""
        t0 = time.perf_counter()
        pipe = self.r.pipeline(transaction=False)
        for i, (doc_id, vec) in enumerate(vectors.items(), 1):
            pipe.hset(PREFIX + doc_id, "vec", np.asarray(vec, dtype=np.float32).tobytes())
            if i % 500 == 0:
                pipe.execute()
        pipe.execute()
        return time.perf_counter() - t0

    def vector_search(self, vector, k: int = 10, date_from: str | None = None) -> list[Hit]:
        """Prefiltered KNN in one query: filter first, search the survivors."""
        prefilter = f"@update_date:[{_days(date_from)} +inf]" if date_from else "*"
        rq = (
            RQuery(f"{prefilter}=>[KNN {k} @vec $blob AS dist]")
            .sort_by("dist")
            .paging(0, k)
            .return_fields("id", "title", "dist")
            .dialect(2)
        )
        # decode_responses would mangle the blob, so it goes as a query param.
        raw = self.r.ft(INDEX).search(
            rq, query_params={"blob": np.asarray(vector, dtype=np.float32).tobytes()}
        )
        return [
            Hit(id=self._doc_id(d), score=1.0 - float(d.dist), title=getattr(d, "title", ""))
            for d in raw.docs
        ]

    def close(self) -> None:
        self.r.close()
