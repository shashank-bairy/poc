"""Elasticsearch: Lucene + JSON REST + cluster coordination.

Two concepts do most of the work, and conflating them is the usual source of
"why does my search return nothing":

  mapping         field *types*. `text` is analyzed into tokens; `keyword` is
                  stored verbatim as one token. Types decide what is even
                  possible -- you cannot aggregate on `text`, and you cannot
                  full-text match on `keyword`.
  analysis        the chain applied to `text` fields: character filters ->
                  tokenizer -> token filters (lowercase, stop, stemmer). It
                  runs at index time AND at query time, and the two must agree.

Everything else follows from that pair:

  match vs term   `match` analyzes the query string, `term` does not. So
                  term: {"title": "Retrieval"} against an English-analyzed
                  field silently returns nothing -- the index holds `retriev`.
  multi-field     `title` (text) + `title.raw` (keyword) lets one source field
                  both search and aggregate/sort.
  query vs filter a `filter` clause contributes no score and is cacheable in
                  the node query cache; a `must` clause scores.
  refresh         a document is searchable one refresh (default 1s) after the
                  write, because a refresh is what cuts a new Lucene segment.

body_for() is reused verbatim by engines/opensearch.py -- that reuse is the
OpenSearch finding.
"""

from __future__ import annotations

import time
from typing import Iterable

from elasticsearch import Elasticsearch, helpers

from core.common import EMBED_DIMS, ES_URL, Doc, Hit, IndexStats, Query

INDEX = "papers"

MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "refresh_interval": "1s",  # a doc is searchable one refresh after the write
    },
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "title": {
                "type": "text",
                "analyzer": "english",
                # Multi-field: `title` searches, `title.raw` sorts/aggregates.
                "fields": {"raw": {"type": "keyword", "ignore_above": 256}},
            },
            "abstract": {"type": "text", "analyzer": "english"},
            # Multi-valued keyword: one index term per array value.
            "authors": {"type": "keyword"},
            "categories": {"type": "keyword"},
            "update_date": {"type": "date"},
            "version_count": {"type": "integer"},
            "doi": {"type": "keyword", "index": False},  # stored, never searchable
        }
    },
}

VECTOR_FIELD = {"type": "dense_vector", "dims": EMBED_DIMS, "index": True, "similarity": "cosine"}


def body_for(q: Query) -> dict:
    body: dict = {"size": q.limit, "from": q.offset}

    if q.kind == "phrase":
        body["query"] = {"match_phrase": {"abstract": q.text}}
    elif q.kind == "boolean":
        body["query"] = {
            "bool": {
                "must": [{"match": {"abstract": t}} for t in q.must],
                # `should` beside `must` boosts rather than filters unless
                # minimum_should_match says otherwise.
                "should": [{"match": {"abstract": t}} for t in q.should],
                "minimum_should_match": 1 if q.should else 0,
                "must_not": [{"match": {"abstract": t}} for t in q.must_not],
            }
        }
    elif q.kind == "prefix":
        # Prefixes only the last term. An edge_ngram analyzer would be faster
        # at query time and much larger on disk.
        body["query"] = {"match_phrase_prefix": {"abstract": q.text}}
    elif q.kind == "fuzzy":
        # AUTO scales edit distance with term length; `match` analyzes first,
        # which is why this finds stems where raw FuzzyQuery does not.
        body["query"] = {"match": {"abstract": {"query": q.text, "fuzziness": "AUTO"}}}
    elif q.kind == "filtered":
        # filter, not must: no score contribution, cacheable in the node query cache.
        body["query"] = {
            "bool": {
                "must": [{"match": {"abstract": q.text}}],
                "filter": [{"range": {"update_date": {"gte": q.date_from}}}],
            }
        }
    elif q.kind == "boosted":
        # best_fields takes the best single field score; most_fields sums them,
        # and that choice moves the ranking more than the boost values do.
        body["query"] = {
            "multi_match": {
                "query": q.text,
                "fields": [f"{f}^{w}" for f, w in q.boosts],
                "type": "best_fields",
            }
        }
    else:
        body["query"] = {"match": {"abstract": q.text}}

    if q.kind == "facet":
        body["aggs"] = {"facets": {"terms": {"field": q.facet_field, "size": 10}}}
    if q.sort_field:
        body["sort"] = [{q.sort_field: "desc"}]  # doc values, no scoring
    if q.kind == "highlight":
        body["highlight"] = {
            "fields": {"abstract": {"fragment_size": 150, "number_of_fragments": 1}}
        }
    return body


