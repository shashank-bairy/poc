"""Redis store.

Redis' native geo support is a sorted set whose score is a 52-bit geohash
interleaving of lat/lng. `GEOSEARCH` decodes that into a set of geohash ranges,
scans them, and distance-filters. It is fast and in-memory, but it has no KNN
command: the closest thing is "search a radius, ASC, COUNT k", which is only
true KNN if the radius happens to contain k points -- so KNN here is an expanding
radius loop, same as Aerospike.

The H3 and S2 layers use no geo features at all:
  - H3  -> one plain Redis SET per cell, queried with SUNION (equality lookups).
  - S2  -> one sorted set with all scores 0 and members "<hex cell id>:<id>",
           queried with ZRANGEBYLEX. Redis scores are IEEE doubles and cannot
           hold a 64-bit cell ID exactly, so the ID goes into the *member* as
           zero-padded hex, where lexicographic order matches numeric order.
"""

from __future__ import annotations

import time

import redis

import h3_layer
import s2_layer
from common import Hit, Point, haversine_m

GEO_KEY = "geo:points"
COORD_KEY = "geo:coords"  # hash: id -> "lat,lng"
H3_KEY = "geo:h3:{cell}"  # set of ids
S2_KEY = "geo:s2"  # lex-ordered sorted set


class RedisStore:
    name = "redis"
    knn_impl = "expanding radius"

    def __init__(self, host: str = "localhost", port: int = 6380):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)

    def load_data(self, points: list[Point]) -> dict:
        self.r.flushdb()

        t0 = time.perf_counter()
        pipe = self.r.pipeline(transaction=False)
        for i, p in enumerate(points, 1):
            # GEOADD builds the geohash sorted set as it goes -- there is no
            # separate "build index" step to time, unlike Postgres.
            pipe.geoadd(GEO_KEY, (p.lng, p.lat, str(p.id)))
            pipe.hset(COORD_KEY, str(p.id), f"{p.lat},{p.lng}")
            if i % 2000 == 0:
                pipe.execute()
        pipe.execute()
        geo_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        pipe = self.r.pipeline(transaction=False)
        for i, p in enumerate(points, 1):
            pipe.sadd(H3_KEY.format(cell=h3_layer.cell_for_point(p.lat, p.lng)), str(p.id))
            cell_hex = s2_layer.to_hex(s2_layer.leaf_cell_id(p.lat, p.lng))
            pipe.zadd(S2_KEY, {f"{cell_hex}:{p.id}": 0})
            if i % 2000 == 0:
                pipe.execute()
        pipe.execute()
        cell_s = time.perf_counter() - t0

        return {"rows": len(points), "insert_s": geo_s, "index_s": {"cells": cell_s}}

    # --- native geohash sorted set ---

    def radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        res = self.r.geosearch(
            GEO_KEY,
            longitude=lng,
            latitude=lat,
            radius=radius_m,
            unit="m",
            sort="ASC",
            withdist=True,
            withcoord=True,
        )
        # Redis reports distance using its own earth radius (6372797.56 m), so
        # recompute with the shared Haversine for an apples-to-apples number.
        return [
            Hit(int(member), coord[1], coord[0], haversine_m(lat, lng, coord[1], coord[0]))
            for member, _dist, coord in res
        ]

    def knn_query(self, lat: float, lng: float, k: int) -> list[Hit]:
        """No native KNN. Expand the radius until k results appear."""
        radius = 200.0
        for _ in range(12):
            res = self.r.geosearch(
                GEO_KEY,
                longitude=lng,
                latitude=lat,
                radius=radius,
                unit="m",
                sort="ASC",
                count=k,
                withcoord=True,
            )
            if len(res) >= k:
                break
            radius *= 2
        hits = [
            Hit(int(member), coord[1], coord[0], haversine_m(lat, lng, coord[1], coord[0]))
            for member, coord in res
        ]
        hits.sort(key=lambda h: h.distance_m)
        return hits[:k]

    # --- cell schemes: no geo commands involved ---

    def h3_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        """Raw candidate rows from the cell lookup, before the distance filter."""
        cells = h3_layer.cells_for_disc(lat, lng, radius_m)
        # SUNION takes every cell key in one command, so the k-ring size costs
        # almost nothing in round trips.
        ids = self.r.sunion([H3_KEY.format(cell=c) for c in cells])
        return self._coords(ids)

    def h3_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return h3_layer.post_filter(self.h3_rows(lat, lng, radius_m), lat, lng, radius_m)

    def s2_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        ranges = s2_layer.ranges_for_disc(lat, lng, radius_m)
        pipe = self.r.pipeline(transaction=False)
        for lo, hi in ranges:
            # "[" = inclusive. The ":" suffix on the upper bound makes sure the
            # whole "<hex>:<id>" family of the top cell is included.
            pipe.zrangebylex(S2_KEY, f"[{s2_layer.to_hex(lo)}", f"[{s2_layer.to_hex(hi)}:\xff")
        ids = [m.split(":", 1)[1] for chunk in pipe.execute() for m in chunk]
        return self._coords(ids)

    def s2_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return s2_layer.post_filter(self.s2_rows(lat, lng, radius_m), lat, lng, radius_m)

    def _coords(self, ids):
        if not ids:
            return []
        ids = list(ids)
        raw = self.r.hmget(COORD_KEY, ids)
        out = []
        for pid, val in zip(ids, raw):
            lat_s, lng_s = val.split(",")
            out.append((int(pid), float(lat_s), float(lng_s)))
        return out

    def close(self) -> None:
        self.r.close()


if __name__ == "__main__":
    from common import CENTER_LAT, CENTER_LNG, load_points

    store = RedisStore()
    print(store.load_data(load_points()))
    for r in (100, 1000, 10000):
        print(
            r,
            "native", len(store.radius_query(CENTER_LAT, CENTER_LNG, r)),
            "h3", len(store.h3_radius_query(CENTER_LAT, CENTER_LNG, r)),
            "s2", len(store.s2_radius_query(CENTER_LAT, CENTER_LNG, r)),
        )
    print("knn5", [h.id for h in store.knn_query(CENTER_LAT, CENTER_LNG, 5)])
    store.close()
