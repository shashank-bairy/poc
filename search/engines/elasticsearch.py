"""Elasticsearch. One function per query, the request body inside the function.

Every body here is POSTed to /papers/_search unchanged, and runs as-is in curl:

    curl -s localhost:9202/papers/_search -H 'content-type: application/json' \
      -d '{"size":10,"query":{"match":{"abstract":"retrieval"}}}' | jq

The rule worth memorising: `match` analyzes the query string, `term` does not.
A term query on an English-analyzed field silently returns nothing, because the
index holds the stem 'retriev', never 'Retrieval'.

engines/opensearch.py inherits every one of these functions unchanged.

    uv run python -m engines.elasticsearch
"""

from __future__ import annotations

import time
from typing import Iterable

from elasticsearch import Elasticsearch, helpers

from core.common import ES_URL, Doc, Hit, IndexStats


class ElasticEngine:
    name = "elasticsearch"
    index_name = "papers"

    # ---------------------------------------------------------------- queries

    def q1_term(self):
        return self.search({
            "size": 10,
            "query": {"match": {"abstract": "retrieval"}},
        })

    def q1_term_count(self) -> int:
        # size 0 fetches nothing, so this is matching cost alone.
        # track_total_hits: ES stops counting at 10,000 and reports a lower
        # bound unless forced.
        return self.count({
            "size": 0,
            "track_total_hits": True,
            "query": {"match": {"abstract": "retrieval"}},
        })

    def q2_phrase(self):
        return self.search({
            "size": 10,
            "query": {"match_phrase": {"abstract": "attention mechanism"}},
        })

    def q3_boolean(self):
        # `should` beside `must` boosts rather than filters unless
        # minimum_should_match says otherwise.
        return self.search({
            "size": 10,
            "query": {
                "bool": {
                    "must": [{"match": {"abstract": "retrieval"}}],
                    "should": [
                        {"match": {"abstract": "dense"}},
                        {"match": {"abstract": "sparse"}},
                    ],
                    "minimum_should_match": 1,
                    "must_not": [{"match": {"abstract": "image"}}],
                }
            },
        })

    def q4_prefix(self):
        # Prefixes only the last term, against the real index. An edge_ngram
        # analyzer would be faster at query time and far larger on disk.
        return self.search({
            "size": 10,
            "query": {"match_phrase_prefix": {"abstract": "quant"}},
        })

    def q5_fuzzy(self):
        # fuzziness AUTO scales edit distance with term length (0 for <=2
        # chars, 1 for <=5, 2 beyond). `match` analyzes first, which is why
        # this finds the stem where a raw Lucene FuzzyQuery on the same string
        # does not.
        return self.search({
            "size": 10,
            "query": {"match": {"abstract": {"query": "transfomer", "fuzziness": "AUTO"}}},
        })

    def q6_filter_text(self):
        # filter, not must: contributes no score and is cacheable in the node
        # query cache. This separation is what Postgres has no equivalent of.
        return self.search({
            "size": 10,
            "query": {
                "bool": {
                    "must": [{"match": {"abstract": "graph neural"}}],
                    "filter": [{"range": {"update_date": {"gte": "2023-01-01"}}}],
                }
            },
        })

    def q7_facet(self, top: int = 10) -> list[tuple[str, int]]:
        # categories is a keyword array, so one document lands in several
        # buckets and the counts sum past the hit count.
        res = self._search({
            "size": 0,
            "query": {"match": {"abstract": "retrieval"}},
            "aggs": {"facets": {"terms": {"field": "categories", "size": 10}}},
        })
        return [(b["key"], b["doc_count"]) for b in res["aggregations"]["facets"]["buckets"]][:top]

    def q8_sort_date(self):
        # Sorting reads doc values and skips scoring entirely, so _score is null.
        return self.search({
            "size": 10,
            "query": {"match": {"abstract": "retrieval"}},
            "sort": [{"update_date": "desc"}],
        })

    def q9_deep_page(self):
        # from/size collects 5,000 hits to return 10. search_after is the fix
        # and needs the previous page's sort values.
        return self.search({
            "size": 10,
            "from": 4990,
            "query": {"match": {"abstract": "learning"}},
        })

    def q10_highlight(self):
        return self.search({
            "size": 10,
            "query": {"match": {"abstract": "knowledge distillation"}},
            "highlight": {
                "fields": {"abstract": {"fragment_size": 150, "number_of_fragments": 1}}
            },
        })

    def q11_boosted(self):
        # best_fields takes the single best field score; most_fields sums them,
        # and that choice moves the ranking more than the boost values do.
        return self.search({
            "size": 10,
            "query": {
                "multi_match": {
                    "query": "language model",
                    "fields": ["title^5", "abstract^1"],
                    "type": "best_fields",
                }
            },
        })

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
        """_source, then the terms the analyzer actually indexed.

        `_source` is the JSON you sent, kept verbatim in a separate stored
        field. It is never searched. What gets searched is the output of the
        analyzer configured for each field in the mapping -- which _analyze
        shows, and which is why a `text` field and a `keyword` field holding
        the same string match completely different queries.
        """
        doc = self._get(doc_id)
        if doc is None:
            return f"no such doc: {doc_id}"
        src = doc["_source"]
        out = ["_source (verbatim JSON, not searchable):"]
        for k, v in src.items():
            if k == "vec":
                v = f"<{len(v)} floats>"
            out.append(f"  {k:<12} {str(v)[:96]}")
        out.append("\nanalyzed terms (what the inverted index holds):")
        out.append(f"  title       (text)    {self._analyze('title', src.get('title', ''))}")
        out.append(f"  abstract    (text)    {self._analyze('abstract', src.get('abstract', ''))[:14]} ...")
        out.append(f"  categories  (keyword) {self._analyze('categories', (src.get('categories') or [''])[0])}"
                   "   <- not split, not stemmed, not lowercased")
        return "\n".join(out)

    # ------------------------------------------------------------- the runner

    def search(self, body: dict) -> list[Hit]:
        res = self._search(body)
        return [
            Hit(
                id=h["_id"],
                score=float(h["_score"] or 0.0),  # a sorted query scores nothing
                title=h["_source"].get("title", ""),
                highlight=h.get("highlight", {}).get("abstract", [""])[0],
            )
            for h in res["hits"]["hits"]
        ]

    def count(self, body: dict) -> int:
        return int(self._search(body)["hits"]["total"]["value"])

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        return self.search({
            "size": k,
            "knn": {
                "field": "vec",
                "query_vector": list(map(float, vector)),
                "k": k,
                "num_candidates": max(50, k * 10),
            },
        })

    # --- the calls OpenSearch overrides, because its client is the 7.x fork ---

    def _search(self, body: dict) -> dict:
        return self.client.search(index=self.index_name, **body)

    def _get(self, doc_id: str) -> dict | None:
        if not self.client.exists(index=self.index_name, id=doc_id):
            return None
        return self.client.get(index=self.index_name, id=doc_id)

    def _analyze(self, field: str, text: str) -> list[str]:
        res = self.client.indices.analyze(index=self.index_name, field=field, text=text)
        return [t["token"] for t in res["tokens"]]

    def _cat_segments(self) -> list[dict]:
        return self.client.cat.segments(index=self.index_name, format="json", bytes="b", h="segment,size")

    def _bulk(self, actions: list) -> None:
        helpers.bulk(self.client, actions, chunk_size=1000, request_timeout=300)

    def _create(self, mapping: dict) -> None:
        self.client.indices.create(index=self.index_name, **mapping)

    def _restore_refresh(self) -> None:
        self.client.indices.put_settings(index=self.index_name, settings={"refresh_interval": "1s"})

    def __init__(self, url: str = ES_URL):
        self.client = Elasticsearch(url, request_timeout=120)

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        if self.client.indices.exists(index=self.index_name):
            self.client.indices.delete(index=self.index_name)

        mapping = {
            "settings": dict(MAPPING["settings"]),
            "mappings": {"properties": dict(MAPPING["mappings"]["properties"])},
        }
        if with_vectors:
            mapping["mappings"]["properties"]["vec"] = self.vector_field
            mapping["settings"].update(self.vector_index_setting)
        # Refresh off while bulk loading: every refresh cuts a new segment.
        mapping["settings"]["refresh_interval"] = "-1"
        self._create(mapping)

        t0 = time.perf_counter()
        self._bulk([{"_index": self.index_name, "_id": d.id, "_source": d.to_json()} for d in docs])
        self.client.indices.refresh(index=self.index_name)
        # forcemerge only so the reported size is comparable between runs.
        self.client.indices.forcemerge(index=self.index_name, max_num_segments=1)
        build_s = time.perf_counter() - t0
        self._restore_refresh()

        size, segments = self._live_size()
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=size,
            notes=f"{segments} segment(s) after forcemerge",
        )

    def _live_size(self) -> tuple[int, int]:
        """Bytes in the live segments, via _cat/segments.

        Not indices.stats store size: that also counts files a merge has
        superseded but not yet deleted, and files an open reader still holds.
        Read right after forcemerge it gave 464 MB for an index whose live
        segment is 231 MB.
        """
        segs = self._cat_segments()
        return sum(int(x["size"]) for x in segs), len(segs)

    def add_vectors(self, vectors: dict[str, list[float]], dims: int = 384) -> float:
        t0 = time.perf_counter()
        self._bulk([
            {"_op_type": "update", "_index": self.index_name, "_id": k,
             "doc": {"vec": list(map(float, v))}}
            for k, v in vectors.items()
        ])
        self.client.indices.refresh(index=self.index_name)
        return time.perf_counter() - t0

    def close(self) -> None:
        self.client.close()

    vector_field = {"type": "dense_vector", "dims": 384, "index": True, "similarity": "cosine"}
    vector_index_setting: dict = {}


MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        # A document is searchable one refresh after the write, because a
        # refresh is what cuts a new Lucene segment.
        "refresh_interval": "1s",
    },
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "title": {
                "type": "text",
                "analyzer": "english",
                # The multi-field: `title` searches, `title.raw` sorts/aggregates.
                "fields": {"raw": {"type": "keyword", "ignore_above": 256}},
            },
            "abstract": {"type": "text", "analyzer": "english"},
            # Multi-valued keyword: one index term per array value.
            "authors": {"type": "keyword"},
            "categories": {"type": "keyword"},
            "update_date": {"type": "date"},
            "version_count": {"type": "integer"},
            # index:false -- returned but never searchable.
            "doi": {"type": "keyword", "index": False},
        }
    },
}


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(ElasticEngine())