class ElasticEngine:
    name = "elasticsearch"
    index_name = INDEX

    def __init__(self, url: str = ES_URL):
        self.client = Elasticsearch(url, request_timeout=120)

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        if self.client.indices.exists(index=self.index_name):
            self.client.indices.delete(index=self.index_name)

        mapping = {"mappings": {"properties": dict(MAPPING["mappings"]["properties"])}}
        mapping["settings"] = dict(MAPPING["settings"])
        if with_vectors:
            mapping["mappings"]["properties"]["vec"] = VECTOR_FIELD
        # Refresh off while bulk loading: every refresh cuts a new segment.
        mapping["settings"]["refresh_interval"] = "-1"
        self.client.indices.create(index=self.index_name, **mapping)

        t0 = time.perf_counter()
        helpers.bulk(
            self.client,
            ({"_index": self.index_name, "_id": d.id, "_source": d.to_json()} for d in docs),
            chunk_size=1000,
            request_timeout=300,
        )
        self.client.indices.refresh(index=self.index_name)
        # forcemerge only to make the size comparable between runs.
        self.client.indices.forcemerge(index=self.index_name, max_num_segments=1)
        build_s = time.perf_counter() - t0
        self.client.indices.put_settings(index=self.index_name, settings={"refresh_interval": "1s"})

        stats = self.client.indices.stats(index=self.index_name)["indices"][self.index_name]["primaries"]
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=stats["store"]["size_in_bytes"],
            notes=f"{stats['segments']['count']} segment(s) after forcemerge",
        )

    def _search(self, body: dict) -> dict:
        return self.client.search(index=self.index_name, **body)

    def search(self, q: Query) -> list[Hit]:
        res = self._search(body_for(q))
        return [
            Hit(
                id=h["_id"],
                score=float(h["_score"] or 0.0),  # a sorted query scores nothing
                title=h["_source"].get("title", ""),
                highlight=h.get("highlight", {}).get("abstract", [""])[0],
            )
            for h in res["hits"]["hits"]
        ]

    def count(self, q: Query) -> int:
        body = body_for(q)
        for key in ("sort", "highlight", "aggs"):
            body.pop(key, None)
        body["size"] = 0
        # ES stops counting at 10k and reports a lower bound by default.
        body["track_total_hits"] = True
        return int(self._search(body)["hits"]["total"]["value"])

    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]:
        body = body_for(q)
        body["aggs"] = {"facets": {"terms": {"field": q.facet_field, "size": top}}}
        body["size"] = 0
        res = self._search(body)
        return [(b["key"], b["doc_count"]) for b in res["aggregations"]["facets"]["buckets"]]

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = EMBED_DIMS) -> float:
        t0 = time.perf_counter()
        helpers.bulk(
            self.client,
            (
                {
                    "_op_type": "update",
                    "_index": self.index_name,
                    "_id": k,
                    "doc": {"vec": list(map(float, v))},
                }
                for k, v in vectors.items()
            ),
            chunk_size=500,
            request_timeout=300,
        )
        self.client.indices.refresh(index=self.index_name)
        return time.perf_counter() - t0

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        res = self.client.search(
            index=self.index_name,
            knn={
                "field": "vec",
                "query_vector": list(map(float, vector)),
                "k": k,
                "num_candidates": max(50, k * 10),
            },
            size=k,
        )
        return [
            Hit(id=h["_id"], score=float(h["_score"]), title=h["_source"].get("title", ""))
            for h in res["hits"]["hits"]
        ]

    def close(self) -> None:
        self.client.close()
