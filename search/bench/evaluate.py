"""Relevance pass: nDCG@10, recall@10 and MRR against BEIR judgements.

Deliberately a different dataset from bench/compare.py's -- reporting a
relevance score beside a latency measured on another corpus is how benchmarks
mislead. SciFact is ~5k docs; nothing here is a latency measurement.

The metrics, and why all three:

    nDCG@10    the standard IR measure. Rewards putting relevant documents
               *higher*, not merely including them. Lead with this one.
    recall@10  did the relevant documents show up at all, order ignored.
    MRR        how far down the first relevant result is -- the metric that
               matches how people actually use a search box.

    uv run python -m bench.evaluate
    uv run python -m bench.evaluate --engines postgres,redis-tfidf,redis-bm25std
"""

from __future__ import annotations

import argparse
import math
import os
import sys

from core.common import BEIR_DIR, Query, load_docs, read_jsonl, table, timed

K = 10
DEFAULT_ENGINES = (
    "postgres,redis-tfidf,redis-bm25,redis-bm25std,lucene,elasticsearch,opensearch,solr"
)


def dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_ids: list[str], rels: dict[str, int], k: int = K) -> float:
    """Rewards relevant documents placed higher, not merely included."""
    gains = [float(rels.get(did, 0)) for did in ranked_ids[:k]]
    denom = dcg([float(g) for g in sorted(rels.values(), reverse=True)[:k]])
    return dcg(gains) / denom if denom else 0.0


def recall_at_k(ranked_ids: list[str], rels: dict[str, int], k: int = K) -> float:
    relevant = {d for d, g in rels.items() if g > 0}
    return len(relevant & set(ranked_ids[:k])) / len(relevant) if relevant else 0.0


def mrr(ranked_ids: list[str], rels: dict[str, int], k: int = K) -> float:
    """How far down the first relevant result is -- how a search box is used."""
    for i, did in enumerate(ranked_ids[:k], 1):
        if rels.get(did, 0) > 0:
            return 1.0 / i
    return 0.0


def load_eval_set(dataset: str):
    base = os.path.join(BEIR_DIR, dataset)
    docs_path = os.path.join(base, "docs.jsonl")
    if not os.path.exists(docs_path):
        sys.exit(f"missing {docs_path}\nRun:  uv run python -m corpora.beir --dataset {dataset}")
    docs = load_docs(docs_path)
    queries = list(read_jsonl(os.path.join(base, "queries.jsonl")))
    qrels: dict[str, dict[str, int]] = {}
    with open(os.path.join(base, "qrels.tsv")) as fh:
        next(fh)
        for line in fh:
            qid, did, score = line.strip().split("\t")
            qrels.setdefault(qid, {})[did] = int(score)
    return docs, queries, qrels


def open_engine(name: str):
    if name == "postgres":
        from engines.postgres import PostgresEngine

        return PostgresEngine()
    if name.startswith("redis"):
        from engines.redis import RedisEngine

        # redis-tfidf / redis-bm25 / redis-bm25std: same index, one flag apart.
        return RedisEngine(scorer=name.split("-", 1)[1].upper() if "-" in name else "BM25STD")
    if name == "lucene":
        from engines.lucene import LuceneEngine

        return LuceneEngine()
    if name == "elasticsearch":
        from engines.elasticsearch import ElasticEngine

        return ElasticEngine()
    if name == "opensearch":
        from engines.opensearch import OpenSearchEngine

        return OpenSearchEngine()
    if name == "solr":
        from engines.solr import SolrEngine

        return SolrEngine()
    if name == "hybrid":
        from engines.hybrid import HybridEngine

        return HybridEngine()
    if name == "dense":
        from engines.hybrid import DenseEngine

        return DenseEngine()
    sys.exit(f"unknown engine: {name}")


def evaluate(engine, queries, qrels) -> dict:
    n, failures = 0, 0
    scores = {"ndcg": 0.0, "recall": 0.0, "mrr": 0.0}
    latencies = []
    for q in queries:
        rels = qrels.get(q["id"], {})
        if not rels:
            continue
        # Free text, no per-engine tuning: the only way the rows stay comparable.
        timing, hits = timed(engine.search, Query(kind="term", text=q["text"], limit=K), repeat=1, warmup=0)
        if timing.error:
            failures += 1
            continue
        latencies.append(timing.p50_ms)
        ids = [h.id for h in hits]
        scores["ndcg"] += ndcg_at_k(ids, rels)
        scores["recall"] += recall_at_k(ids, rels)
        scores["mrr"] += mrr(ids, rels)
        n += 1
    if not n:
        return {"queries": 0, "failures": failures}
    return {
        "queries": n,
        "failures": failures,
        "ndcg": scores["ndcg"] / n,
        "recall": scores["recall"] / n,
        "mrr": scores["mrr"] / n,
        "ms": sum(latencies) / len(latencies),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="scifact")
    ap.add_argument("--engines", default=DEFAULT_ENGINES)
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args()

    docs, queries, qrels = load_eval_set(args.dataset)
    judged = sum(len(v) for v in qrels.values())
    print(f"{args.dataset}: {len(docs):,} docs, {len(qrels):,} judged queries, {judged:,} judgements")

    rows = []
    for name in args.engines.split(","):
        try:
            engine = open_engine(name)
        except Exception as exc:  # noqa: BLE001
            rows.append([name, "-", "-", "-", "-", f"unavailable: {type(exc).__name__}"])
            continue
        try:
            if not args.skip_index:
                engine.index(docs)
            res = evaluate(engine, queries, qrels)
            if not res["queries"]:
                rows.append([name, "-", "-", "-", "-", f"no queries answered ({res['failures']} failed)"])
            else:
                rows.append(
                    [
                        name,
                        f"{res['ndcg']:.4f}",
                        f"{res['recall']:.4f}",
                        f"{res['mrr']:.4f}",
                        f"{res['ms']:.2f}",
                        f"{res['queries']} queries"
                        + (f", {res['failures']} failed" if res["failures"] else ""),
                    ]
                )
        except Exception as exc:  # noqa: BLE001
            rows.append([name, "-", "-", "-", "-", f"{type(exc).__name__}: {exc}"])
        finally:
            engine.close()

    print(f"\n== relevance on {args.dataset}, k={K} ==")
    print(table(["engine", f"nDCG@{K}", f"recall@{K}", "MRR", "ms/query", "notes"], rows))
    print(
        "\nRead it in two places:\n"
        "  postgres vs the rest          -- ts_rank has no IDF term, no length normalization\n"
        "  redis-tfidf vs -bm25 vs -bm25std -- identical index, one scorer flag apart"
    )


if __name__ == "__main__":
    main()
