"""Elasticsearch store.

Elasticsearch indexes `geo_point` with a BKD tree (the same block k-d tree it
uses for numerics and dates), not a geohash sorted set and not an R-tree. A
`geo_distance` query walks that tree, then distance-filters the survivors.

The H3 and S2 layers use no geo features at all:
  - H3 -> a `keyword` field, queried with one `terms` clause holding the whole
          k-ring. Same shape as Postgres' `= ANY(cells)`.
  - S2 -> a `long` field, queried with a `bool.should` of `range` clauses, one
          per covering range. This is the interesting one: unlike Aerospike,
          Elasticsearch happily takes many ranges in a *single* request, so S2
          keeps its one-round-trip property here.

Result sets here run to thousands of documents, which is past the default
`from`/`size` window, so every read pages with `search_after` -- the same thing
you would do in production and the reason no `max_result_window` override is
needed.
"""

from __future__ import annotations

import time

from elasticsearch import Elasticsearch, helpers

import h3_layer
import s2_layer
from common import Hit, Point, haversine_m

ES_URL = "http://localhost:9201"
INDEX = "geo_points"

MAPPING = {
    "properties": {
        "id": {"type": "long"},
        "lat": {"type": "double"},
        "lng": {"type": "double"},
        # The native spatial field: BKD-tree indexed.
        "location": {"type": "geo_point"},
        # Deliberately NOT spatial: an ordinary keyword and an ordinary long.
        "h3_cell": {"type": "keyword"},
        "s2_cell": {"type": "long"},
    }
}

SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    # Bulk-load idiom: stop building searchable segments while writing, then
    # refresh once at the end. Leaving this at the 1s default roughly doubles
    # the load time here for no benefit -- nothing queries mid-load.
    "refresh_interval": "-1",
}

PAGE = 5_000


