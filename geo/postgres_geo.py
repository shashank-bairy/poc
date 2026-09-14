"""Postgres + PostGIS store — the reference implementation.

PostGIS indexes geometry with a GiST R-tree, so radius and KNN are both native
one-liners (`ST_DWithin`, `<->`). Its results are treated as ground truth for
the other stores. The same table also carries plain `h3_cell` / `s2_cell`
columns with ordinary btree indexes, so the cell-based schemes can be measured
on identical data with no spatial index involved.
"""

from __future__ import annotations

import psycopg2
from psycopg2.extras import execute_values

import h3_layer
import s2_layer
from common import Hit, Point

DSN = "host=localhost port=55432 dbname=geo user=postgres password=postgres"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
DROP TABLE IF EXISTS points;
CREATE TABLE points (
    id      integer PRIMARY KEY,
    lat     double precision NOT NULL,
    lng     double precision NOT NULL,
    geog    geography(Point, 4326) NOT NULL,
    h3_cell text NOT NULL,
    s2_cell bigint NOT NULL
);
"""

INDEXES = [
    # The spatial index: GiST R-tree over the geography column.
    "CREATE INDEX points_geog_gist ON points USING GIST (geog)",
    # Deliberately NOT spatial: ordinary indexes over the cell IDs.
    "CREATE INDEX points_h3_btree ON points USING BTREE (h3_cell)",
    "CREATE INDEX points_s2_btree ON points USING BTREE (s2_cell)",
]


class PostgresStore:
    name = "postgres"

    def __init__(self, dsn: str = DSN):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def load_data(self, points: list[Point]) -> dict:
        import time

        rows = [
            (
                p.id,
                p.lat,
                p.lng,
                p.lng,  # ST_MakePoint takes (x=lng, y=lat)
                p.lat,
                h3_layer.cell_for_point(p.lat, p.lng),
                s2_layer.to_signed(s2_layer.leaf_cell_id(p.lat, p.lng)),
            )
            for p in points
        ]

        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

            t0 = time.perf_counter()
            execute_values(
                cur,
                "INSERT INTO points (id, lat, lng, geog, h3_cell, s2_cell) VALUES %s",
                rows,
                template="(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)",
                page_size=1000,
            )
            insert_s = time.perf_counter() - t0

            timings = {}
            for stmt in INDEXES:
                t = time.perf_counter()
                cur.execute(stmt)
                timings[stmt.split()[2]] = time.perf_counter() - t
            cur.execute("ANALYZE points")

        return {"rows": len(rows), "insert_s": insert_s, "index_s": timings}

    # --- native spatial index (GiST R-tree) ---
    #
    # `false` as the last argument to ST_DWithin/ST_Distance selects the sphere
    # instead of the WGS84 spheroid. PostGIS defaults to the spheroid, which is
    # the more accurate real-world answer, but Redis, H3 and S2 all work on a
    # sphere. Left on the default, ~26 of 7461 points at a 10 km radius differ
    # purely because of the earth model, which would swamp the index-precision
    # numbers this POC is trying to measure. `spheroid_radius_count()` below
    # keeps that comparison available on its own terms.

    def radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        sql = """
            SELECT id, lat, lng, ST_Distance(geog, q.pt, false) AS d
            FROM points,
                 (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS pt) q
            WHERE ST_DWithin(geog, q.pt, %s, false)
            ORDER BY d
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (lng, lat, radius_m))
            return [Hit(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    def knn_query(self, lat: float, lng: float, k: int) -> list[Hit]:
        # `<->` on geography is an index-assisted nearest-neighbour scan.
        sql = """
            SELECT id, lat, lng, ST_Distance(geog, q.pt, false) AS d
            FROM points,
                 (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS pt) q
            ORDER BY geog <-> q.pt
            LIMIT %s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (lng, lat, k))
            return [Hit(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    # --- cell-based schemes, plain btree only ---

    def h3_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        """Raw candidate rows from the cell lookup, before the distance filter."""
        cells = h3_layer.cells_for_disc(lat, lng, radius_m)
        with self.conn.cursor() as cur:
            # One predicate covers the whole k-ring -- this is why H3 does well
            # here and badly on a store without an IN-style predicate.
            cur.execute("SELECT id, lat, lng FROM points WHERE h3_cell = ANY(%s)", (cells,))
            return cur.fetchall()

    def h3_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return h3_layer.post_filter(self.h3_rows(lat, lng, radius_m), lat, lng, radius_m)

    def s2_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        ranges = s2_layer.ranges_for_disc(lat, lng, radius_m)
        los = [s2_layer.to_signed(lo) for lo, _ in ranges]
        his = [s2_layer.to_signed(hi) for _, hi in ranges]
        sql = """
            SELECT p.id, p.lat, p.lng
            FROM points p
            JOIN unnest(%s::bigint[], %s::bigint[]) AS r(lo, hi)
              ON p.s2_cell BETWEEN r.lo AND r.hi
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (los, his))
            return cur.fetchall()

    def s2_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return s2_layer.post_filter(self.s2_rows(lat, lng, radius_m), lat, lng, radius_m)

    def spheroid_radius_count(self, lat: float, lng: float, radius_m: float) -> int:
        """Same radius query on the WGS84 spheroid, for the earth-model delta."""
        sql = """
            SELECT count(*)
            FROM points,
                 (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS pt) q
            WHERE ST_DWithin(geog, q.pt, %s, true)
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (lng, lat, radius_m))
            return cur.fetchone()[0]

    def close(self) -> None:
        self.conn.close()


if __name__ == "__main__":
    from common import CENTER_LAT, CENTER_LNG, load_points

    store = PostgresStore()
    print(store.load_data(load_points()))
    for r in (100, 1000, 10000):
        print(
            r,
            "native", len(store.radius_query(CENTER_LAT, CENTER_LNG, r)),
            "h3", len(store.h3_radius_query(CENTER_LAT, CENTER_LNG, r)),
            "s2", len(store.s2_radius_query(CENTER_LAT, CENTER_LNG, r)),
        )
    print("knn5", store.knn_query(CENTER_LAT, CENTER_LNG, 5))
    store.close()
