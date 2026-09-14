"""HTTP API behind the React UI.

Wraps the three stores and the two cell layers in JSON endpoints so the browser
can drive real queries against real databases. Nothing here does spatial work of
its own -- it times the store calls and reshapes the results for the map.

Run:  uv run uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import statistics
import time
from contextlib import asynccontextmanager
from typing import Any

import h3
import s2sphere
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import geohash_layer
import h3_layer
import s2_layer
from common import (
    CENTER_LAT,
    CENTER_LNG,
    H3_RES,
    brute_force_knn,
    brute_force_radius,
    load_points,
)

RADII = [100, 250, 500, 1_000, 2_500, 5_000, 10_000]
STORE_NAMES = ["postgres", "redis", "aerospike"]
# Method name -> the store method that implements it. Stores advertise only the
# ones they actually have.
METHOD_ATTR = {
    "native": "radius_query",
    "h3": "h3_radius_query",
    "s2": "s2_radius_query",
}
METHODS = list(METHOD_ATTR)


def methods_for(store) -> list[str]:
    return [m for m, attr in METHOD_ATTR.items() if hasattr(store, attr)]

_points: list = []
_stores: dict[str, Any] = {}
_store_errors: dict[str, str] = {}


def _connect_all() -> None:
    """Connect to whatever is reachable; record failures instead of crashing."""
    from aerospike_geo import AerospikeStore
    from postgres_geo import PostgresStore
    from redis_geo import RedisStore

    for name, factory in (
        ("postgres", PostgresStore),
        ("redis", RedisStore),
        ("aerospike", AerospikeStore),
    ):
        try:
            _stores[name] = factory()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as-is
            _store_errors[name] = f"{type(exc).__name__}: {exc}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _points.extend(load_points())
    _connect_all()
    yield
    for store in _stores.values():
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="geo POC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _store(name: str):
    if name in _stores:
        return _stores[name]
    raise HTTPException(503, _store_errors.get(name, f"unknown store {name!r}"))


@app.get("/api/meta")
def meta() -> dict:
    return {
        "center": {"lat": CENTER_LAT, "lng": CENTER_LNG},
        "point_count": len(_points),
        "radii": RADII,
        "methods": METHODS,
        "h3_res": H3_RES,
        "stores": [
            {
                "name": n,
                "available": n in _stores,
                "error": _store_errors.get(n),
                "methods": methods_for(_stores[n]) if n in _stores else [],
            }
            for n in STORE_NAMES
        ],
    }


@app.get("/api/points")
def points(limit: int = Query(5000, ge=0, le=50_000)) -> dict:
    """Evenly sampled points for the background layer."""
    if limit >= len(_points) or limit == 0:
        sample = _points
    else:
        step = len(_points) / limit
        sample = [_points[int(i * step)] for i in range(limit)]
    return {
        "total": len(_points),
        "returned": len(sample),
        "points": [[p.id, p.lat, p.lng] for p in sample],
    }


@app.get("/api/query")
def query(
    store: str,
    method: str = "native",
    lat: float = CENTER_LAT,
    lng: float = CENTER_LNG,
    radius: float = Query(1000.0, gt=0),
    repeat: int = Query(3, ge=1, le=25),
) -> dict:
    """Run one radius query, timed, with precision measured against brute force."""
    s = _store(store)
    if method not in METHODS:
        raise HTTPException(400, f"method must be one of {METHODS}")

    if not hasattr(s, METHOD_ATTR[method]):
        raise HTTPException(400, f"{store} does not support method {method!r}")
    fn = getattr(s, METHOD_ATTR[method])

    samples = []
    hits = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        hits = fn(lat, lng, radius)
        samples.append((time.perf_counter() - t0) * 1000)

    # How many rows the index handed back before the distance filter ran. Only
    # the cell methods expose this; the native indexes filter internally.
    candidates = None
    probes = None
    rejected: list[list] = []
    if method != "native":
        rows_attr = {"h3": "h3_rows", "s2": "s2_rows"}[method]
        rows = getattr(s, rows_attr)(lat, lng, radius)
        candidates = len(rows)
        probes = (
            len(s2_layer.ranges_for_disc(lat, lng, radius))
            if method == "s2"
            else len(h3_layer.cells_for_disc(lat, lng, radius))
        )
        # Candidates the cells returned but the distance filter threw away --
        # the over-covering, drawn on the map in a different colour.
        kept = {h.id for h in hits}
        rejected = [[int(r[0]), r[1], r[2]] for r in rows if int(r[0]) not in kept]

    truth = {h.id for h in brute_force_radius(_points, lat, lng, radius)}
    got = {h.id for h in hits}

    return {
        "store": store,
        "method": method,
        "center": {"lat": lat, "lng": lng},
        "radius_m": radius,
        "latency_ms": round(statistics.median(samples), 2),
        "latency_min_ms": round(min(samples), 2),
        "count": len(hits),
        "truth_count": len(truth),
        "false_positives": sorted(got - truth),
        "false_negatives": sorted(truth - got),
        "candidates": candidates,
        "probes": probes,
        "hits": [
            {"id": h.id, "lat": h.lat, "lng": h.lng, "d": round(h.distance_m, 1)}
            for h in hits[:5000]
        ],
        "rejected": rejected[:8000],
        "truncated": len(hits) > 5000 or len(rejected) > 8000,
    }


@app.get("/api/knn")
def knn(
    store: str,
    lat: float = CENTER_LAT,
    lng: float = CENTER_LNG,
    k: int = Query(10, ge=1, le=500),
    repeat: int = Query(3, ge=1, le=25),
) -> dict:
    s = _store(store)
    samples, hits = [], []
    for _ in range(repeat):
        t0 = time.perf_counter()
        hits = s.knn_query(lat, lng, k)
        samples.append((time.perf_counter() - t0) * 1000)

    truth = [h.id for h in brute_force_knn(_points, lat, lng, k)]
    return {
        "store": store,
        "k": k,
        "latency_ms": round(statistics.median(samples), 2),
        "exact": [h.id for h in hits] == truth,
        "implementation": "native <-> operator" if store == "postgres" else "expanding radius loop",
        "hits": [
            {"id": h.id, "lat": h.lat, "lng": h.lng, "d": round(h.distance_m, 1)} for h in hits
        ],
    }


@app.get("/api/cells")
def cells(
    scheme: str,
    lat: float = CENTER_LAT,
    lng: float = CENTER_LNG,
    radius: float = Query(1000.0, gt=0),
) -> dict:
    """Polygons for whichever cells a scheme would scan for this disc."""
    out = []
    if scheme == "geohash":
        for cell in geohash_layer.cells_for_disc(lat, lng, radius):
            lat_min, lat_max, lng_min, lng_max = geohash_layer.bbox(cell)
            out.append(
                {
                    "id": cell,
                    "label": f"geohash {cell}",
                    "ring": [
                        [lat_min, lng_min],
                        [lat_min, lng_max],
                        [lat_max, lng_max],
                        [lat_max, lng_min],
                    ],
                }
            )
    elif scheme == "h3":
        for cell in h3_layer.cells_for_disc(lat, lng, radius):
            out.append(
                {
                    "id": cell,
                    "label": f"h3 res {H3_RES}",
                    "ring": [[la, ln] for la, ln in h3.cell_to_boundary(cell)],
                }
            )
    elif scheme == "s2":
        for cell_id in s2_layer.covering_cells(lat, lng, radius):
            cell = s2sphere.Cell(cell_id)
            ring = []
            for i in range(4):
                ll = s2sphere.LatLng.from_point(cell.get_vertex(i))
                ring.append([ll.lat().degrees, ll.lng().degrees])
            out.append({"id": str(cell_id.id()), "label": f"s2 level {cell_id.level()}", "ring": ring})
    else:
        raise HTTPException(400, "scheme must be geohash, h3 or s2")

    return {"scheme": scheme, "count": len(out), "cells": out}


@app.get("/api/compare")
def compare(
    lat: float = CENTER_LAT,
    lng: float = CENTER_LNG,
    radius: float = Query(1000.0, gt=0),
    repeat: int = Query(3, ge=1, le=25),
) -> dict:
    """Every available store x every method, for the results table."""
    truth = {h.id for h in brute_force_radius(_points, lat, lng, radius)}
    rows = []
    for name in STORE_NAMES:
        if name not in _stores:
            continue
        for method in methods_for(_stores[name]):
            r = query(store=name, method=method, lat=lat, lng=lng, radius=radius, repeat=repeat)
            rows.append(
                {
                    "store": name,
                    "method": method,
                    "latency_ms": r["latency_ms"],
                    "count": r["count"],
                    "candidates": r["candidates"],
                    "overscan": (
                        round(r["candidates"] / max(r["count"], 1), 1)
                        if r["candidates"] is not None
                        else None
                    ),
                    "probes": r["probes"],
                    "false_positives": len(r["false_positives"]),
                    "false_negatives": len(r["false_negatives"]),
                }
            )
    return {"radius_m": radius, "truth_count": len(truth), "rows": rows}
