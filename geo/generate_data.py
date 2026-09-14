"""Generate the shared dataset every store loads from.

Points are drawn in clusters around a few "hotspots" near the city center plus
a uniform scatter, so queries at different radii return meaningfully different
counts instead of a flat uniform density.
"""

from __future__ import annotations

import argparse
import csv
import math
import random

from common import CENTER_LAT, CENTER_LNG, POINTS_CSV

# Degrees per meter at the equator; longitude gets scaled by cos(lat).
DEG_PER_M = 1.0 / 111_320.0


def offset(lat: float, lng: float, north_m: float, east_m: float) -> tuple[float, float]:
    new_lat = lat + north_m * DEG_PER_M
    new_lng = lng + east_m * DEG_PER_M / math.cos(math.radians(lat))
    return new_lat, new_lng


def generate(n: int, seed: int, spread_m: float) -> list[tuple[int, float, float]]:
    rng = random.Random(seed)

    # A handful of dense hotspots scattered within the overall spread.
    hotspots = []
    for _ in range(8):
        hotspots.append(
            offset(
                CENTER_LAT,
                CENTER_LNG,
                rng.uniform(-spread_m, spread_m),
                rng.uniform(-spread_m, spread_m),
            )
        )

    rows = []
    for i in range(n):
        if rng.random() < 0.7:
            # Clustered: gaussian around a random hotspot, sigma ~800m.
            hlat, hlng = rng.choice(hotspots)
            lat, lng = offset(hlat, hlng, rng.gauss(0, 800), rng.gauss(0, 800))
        else:
            # Uniform scatter over the whole area.
            lat, lng = offset(
                CENTER_LAT, CENTER_LNG, rng.uniform(-spread_m, spread_m), rng.uniform(-spread_m, spread_m)
            )
        rows.append((i, round(lat, 7), round(lng, 7)))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--count", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--spread-m", type=float, default=15_000, help="half-width of the area, in meters")
    ap.add_argument("-o", "--out", default=POINTS_CSV)
    args = ap.parse_args()

    rows = generate(args.count, args.seed, args.spread_m)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "lat", "lng"])
        w.writerows(rows)
    print(f"wrote {len(rows)} points -> {args.out}")


if __name__ == "__main__":
    main()
