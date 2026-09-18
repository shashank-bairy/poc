"""OpenSearch -- the Elasticsearch 7.10 fork, running the same query functions.

Every query function is inherited from ElasticEngine unchanged. Not copied,
inherited: there is nothing to express differently. That is the finding -- for
ordinary retrieval, swapping one product for the other is a client-library
change.

    curl -s localhost:9203/papers/_search -H 'content-type: application/json' \
      -d '{"size":10,"query":{"match":{"abstract":"retrieval"}}}' | jq

Where it does diverge:
  * Licensing. Apache 2.0 here; ES left it at 7.11. That is why the fork
    exists, and it is not a technical difference at all.
  * Vectors. knn_vector + a `knn` clause INSIDE the query, versus ES's
    dense_vector + a top-level `knn`. The index also needs index.knn: true.
    vector_search below is the only query function this class overrides.
  * Client. opensearch-py is a fork of elasticsearch-py 7.x, so .search() and
    friends still take body=, which ES 8 removed.

    uv run python -m engines.opensearch
"""

from __future__ import annotations

from opensearchpy import OpenSearch, helpers

from core.common import OS_URL, Hit
from engines.elasticsearch import ElasticEngine


class OpenSearchEngine(ElasticEngine):
    name = "opensearch"
    index_name = "papers"

    # knn_vector, not dense_vector; and k-NN is opt-in per index here.
    vector_field = {
        "type": "knn_vector",
        "dimension": 384,
        "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
    }
    vector_index_setting = {"index.knn": True}

    def vector_search(self, vector, k: int = 10) -> list[Hit]:
        # The one query shape that differs: knn inside the query, not beside it.
        return self.search({
            "size": k,
            "query": {"knn": {"vec": {"vector": list(map(float, vector)), "k": k}}},
        })

    def __init__(self, url: str = OS_URL):
        self.client = OpenSearch(url, timeout=120)

    def _search(self, body: dict) -> dict:
        return self.client.search(index=self.index_name, body=body)

    def _get(self, doc_id: str) -> dict | None:
        if not self.client.exists(index=self.index_name, id=doc_id):
            return None
        return self.client.get(index=self.index_name, id=doc_id)

    def _analyze(self, field: str, text: str) -> list[str]:
        res = self.client.indices.analyze(index=self.index_name, body={"field": field, "text": text})
        return [t["token"] for t in res["tokens"]]

    def _cat_segments(self) -> list[dict]:
        return self.client.cat.segments(index=self.index_name, format="json", bytes="b", h="segment,size")

    def _bulk(self, actions: list) -> None:
        # opensearchpy.helpers, not elasticsearch.helpers -- the two clients are
        # not interchangeable even though the request bodies are.
        helpers.bulk(self.client, actions, chunk_size=1000, request_timeout=300)

    def _create(self, mapping: dict) -> None:
        self.client.indices.create(index=self.index_name, body=mapping)

    def _restore_refresh(self) -> None:
        self.client.indices.put_settings(index=self.index_name, body={"refresh_interval": "1s"})

    def close(self) -> None:
        self.client.close()


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(OpenSearchEngine())
