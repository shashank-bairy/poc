"""Minimal geohash implementation, for visualising what Redis actually indexes.

Redis stores a 52-bit interleaving of latitude and longitude bits as the score
of a sorted set -- that is a geohash. `GEOSEARCH` picks a bit-depth whose cell
is at least as wide as the query radius, then scans that cell plus its eight
neighbours and distance-filters the result. Reproducing that here makes the
cell shape visible next to H3 hexagons and S2 squares.

This module is for plotting only; nothing in the query path depends on it.
"""

from __future__ import annotations

import math

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def encode(lat: float, lng: float, precision: int = 7) -> str:
    lat_range, lng_range = [-90.0, 90.0], [-180.0, 180.0]
    out, bit, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = sum(lng_range) / 2
            if lng > mid:
                ch = (ch << 1) | 1
                lng_range[0] = mid
            else:
                ch <<= 1
                lng_range[1] = mid
        else:
            mid = sum(lat_range) / 2
            if lat > mid:
                ch = (ch << 1) | 1
                lat_range[0] = mid
            else:
                ch <<= 1
                lat_range[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def bbox(geohash: str) -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lng_min, lng_max) of a geohash cell."""
    lat_range, lng_range = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for char in geohash:
        val = BASE32.index(char)
        for mask in (16, 8, 4, 2, 1):
            target = lng_range if even else lat_range
            mid = sum(target) / 2
            if val & mask:
                target[0] = mid
            else:
                target[1] = mid
            even = not even
    return lat_range[0], lat_range[1], lng_range[0], lng_range[1]


def precision_for_radius(lat: float, radius_m: float) -> int:
    """Coarsest precision whose cell is still at least as wide as the radius.

    Same rule Redis applies when choosing how many geohash bits to scan.
    """
    for precision in range(12, 0, -1):
        lat_min, lat_max, lng_min, lng_max = bbox(encode(lat, 0.0, precision))
        height_m = (lat_max - lat_min) * 111_320
        width_m = (lng_max - lng_min) * 111_320 * math.cos(math.radians(lat))
        if min(height_m, width_m) >= radius_m:
            return precision
    return 1


def cells_for_disc(lat: float, lng: float, radius_m: float) -> list[str]:
    """The center cell plus its eight neighbours -- Redis' 3x3 scan area."""
    precision = precision_for_radius(lat, radius_m)
    center = encode(lat, lng, precision)
    lat_min, lat_max, lng_min, lng_max = bbox(center)
    dlat, dlng = lat_max - lat_min, lng_max - lng_min
    clat, clng = (lat_min + lat_max) / 2, (lng_min + lng_max) / 2
    return sorted(
        {
            encode(clat + i * dlat, clng + j * dlng, precision)
            for i in (-1, 0, 1)
            for j in (-1, 0, 1)
        }
    )
