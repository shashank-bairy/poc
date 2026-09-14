"""Shared config, dataset loading, and geo math for the POC.

Everything here is store-agnostic. Each store module (postgres_geo, redis_geo,
aerospike_geo) implements the same four-method interface defined by `GeoStore`
so compare.py can drive them uniformly.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from typing import Iterable, Protocol

# Bangalore city center. All generated points cluster around this.
CENTER_LAT = 12.9716
CENTER_LNG = 77.5946

POINTS_CSV = os.path.join(os.path.dirname(__file__), "points.csv")

# H3 resolution 8 -> ~0.46 km^2 hexagons (edge ~461m). Good middle ground for
# radii between 100m and 10km. Resolution 9 would be ~174m edge.
H3_RES = 8

# S2 level 13 -> ~1.27 km^2 cells, roughly comparable to H3 res 8.
S2_MIN_LEVEL = 11
S2_MAX_LEVEL = 14
S2_MAX_CELLS = 32

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class Point:
    id: int
    lat: float
    lng: float


@dataclass(frozen=True)
class Hit:
    """One query result: a point plus its true distance from the query center."""

    id: int
    lat: float
    lng: float
    distance_m: float


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters. Used as ground-truth post-filter."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def load_points(path: str = POINTS_CSV) -> list[Point]:
    with open(path, newline="") as fh:
        return [
            Point(int(row["id"]), float(row["lat"]), float(row["lng"]))
            for row in csv.DictReader(fh)
        ]


def brute_force_radius(points: Iterable[Point], lat: float, lng: float, radius_m: float) -> list[Hit]:
    """Exact answer by scanning every point. The precision baseline."""
    hits = []
    for p in points:
        d = haversine_m(lat, lng, p.lat, p.lng)
        if d <= radius_m:
            hits.append(Hit(p.id, p.lat, p.lng, d))
    hits.sort(key=lambda h: h.distance_m)
    return hits


def brute_force_knn(points: Iterable[Point], lat: float, lng: float, k: int) -> list[Hit]:
    hits = [Hit(p.id, p.lat, p.lng, haversine_m(lat, lng, p.lat, p.lng)) for p in points]
    hits.sort(key=lambda h: h.distance_m)
    return hits[:k]


class GeoStore(Protocol):
    """The uniform interface every store module implements."""

    name: str

    def load_data(self, points: list[Point]) -> dict: ...
    def radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]: ...
    def knn_query(self, lat: float, lng: float, k: int) -> list[Hit]: ...
    def h3_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]: ...
    def s2_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]: ...
    def close(self) -> None: ...
