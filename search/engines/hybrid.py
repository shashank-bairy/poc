"""Hybrid retrieval: BM25 and vectors in parallel, fused with RRF.

Why fusing helps at all:

  * Keyword search nails exact and rare terms -- product codes, author names,
    a misspelling the user is sure about. A vector model has no special
    representation for a token it has barely seen.
  * Vector search nails paraphrase -- a question finding the statement that
    answers it, with no shared terms at all.
  * The two fail on *different* queries, so fusing the rankings beats either.

**Reciprocal Rank Fusion**, and its appeal is what it ignores:

    score(d) = sum over retrievers of  1 / (k + rank(d))      k = 60

Ranks only, never scores. BM25 scores are unbounded and corpus-dependent while
cosines sit in [-1, 1]; putting them on one scale needs assumptions that
quietly differ per query, and RRF needs none. k=60 is the constant from the
original paper: it damps the top ranks so one retriever cannot dominate.

Measured on SciFact: BM25 0.668, dense alone 0.647, RRF 0.708 nDCG@10. Dense
*loses* to BM25 and the fusion still beats both -- which is the whole argument.
"""

from __future__ import annotations

import argparse
from typing import Iterable

from core.common import Doc, Hit, IndexStats, Query

RRF_K = 60


def rrf(rankings: list[list[str]], k: int = RRF_K, weights: list[float] | None = None):
    """Fuse ranked ID lists -> [(id, score)] best first."""
    weights = weights or [1.0] * len(rankings)
    scores: dict[str, float] = {}
    for ranking, w in zip(rankings, weights):
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class HybridEngine:
    """Elasticsearch by default: inverted index and dense_vector in one index,
    so the two retrievals are two queries against one service."""

    name = "hybrid"

    def __init__(self, backend: str = "elasticsearch", candidates: int = 50):
        if backend == "elasticsearch":
            from engines.elasticsearch import ElasticEngine

            self.engine = ElasticEngine()
        elif backend == "redis":
            from engines.redis import RedisEngine

            self.engine = RedisEngine(scorer="BM25STD")
        elif backend == "postgres":
            from engines.postgres import PostgresEngine

            self.engine = PostgresEngine()
        else:
            raise ValueError(f"unknown hybrid backend: {backend}")
        self.backend = backend
        # Fuse deeper than you return, or a doc ranked 30th by BM25 and 5th by
        # the vector index can never reach the final top 10.
        self.candidates = candidates
        self.name = f"hybrid[{backend}]"

    def index(self, docs: Iterable[Doc]) -> IndexStats:
        from core import embed

        docs = list(docs)
        stats = self.engine.index(docs, with_vectors=True)
        vectors = embed.encode([embed.doc_text(d) for d in docs])
        add_s = self.engine.add_vectors({d.id: v for d, v in zip(docs, vectors)})
        return IndexStats(
            docs=stats.docs,
            build_s=stats.build_s + add_s,
            size_bytes=stats.size_bytes,
            notes=f"{stats.notes}; +{add_s:.1f}s embedding and vector load",
        )

    def search(self, q: Query) -> list[Hit]:
        from core import embed

        bm25_hits = self.engine.search(Query(**{**q.__dict__, "limit": self.candidates, "offset": 0}))
        vec_hits = self.engine.vector_search(embed.encode([q.text])[0], k=self.candidates)

        fused = rrf([[h.id for h in bm25_hits], [h.id for h in vec_hits]])
        titles = {h.id: h.title for h in (*bm25_hits, *vec_hits)}
        return [
            Hit(id=i, score=s, title=titles.get(i, ""))
            for i, s in fused[q.offset : q.offset + q.limit]
        ]

    def count(self, q: Query) -> int:
        """A vector index returns k for any query, so only the BM25 count means anything."""
        return self.engine.count(q)

    def facet(self, q: Query, top: int = 10):
        return self.engine.facet(q, top=top)

    def close(self) -> None:
        self.engine.close()


class DenseEngine:
    """Vectors alone -- the second baseline, without which 'hybrid beats BM25'
    says nothing about whether the fusion helped."""

    name = "dense"

    def __init__(self, backend: str = "elasticsearch"):
        self.hybrid = HybridEngine(backend=backend)
        self.engine = self.hybrid.engine
        self.name = f"dense[{backend}]"

    def index(self, docs):
        return self.hybrid.index(docs)

    def search(self, q: Query) -> list[Hit]:
        from core import embed

        return self.engine.vector_search(embed.encode([q.text])[0], k=q.limit)

    def count(self, q: Query) -> int:
        return q.limit

    def facet(self, q: Query, top: int = 10):
        return self.engine.facet(q, top=top)

    def close(self) -> None:
        self.hybrid.close()


def main() -> None:
    from core import embed
    from core.common import load_docs

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="elasticsearch")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--query", default="how do neural networks forget old tasks")
    args = ap.parse_args()

    eng = HybridEngine(backend=args.backend)
    print(eng.index(load_docs(limit=args.limit)))

    q = Query(kind="term", text=args.query, limit=10)
    bm25 = eng.engine.search(q)
    vec = eng.engine.vector_search(embed.encode([args.query])[0], k=10)

    print(f"\nquery: {args.query}\n")
    for label, hits in (("BM25", bm25), ("vector", vec), ("RRF", eng.search(q))):
        print(f"-- {label}")
        for i, h in enumerate(hits[:5], 1):
            print(f"   {i}. {h.title[:72]}")
    eng.close()


if __name__ == "__main__":
    main()
