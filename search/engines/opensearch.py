"""OpenSearch: the Elasticsearch 7.10 fork, pointed at the same queries.

Every query body in engines/elasticsearch.py is reused unchanged. The brevity
of this module is the finding.

Where it diverges: Apache 2.0 licensing (the reason the fork exists); k-NN is
built in and free but uses `knn_vector` + a `knn` query clause rather than
`dense_vector` + a top-level `knn`; a security plugin instead of x-pack; and
opensearch-py is the 7.x client, so `.search()` still takes `body=`.
"""

from __future__ import annotations

import time

from opensearchpy import OpenSearch, helpers

from core.common import EMBED_DIMS, OS_URL, Hit, IndexStats
from engines.elasticsearch import MAPPING, ElasticEngine

VECTOR_FIELD = {
    "type": "knn_vector",
    "dimension": EMBED_DIMS,
    "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
}


class OpenSearchEngine(ElasticEngine):
    name = "opensearch"
    index_name = "papers"

    def __init__(self, url: str = OS_URL):
        self.client = OpenSearch(url, timeout=120)

    def index(self, docs, with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        if self.client.indices.exists(index=self.index_name):
            self.client.indices.delete(index=self.index_name)

        body = {
            "settings": {**MAPPING["settings"], "refresh_interval": "-1"},
            "mappings": {"properties": dict(MAPPING["mappings"]["properties"])},
        }
        if with_vectors:
            body["mappings"]["properties"]["vec"] = VECTOR_FIELD
            body["settings"]["index.knn"] = True  # opt-in per index; ES needs no switch
        self.client.indices.create(index=self.index_name, body=body)

        t0 = time.perf_counter()
        helpers.bulk(
            self.client,
            [{"_index": self.index_name, "_id": d.id, "_source": d.to_json()} for d in docs],
            chunk_size=1000,
            request_timeout=300,
        )
        self.client.indices.refresh(index=self.index_name)
        self.client.indices.forcemerge(index=self.index_name, max_num_segments=1)
        build_s = time.perf_counter() - t0
        self.client.indices.put_settings(index=self.index_name, body={"refresh_interval": "1s"})

        stats = self.client.indices.stats(index=self.index_name)["indices"][self.index_name]["primaries"]
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=stats["store"]["size_in_bytes"],
            notes=f"{stats['segments']['count']} segment(s); same bodies as ES",
        )

    def _search(self, body: dict) -> dict:
        return self.client.search(index=self.index_name, body=body)

    def add_vectors(self, vectors, dims: int = EMBED_DIMS) -> float:
        t0 = time.perf_counter()
        helpers.bulk(
            self.client,
            [
                {
                    "_op_type": "update",
                    "_index": self.index_name,
                    "_id": k,
                    "doc": {"vec": list(map(float, v))},
                }
                for k, v in vectors.items()
            ],
            chunk_size=500,
            request_timeout=300,
        )
        self.client.indices.refresh(index=self.index_name)
        return time.perf_counter() - t0

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        res = self.client.search(
            index=self.index_name,
            body={"size": k, "query": {"knn": {"vec": {"vector": list(map(float, vector)), "k": k}}}},
        )
        return [
            Hit(id=h["_id"], score=float(h["_score"]), title=h["_source"].get("title", ""))
            for h in res["hits"]["hits"]
        ]

    def close(self) -> None:
        self.client.close()
