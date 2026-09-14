"""Run every query type against every store and print the comparison tables.

Ground truth is a brute-force Haversine scan of points.csv, so precision is
measured against an exact answer rather than against one of the databases.
"""

from __future__ import annotations

import argparse
import statistics
import time

from common import (
    CENTER_LAT,
    CENTER_LNG,
    Hit,
    brute_force_knn,
    brute_force_radius,
    load_points,
)
import h3_layer
import s2_layer

RADII = [100.0, 1_000.0, 10_000.0]
K = 10

METHOD_ATTR = {
    "native": "radius_query",
    "h3": "h3_radius_query",
    "s2": "s2_radius_query",
}

# How each store actually fetches the cells. Every one of these is a single
# round trip -- the shape just differs.
ACCESS_PATH = {
    ("postgres", "h3"): "= ANY(cells)",
    ("postgres", "s2"): "join on ranges",
    ("redis", "h3"): "SUNION",
    ("redis", "s2"): "pipelined ZRANGEBYLEX",
    ("aerospike", "h3"): "batch_read on keys",
    ("aerospike", "s2"): "one query per range",
}


def methods_for(store) -> list[str]:
    return [m for m, attr in METHOD_ATTR.items() if hasattr(store, attr)]


def timed(fn, *args, repeat: int = 5):
    """Run fn repeat times, return (result, median_ms)."""
    result = None
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn(*args)
        samples.append((time.perf_counter() - t0) * 1000)
    return result, statistics.median(samples)


def precision(hits: list[Hit], truth_ids: set[int]) -> tuple[int, int]:
    """(false positives, false negatives) against ground truth."""
    got = {h.id for h in hits}
    return len(got - truth_ids), len(truth_ids - got)


def open_stores(which: list[str]):
    stores = []
    if "postgres" in which:
        from postgres_geo import PostgresStore

        stores.append(PostgresStore())
    if "redis" in which:
        from redis_geo import RedisStore

        stores.append(RedisStore())
    if "aerospike" in which:
        from aerospike_geo import AerospikeStore

        stores.append(AerospikeStore())
    return stores


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(str(r[i]).ljust(widths[i]) for i in range(len(r))) for r in rows)
    return f"{line}\n{sep}\n{body}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--stores",
        default="postgres,redis,aerospike",
        help="comma-separated subset to run",
    )
    ap.add_argument("--skip-load", action="store_true", help="query existing data, do not reload")
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()

    points = load_points()
    which = [s.strip() for s in args.stores.split(",") if s.strip()]
    stores = open_stores(which)

    print(f"dataset: {len(points)} points, query center ({CENTER_LAT}, {CENTER_LNG})\n")

    # --- load / index build ---
    if not args.skip_load:
        rows = []
        for s in stores:
            stats = s.load_data(points)
            idx = ", ".join(f"{k}={v:.2f}s" for k, v in stats["index_s"].items())
            rows.append([s.name, stats["rows"], f"{stats['insert_s']:.2f}s", idx])
        print("LOAD / INDEX BUILD")
        print(table(["store", "rows", "insert", "index build"], rows), "\n")

    # --- radius queries ---
    rows = []
    for radius in RADII:
        truth = {h.id for h in brute_force_radius(points, CENTER_LAT, CENTER_LNG, radius)}
        h3_cells = len(h3_layer.cells_for_disc(CENTER_LAT, CENTER_LNG, radius))
        s2_ranges = len(s2_layer.ranges_for_disc(CENTER_LAT, CENTER_LNG, radius))
        counts = {"native": None, "h3": h3_cells, "s2": s2_ranges}
        for s in stores:
            for label in methods_for(s):
                fn = getattr(s, METHOD_ATTR[label])
                n = counts[label]
                probes = (
                    "" if n is None else f"{n} via {ACCESS_PATH.get((s.name, label), '?')}"
                )
                hits, ms = timed(fn, CENTER_LAT, CENTER_LNG, radius, repeat=args.repeat)
                fp, fn_ = precision(hits, truth)
                rows.append(
                    [s.name, label, f"{int(radius)}m", len(truth), len(hits), f"{ms:.1f}", fp, fn_, probes]
                )
    print("RADIUS QUERY  (truth = brute-force Haversine scan)")
    print(
        table(
            ["store", "method", "radius", "truth", "got", "p50 ms", "false+", "false-", "cells fetched by"],
            rows,
        ),
        "\n",
    )

    # --- KNN ---
    truth_knn = [h.id for h in brute_force_knn(points, CENTER_LAT, CENTER_LNG, K)]
    rows = []
    for s in stores:
        hits, ms = timed(s.knn_query, CENTER_LAT, CENTER_LNG, K, repeat=args.repeat)
        got = [h.id for h in hits]
        rows.append(
            [
                s.name,
                f"k={K}",
                f"{ms:.1f}",
                "exact" if got == truth_knn else f"{len(set(got) & set(truth_knn))}/{K} overlap",
                "native" if s.name == "postgres" else "expanding radius",
            ]
        )
    print("KNN QUERY")
    print(table(["store", "k", "p50 ms", "vs truth", "implementation"], rows), "\n")

    # --- earth-model footnote ---
    for s in stores:
        if s.name == "postgres":
            print("EARTH MODEL (postgres only)")
            rows = []
            for radius in RADII:
                sphere = len(s.radius_query(CENTER_LAT, CENTER_LNG, radius))
                spheroid = s.spheroid_radius_count(CENTER_LAT, CENTER_LNG, radius)
                rows.append([f"{int(radius)}m", sphere, spheroid, spheroid - sphere])
            print(table(["radius", "sphere", "WGS84 spheroid", "delta"], rows), "\n")

    for s in stores:
        s.close()


if __name__ == "__main__":
    main()
