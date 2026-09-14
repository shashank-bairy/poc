"""Aerospike store.

Aerospike's native geo support is a GEO2DSPHERE secondary index over a GeoJSON
bin. Internally it stores a set of covering cells per record, so region queries
("points within this circle / polygon") are native and fast. There is no KNN
and no ORDER BY, so nearest-neighbour is an expanding-radius loop with a
client-side sort.

The cell layers have to be laid out with Aerospike's grain, not against it.

Aerospike secondary-index queries take exactly one predicate -- there is no
`IN (...)`. Storing the H3 cell in a bin and querying that bin therefore means
one query per cell in the k-ring: 547 round trips at a 10 km radius, which
measured at 676 ms. Unusable, and not the database's fault -- that is simply
the wrong access path.

The right one is the primary key. `batch_read` fetches thousands of keys in a
single call, so H3 is stored inverted here: the cell ID *is* the record key, and
the record holds the points inside that cell. The k-ring becomes a list of keys
and the whole query is one batch call no matter how many cells it covers. Same
hexagons, same answers, 17 ms instead of 676 ms.

Tradeoff: the cell records duplicate the coordinates, so writes maintain both
copies, and a dense cell becomes a large hot record (at resolution 8 the biggest
here holds 319 points). A point that moves has to be removed from one cell
record and added to another -- work a plain bin update would have done for free.

S2 stays on the secondary index, because its coverings are *ranges* rather than
enumerable keys, and ranges are what `between(lo, hi)` is for. That makes the
two cell schemes here a fair comparison of Aerospike's two access paths.
"""

from __future__ import annotations

import time

import aerospike
from aerospike import predicates as p
from aerospike_helpers.batch.records import BatchRecords, Write
from aerospike_helpers.operations import operations as op

import h3_layer
import s2_layer
from common import Hit, Point, haversine_m

NAMESPACE = "test"
SET = "points"
# Inverted index: one record per H3 cell, keyed by the cell ID itself, holding
# the points that fall in it. Read via batch_read on the primary key.
H3_SET = "h3idx"


class AerospikeStore:
    name = "aerospike"

    def __init__(self, host: str = "127.0.0.1", port: int = 3000):
        self.client = aerospike.client({"hosts": [(host, port)]}).connect()

    def _drop_indexes(self) -> None:  # noqa: D401
        for name in ("pt_loc_geo", "pt_h3_str", "pt_s2_int"):
            try:
                self.client.index_remove(NAMESPACE, name)
            except aerospike.exception.IndexNotFound:
                pass

    def load_data(self, points: list[Point]) -> dict:
        self._drop_indexes()
        self.client.truncate(NAMESPACE, SET, 0)
        self.client.truncate(NAMESPACE, H3_SET, 0)
        time.sleep(1)  # truncate is asynchronous server-side

        t0 = time.perf_counter()
        batch = []
        for i, pt in enumerate(points, 1):
            key = (NAMESPACE, SET, pt.id)
            bins = {
                "id": pt.id,
                "lat": pt.lat,
                "lng": pt.lng,
                # GeoJSON coordinates are [longitude, latitude].
                "loc": aerospike.GeoJSON({"type": "Point", "coordinates": [pt.lng, pt.lat]}),
                # Aerospike integers are signed 64-bit; shift the unsigned S2
                # leaf ID into that range, order-preserving.
                "s2": s2_layer.to_signed(s2_layer.leaf_cell_id(pt.lat, pt.lng)),
            }
            batch.append(Write(key, [op.write(k, v) for k, v in bins.items()]))
            if i % 1000 == 0:
                self.client.batch_write(BatchRecords(batch))
                batch = []
        if batch:
            self.client.batch_write(BatchRecords(batch))
        insert_s = time.perf_counter() - t0

        # Build the H3 inverted index: group the points by cell, write one
        # record per cell keyed by the cell ID.
        t0 = time.perf_counter()
        by_cell: dict[str, list] = {}
        for pt in points:
            by_cell.setdefault(h3_layer.cell_for_point(pt.lat, pt.lng), []).append(
                [pt.id, pt.lat, pt.lng]
            )
        batch = []
        for cell, members in by_cell.items():
            batch.append(Write((NAMESPACE, H3_SET, cell), [op.write("pts", members)]))
            if len(batch) == 1000:
                self.client.batch_write(BatchRecords(batch))
                batch = []
        if batch:
            self.client.batch_write(BatchRecords(batch))
        h3idx_s = time.perf_counter() - t0

        timings = {"h3idx": h3idx_s}
        for label, bin_name, datatype, index_name in (
            ("geo", "loc", aerospike.INDEX_GEO2DSPHERE, "pt_loc_geo"),
            ("s2", "s2", aerospike.INDEX_NUMERIC, "pt_s2_int"),
        ):
            t = time.perf_counter()
            self.client.index_single_value_create(NAMESPACE, SET, bin_name, datatype, index_name)
            timings[label] = time.perf_counter() - t
        time.sleep(1)  # let the index builds settle before querying

        return {
            "rows": len(points),
            "insert_s": insert_s,
            "index_s": timings,
            "h3_cells": len(by_cell),
            "h3_max_cell": max(len(v) for v in by_cell.values()),
        }

    def _run(self, predicate) -> list[tuple[int, float, float]]:
        q = self.client.query(NAMESPACE, SET)
        q.select("id", "lat", "lng")
        q.where(predicate)
        return [(b["id"], b["lat"], b["lng"]) for _key, _meta, b in q.results()]

    # --- native GEO2DSPHERE index ---

    def radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        rows = self._run(p.geo_within_radius("loc", lng, lat, radius_m))
        hits = [Hit(i, la, ln, haversine_m(lat, lng, la, ln)) for i, la, ln in rows]
        hits.sort(key=lambda h: h.distance_m)
        return hits

    def knn_query(self, lat: float, lng: float, k: int) -> list[Hit]:
        """No native KNN: widen the radius until k results come back."""
        radius = 200.0
        hits: list[Hit] = []
        for _ in range(12):
            hits = self.radius_query(lat, lng, radius)
            if len(hits) >= k:
                break
            radius *= 2
        return hits[:k]

    # --- cell schemes: plain string / integer secondary indexes ---

    def h3_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        """Every point in the k-ring, fetched as primary keys in one call.

        `batch_read` takes all the keys at once and the server fans the request
        out internally, so a 547-cell k-ring costs one client round trip.
        """
        keys = [(NAMESPACE, H3_SET, cell) for cell in h3_layer.cells_for_disc(lat, lng, radius_m)]
        batch = self.client.batch_read(keys, ["pts"])
        rows = []
        for record in batch.batch_records:
            # result 0 = found; anything else means the cell holds no points.
            if record.result == 0 and record.record:
                rows.extend(tuple(row) for row in record.record[2]["pts"])
        return rows

    def h3_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return h3_layer.post_filter(self.h3_rows(lat, lng, radius_m), lat, lng, radius_m)

    def s2_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        rows = []
        for lo, hi in s2_layer.ranges_for_disc(lat, lng, radius_m):
            rows.extend(self._run(p.between("s2", s2_layer.to_signed(lo), s2_layer.to_signed(hi))))
        return rows

    def s2_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return s2_layer.post_filter(self.s2_rows(lat, lng, radius_m), lat, lng, radius_m)

    def close(self) -> None:
        self.client.close()


if __name__ == "__main__":
    from common import CENTER_LAT, CENTER_LNG, load_points

    store = AerospikeStore()
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
