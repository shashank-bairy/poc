"""S2 (Google) cell layer — the other pure indexing scheme.

Where H3 enumerates a ring of same-size cells and does equality lookups, S2
leans on its hierarchy: cell IDs are a quadtree traversal order, so every
ancestor cell corresponds to a *contiguous integer range* of its descendants.
A query disc is covered by a handful of variable-size cells, and each covering
cell becomes one `BETWEEN lo AND hi` range scan on a plain integer index.

Points are stored as their level-30 leaf cell ID. Those are unsigned 64-bit, so
`to_signed` shifts them into signed-int64 space (order-preserving) for stores
with signed integer columns, and `to_hex` gives a lexicographically-ordered
form for stores that only range-scan on strings (Redis).
"""

from __future__ import annotations

import s2sphere

from common import S2_MAX_CELLS, S2_MAX_LEVEL, S2_MIN_LEVEL, Hit, haversine_m

_OFFSET = 1 << 63
EARTH_RADIUS_M = 6_371_008.8


def leaf_cell_id(lat: float, lng: float) -> int:
    """Level-30 leaf cell ID (unsigned 64-bit) for a point."""
    ll = s2sphere.LatLng.from_degrees(lat, lng)
    return s2sphere.CellId.from_lat_lng(ll).id()


def to_signed(cell_id: int) -> int:
    """uint64 -> int64, preserving sort order (for BIGINT columns / bins)."""
    return cell_id - _OFFSET


def to_hex(cell_id: int) -> str:
    """uint64 -> zero-padded hex, preserving sort order (for lex range scans)."""
    return f"{cell_id:016x}"


def ranges_for_disc(
    lat: float,
    lng: float,
    radius_m: float,
    min_level: int = S2_MIN_LEVEL,
    max_level: int = S2_MAX_LEVEL,
    max_cells: int = S2_MAX_CELLS,
) -> list[tuple[int, int]]:
    """Cover the disc with S2 cells, return their leaf-ID ranges as (lo, hi).

    Each returned pair is inclusive and directly usable as a range predicate on
    the stored leaf cell ID.
    """
    ll = s2sphere.LatLng.from_degrees(lat, lng)
    center = ll.to_point()
    # A spherical cap of angular radius = arc length / earth radius.
    angle = s2sphere.Angle.from_radians(radius_m / EARTH_RADIUS_M)
    cap = s2sphere.Cap.from_axis_angle(center, angle)

    coverer = s2sphere.RegionCoverer()
    coverer.min_level = min_level
    coverer.max_level = max_level
    coverer.max_cells = max_cells

    return [
        (cell.range_min().id(), cell.range_max().id())
        for cell in coverer.get_covering(cap)
    ]


def covering_cells(lat: float, lng: float, radius_m: float, **kw) -> list[s2sphere.CellId]:
    """The covering itself (for plotting cell outlines)."""
    ll = s2sphere.LatLng.from_degrees(lat, lng)
    angle = s2sphere.Angle.from_radians(radius_m / EARTH_RADIUS_M)
    cap = s2sphere.Cap.from_axis_angle(ll.to_point(), angle)
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = kw.get("min_level", S2_MIN_LEVEL)
    coverer.max_level = kw.get("max_level", S2_MAX_LEVEL)
    coverer.max_cells = kw.get("max_cells", S2_MAX_CELLS)
    return coverer.get_covering(cap)


def post_filter(rows, lat: float, lng: float, radius_m: float) -> list[Hit]:
    hits = []
    for pid, plat, plng in rows:
        d = haversine_m(lat, lng, plat, plng)
        if d <= radius_m:
            hits.append(Hit(int(pid), plat, plng, d))
    hits.sort(key=lambda h: h.distance_m)
    return hits


def disc_stats(lat: float, lng: float, radius_m: float) -> dict:
    return {"scheme": "s2", "ranges": len(ranges_for_disc(lat, lng, radius_m))}