class ElasticStore:
    name = "elastic"
    knn_impl = "sort by _geo_distance (scan + sort)"

    def __init__(self, url: str = ES_URL):
        self.es = Elasticsearch(url, request_timeout=120)
        # Fail fast at construction, like the other stores' connect calls do,
        # so api.py can record the store as unavailable instead of erroring per
        # request.
        self.es.info()

    def load_data(self, points: list[Point]) -> dict:
        if self.es.indices.exists(index=INDEX):
            self.es.indices.delete(index=INDEX)
        self.es.indices.create(index=INDEX, mappings=MAPPING, settings=SETTINGS)

        def docs():
            for p in points:
                yield {
                    "_index": INDEX,
                    "_id": str(p.id),
                    "_source": {
                        "id": p.id,
                        "lat": p.lat,
                        "lng": p.lng,
                        # geo_point takes lon/lat in this object form.
                        "location": {"lat": p.lat, "lon": p.lng},
                        "h3_cell": h3_layer.cell_for_point(p.lat, p.lng),
                        "s2_cell": s2_layer.to_signed(s2_layer.leaf_cell_id(p.lat, p.lng)),
                    },
                }

        t0 = time.perf_counter()
        helpers.bulk(self.es, docs(), chunk_size=2000, refresh=False)
        insert_s = time.perf_counter() - t0

        # There is no "CREATE INDEX" step -- segments were written during the
        # bulk. What is left is making them searchable and merging them down,
        # which is the closest equivalent and the thing that actually costs time.
        t0 = time.perf_counter()
        self.es.indices.refresh(index=INDEX)
        refresh_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        self.es.indices.forcemerge(index=INDEX, max_num_segments=1)
        merge_s = time.perf_counter() - t0

        self.es.indices.put_settings(index=INDEX, settings={"refresh_interval": "1s"})

        return {
            "rows": len(points),
            "insert_s": insert_s,
            "index_s": {"refresh": refresh_s, "forcemerge": merge_s},
        }

    # --- paging helper ---

    def _scan(self, query: dict) -> list[tuple]:
        """Every match, paged with search_after.

        `_source` is switched off and the three fields come from doc values
        instead: cheaper than reading and parsing the stored JSON for thousands
        of hits, and the only thing this POC needs back is (id, lat, lng).
        """
        out: list[tuple] = []
        after = None
        while True:
            kwargs = dict(
                index=INDEX,
                query=query,
                size=PAGE,
                sort=[{"id": "asc"}],
                source=False,
                docvalue_fields=["id", "lat", "lng"],
                track_total_hits=False,
            )
            if after is not None:
                kwargs["search_after"] = after
            hits = self.es.search(**kwargs)["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                f = h["fields"]
                out.append((int(f["id"][0]), float(f["lat"][0]), float(f["lng"][0])))
            if len(hits) < PAGE:
                break
            after = hits[-1]["sort"]
        return out

    @staticmethod
    def _to_hits(rows, lat: float, lng: float) -> list[Hit]:
        """(id, lat, lng) rows -> Hits sorted by distance. No filtering."""
        hits = [Hit(pid, plat, plng, haversine_m(lat, lng, plat, plng)) for pid, plat, plng in rows]
        hits.sort(key=lambda h: h.distance_m)
        return hits

    # --- native geo_point (BKD tree) ---

    def _geo_query(self, lat: float, lng: float, radius_m: float, distance_type: str = "arc") -> dict:
        return {
            "geo_distance": {
                "distance": f"{radius_m}m",
                "distance_type": distance_type,
                "location": {"lat": lat, "lon": lng},
            }
        }

    def radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        """Whatever the BKD tree says is inside, with distances recomputed.

        Deliberately *not* post-filtered with our own Haversine: the point of
        the native row is to show what Elasticsearch itself considers inside the
        circle, the same way the Redis row does. Any disagreement with ground
        truth is a real finding, not something to paper over.
        """
        rows = self._scan(self._geo_query(lat, lng, radius_m))
        return self._to_hits(rows, lat, lng)

    def knn_query(self, lat: float, lng: float, k: int) -> list[Hit]:
        """Exact nearest-k, but not an index traversal.

        `sort: _geo_distance` computes the distance for every matching document
        and sorts -- with `match_all` that is all 20,000. The answer is exact;
        the BKD tree contributes nothing. (Elasticsearch's `knn` section is for
        `dense_vector` similarity, not geo.) A production version would bound it
        with a `geo_distance` filter first, which is the expanding-radius trick
        Redis and Aerospike are forced into anyway.
        """
        resp = self.es.search(
            index=INDEX,
            query={"match_all": {}},
            size=k,
            sort=[
                {
                    "_geo_distance": {
                        "location": {"lat": lat, "lon": lng},
                        "order": "asc",
                        "unit": "m",
                        "distance_type": "arc",
                    }
                }
            ],
            source=False,
            docvalue_fields=["id", "lat", "lng"],
            track_total_hits=False,
        )
        rows = [
            (int(h["fields"]["id"][0]), float(h["fields"]["lat"][0]), float(h["fields"]["lng"][0]))
            for h in resp["hits"]["hits"]
        ]
        # Recompute distance with the shared Haversine so the number is
        # comparable to every other store's.
        return self._to_hits(rows, lat, lng)[:k]

    # --- cell schemes: no geo field involved ---

    def h3_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        """Raw candidate rows from the cell lookup, before the distance filter."""
        cells = h3_layer.cells_for_disc(lat, lng, radius_m)
        # One `terms` clause carries the whole k-ring. The default ceiling is
        # 65,536 terms, far above the 547 a 10 km disc needs.
        return self._scan({"terms": {"h3_cell": cells}})

    def h3_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return h3_layer.post_filter(self.h3_rows(lat, lng, radius_m), lat, lng, radius_m)

    def s2_rows(self, lat: float, lng: float, radius_m: float) -> list[tuple]:
        ranges = s2_layer.ranges_for_disc(lat, lng, radius_m)
        should = [
            {
                "range": {
                    "s2_cell": {
                        "gte": s2_layer.to_signed(lo),
                        "lte": s2_layer.to_signed(hi),
                    }
                }
            }
            for lo, hi in ranges
        ]
        # All 32 ranges in one request -- the property Aerospike lacks.
        return self._scan({"bool": {"should": should, "minimum_should_match": 1}})

    def s2_radius_query(self, lat: float, lng: float, radius_m: float) -> list[Hit]:
        return s2_layer.post_filter(self.s2_rows(lat, lng, radius_m), lat, lng, radius_m)

    # --- earth-model footnote, Elasticsearch's version ---

    def plane_radius_count(self, lat: float, lng: float, radius_m: float) -> int:
        """Same query with `distance_type: plane`.

        Elasticsearch offers a flat-earth approximation that skips the
        trigonometry. Faster, and wrong by a growing margin as the radius grows
        -- the same class of disagreement as PostGIS sphere vs spheroid, but
        chosen by a query parameter rather than a column type.
        """
        return self.es.count(
            index=INDEX, query=self._geo_query(lat, lng, radius_m, distance_type="plane")
        )["count"]

    def index_size_bytes(self) -> int:
        stats = self.es.indices.stats(index=INDEX, metric="store")
        return stats["indices"][INDEX]["primaries"]["store"]["size_in_bytes"]

    def close(self) -> None:
        self.es.close()


if __name__ == "__main__":
    from common import CENTER_LAT, CENTER_LNG, load_points

    store = ElasticStore()
    print(store.load_data(load_points()))
    for r in (100, 1000, 10000):
        print(
            r,
            "native", len(store.radius_query(CENTER_LAT, CENTER_LNG, r)),
            "h3", len(store.h3_radius_query(CENTER_LAT, CENTER_LNG, r)),
            "s2", len(store.s2_radius_query(CENTER_LAT, CENTER_LNG, r)),
            "plane", store.plane_radius_count(CENTER_LAT, CENTER_LNG, r),
        )
    print("knn5", [h.id for h in store.knn_query(CENTER_LAT, CENTER_LNG, 5)])
    print("index bytes", store.index_size_bytes())
    store.close()
