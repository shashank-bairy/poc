"""Performance pass: same corpus, same query suite, every engine.

Three tables: index build cost, per-query latency split into matching
(count-only) and matching+fetching, and top-10 agreement between engines.

    uv run python -m bench.compare --limit 5000 --engines postgres,redis
"""

from __future__ import annotations

import argparse
import sys

from core.common import ARXIV_DOCS, QUERY_SUITE, Query, Unsupported, load_docs, table, timed

ALL_ENGINES = ("postgres", "redis", "lucene", "elasticsearch", "opensearch", "solr")


def open_engines(names: list[str]) -> list:
    """Imported lazily: a missing service should not stop the other five."""
    engines = []
    for name in names:
        try:
            if name == "postgres":
                from engines.postgres import PostgresEngine

                engines.append(PostgresEngine())
            elif name == "redis":
                from engines.redis import RedisEngine

                engines.append(RedisEngine(scorer="BM25STD"))
            elif name == "lucene":
                from engines.lucene import LuceneEngine

                engines.append(LuceneEngine())
            elif name == "elasticsearch":
                from engines.elasticsearch import ElasticEngine

                engines.append(ElasticEngine())
            elif name == "opensearch":
                from engines.opensearch import OpenSearchEngine

                engines.append(OpenSearchEngine())
            elif name == "solr":
                from engines.solr import SolrEngine

                engines.append(SolrEngine())
            else:
                sys.exit(f"unknown engine: {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  skipping {name}: {type(exc).__name__}: {exc}")
    return engines


def index_all(engines, docs) -> None:
    rows = []
    corpus_mb = sum(len(d.title) + len(d.abstract) for d in docs) / 1024 / 1024
    for eng in engines:
        try:
            stats = eng.index(docs)
            rows.append(
                [
                    eng.name,
                    f"{stats.docs:,}",
                    f"{stats.build_s:.1f}s",
                    f"{stats.size_mb:.1f} MB",
                    f"{stats.size_mb / corpus_mb:.2f}x" if corpus_mb else "-",
                    stats.notes,
                ]
            )
        except Exception as exc:  # noqa: BLE001
            rows.append([eng.name, "-", "-", "-", "-", f"FAILED {type(exc).__name__}: {exc}"])
    print(f"\n== index build  (raw text {corpus_mb:.1f} MB) ==")
    print(table(["engine", "docs", "build", "index size", "vs corpus", "notes"], rows))


def latency_table(engines, queries: tuple[Query, ...], repeat: int) -> dict:
    """Returns {(query label, engine): [hit ids]} for the agreement table."""
    tops: dict = {}
    rows = []
    for q in queries:
        for eng in engines:
            t_count, total = timed(eng.count, q, repeat=repeat)
            t_search, hits = timed(eng.search, q, repeat=repeat)
            if t_search.error:
                rows.append([q.name(), eng.name, "-", "-", "-", t_search.error[:48]])
                continue
            tops[(q.label, eng.name)] = [h.id for h in hits]
            rows.append(
                [
                    q.name(),
                    eng.name,
                    f"{t_count.p50_ms:.2f}" if not t_count.error else "-",
                    f"{t_search.p50_ms:.2f}",
                    f"{t_search.p95_ms:.2f}",
                    f"{total if isinstance(total, int) else '?':>9} matched, {len(hits)} returned",
                ]
            )
    print(f"\n== latency, {repeat} warm runs per cell ==")
    print(table(["query", "engine", "count ms", "fetch ms", "p95 ms", "hits"], rows))
    return tops


def facet_table(engines, q: Query) -> None:
    rows = []
    for eng in engines:
        try:
            rows.append([eng.name, ", ".join(f"{v}:{c}" for v, c in eng.facet(q, top=5))])
        except Unsupported as exc:
            rows.append([eng.name, f"unsupported: {exc}"])
        except Exception as exc:  # noqa: BLE001
            rows.append([eng.name, f"{type(exc).__name__}: {exc}"])
    print(f"\n== facets on '{q.text}' (multi-valued: counts sum past the hit count) ==")
    print(table(["engine", f"top {q.facet_field}"], rows))
    if all(not r[1] for r in rows):
        print(f"  (empty: this corpus has no {q.facet_field}. The BEIR sets carry none.)")


def agreement_table(tops: dict, queries, engines) -> None:
    """Overlap with the leftmost engine that answered. Not a correctness score:
    there is no correct answer, which is the point."""
    names = [e.name for e in engines]
    rows = []
    for q in queries:
        present = [n for n in names if (q.label, n) in tops]
        if len(present) < 2:
            continue
        base_ids = set(tops[(q.label, present[0])])
        row = [q.name()]
        for n in names:
            if n == present[0]:
                row.append("--")
            elif (q.label, n) in tops:
                row.append(f"{len(base_ids & set(tops[(q.label, n)]))}/{len(base_ids)}")
            else:
                row.append("-")
        rows.append(row)
    if rows:
        print("\n== top-10 overlap with the leftmost engine that answered ==")
        print(table(["query", *names], rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engines", default=",".join(ALL_ENGINES))
    ap.add_argument("--limit", type=int, default=50_000, help="docs to index")
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--docs", default=ARXIV_DOCS)
    ap.add_argument("--query", type=int, help="run only query N (1-11)")
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args()

    docs = load_docs(args.docs, limit=args.limit)
    print(f"corpus: {len(docs):,} docs from {args.docs}")

    engines = open_engines(args.engines.split(","))
    if not engines:
        sys.exit("no engines available; is docker compose up?")

    if not args.skip_index:
        index_all(engines, docs)

    queries = (QUERY_SUITE[args.query - 1],) if args.query else QUERY_SUITE
    tops = latency_table(engines, queries, args.repeat)
    facet_q = next((q for q in queries if q.kind == "facet"), None)
    if facet_q:
        facet_table(engines, facet_q)
    agreement_table(tops, queries, engines)

    for eng in engines:
        eng.close()


if __name__ == "__main__":
    main()
