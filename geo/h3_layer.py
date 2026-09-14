"""H3 (Uber) cell layer — a pure indexing scheme, no storage of its own.

Idea: every point gets an H3 cell ID (a plain string). A radius query becomes
"which cells cover this disc?" -> `grid_disk` -> a set of cell IDs -> ordinary
equality lookups (IN / hash index) in whatever database you happen to use.
Because the k-ring is a hexagonal superset of the disc, the store returns extra
points near the boundary; a Haversine post-filter trims them to a true circle.
"""

from __future__ import annotations

import math

import h3

from common import H3_RES, Hit, haversine_m


def cell_for_point(lat: float, lng: float, res: int = H3_RES) -> str:
    return h3.latlng_to_cell(lat, lng, res)


def cells_for_disc(lat: float, lng: float, radius_m: float, res: int = H3_RES) -> list[str]:
    """Smallest k-ring that provably covers the disc, as a list of cell IDs.

    Hex centers sit `sqrt(3) * edge` apart, and any point lies within one
    circumradius (`edge`) of its own cell center. So a point at distance r from
    the query center belongs to a cell whose center is at most `r + edge` away,
    which the k-ring reaches once `k * sqrt(3) * edge >= r + edge`.
    """
    edge_m = h3.average_hexagon_edge_length(res, unit="m")
    k = math.ceil((radius_m + edge_m) / (math.sqrt(3) * edge_m))
    # H3 cells are not perfectly regular (12 pentagons, varying edge lengths),
    # so pad by one ring rather than trusting the ideal-hexagon math exactly.
    k += 1
    return list(h3.grid_disk(cell_for_point(lat, lng, res), k))


def post_filter(rows, lat: float, lng: float, radius_m: float) -> list[Hit]:
    """Trim a cell-based candidate set to a true circle, sorted by distance.

    `rows` is an iterable of (id, lat, lng).
    """
    hits = []
    for pid, plat, plng in rows:
        d = haversine_m(lat, lng, plat, plng)
        if d <= radius_m:
            hits.append(Hit(int(pid), plat, plng, d))
    hits.sort(key=lambda h: h.distance_m)
    return hits


def disc_stats(lat: float, lng: float, radius_m: float, res: int = H3_RES) -> dict:
    cells = cells_for_disc(lat, lng, radius_m, res)
    return {"scheme": "h3", "res": res, "cells": len(cells)}
