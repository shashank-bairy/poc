# Geospatial Indexing POC — Redis, Aerospike, Postgres/PostGIS, Elasticsearch, H3, S2

## Goal

Understand six approaches to geospatial querying, grouped by what they actually are:

- **Native geo indexes** — systems with spatial querying built in:
  - Redis (geohash encoded into a sorted set)
  - Aerospike (geohash-based secondary index)
  - Postgres/PostGIS (R-tree via GiST)
  - Elasticsearch (BKD tree over `geo_point`)
- **Pure indexing schemes** — no storage of their own, just cell-ID encodings layered on top of any database:
  - H3 (Uber, hexagonal grid)
  - S2 (Google, quadtree/square grid)

## Background: how each system indexes space

| System | Indexing approach | Notes |
|---|---|---|
| **Redis** | Sorted set with geohash encoded as the score | `GEOADD`, `GEOSEARCH`. Fast, in-memory. Approximate at geohash cell edges. |
| **Aerospike** | Geohash-based secondary index (regions/cells) | `GeoJSON` bin + geo index. Good for radius/polygon "points-in-region." No native KNN — approximate with expanding radius queries. |
| **Postgres (PostGIS)** | GiST R-tree over `geometry`/`geography` | The reference implementation. True radius, KNN (`<->` operator), polygon containment, spatial joins. |
| **Elasticsearch** | BKD tree (block k-d tree) over `geo_point` | The same structure it indexes numbers and dates with. Radius, bounding box, polygon, region containment — all native. No KNN: `sort: _geo_distance` is exact but a scan. Immutable segments, so writes are visible only after a refresh. |
| **H3** | Hierarchical hexagonal grid, cell ID per point | Hexagons give uniform neighbor distance — good for ring/buffer-style queries. |
| **S2** | Hierarchical quadtree on cube faces, cell ID per point | Squares subdivide recursively — good for hierarchical containment and covering large/irregular regions. |

The core idea behind H3/S2: cells near each other in space tend to have IDs that sort near each other, so spatial queries reduce to plain equality/range lookups on an integer — no special spatial index required.

## Setup (once)

```bash
mkdir geo-poc && cd geo-poc
docker network create geo-net

docker run -d --name redis --network geo-net -p 6379:6379 redis/redis-stack
docker run -d --name aerospike --network geo-net -p 3000:3000 aerospike/aerospike-server
docker run -d --name postgis --network geo-net -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgis/postgis
docker run -d --name elastic --network geo-net -p 9200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3

pip install redis aerospike psycopg2-binary elasticsearch h3 s2sphere folium haversine
```

Generate **one shared dataset once** and save it to `points.csv` (columns: `id, lat, lng`) — e.g. 20k random points clustered around a city center. Every system loads from this same file so results stay comparable.

## Build order

Do these in sequence — each step gives you the grounding to understand the next.

### Step 1 — Postgres/PostGIS (reference implementation)

- Load points into a `geography(Point)` column, `CREATE INDEX ... USING GIST`.
- Write `radius_query(lat, lng, r)` using `ST_DWithin`.
- Write `knn_query(lat, lng, k)` using the `<->` operator.
- Plot results on a map to sanity-check by eye. This is your ground truth for Steps 2–3.

### Step 2 — Redis

- `GEOADD` the same points; query with `GEOSEARCH`.
- Compare radius results against Postgres.
- Note discrepancies near geohash cell boundaries.

### Step 3 — Aerospike

- Store points as a GeoJSON bin; `create_index` with a `GEO2DSPHERE` index type.
- Radius query is native.
- KNN isn't native — implement it as "expand radius until you have k results, then sort client-side by distance."
- Compare against Postgres KNN.

### Step 4 — H3 layer (add to all four systems)

- Precompute `h3_cell = h3.latlng_to_cell(lat, lng, res)` for every point (start with resolution 7–9).
- In each system, add a plain integer/string column/field/bin for `h3_cell`, indexed normally (btree/hash — nothing spatial).
- Radius query becomes: `h3.grid_disk(center_cell, k_rings)` → union of cells → equality lookup → post-filter with Haversine distance to trim to a true radius.
- Compare latency and precision against each system's native geo query from Steps 1–3.

### Step 5 — S2 layer (repeat Step 4 with S2)

- Same pattern, using `S2RegionCoverer` to get a cell covering for a query disc instead of H3's k-ring.
- Plot S2 cell coverings (squares) vs. H3 coverings (hexagons) over the same query circle with `folium` — a good visual for the shape difference.

## What to record at each step

| Metric | Why it matters |
|---|---|
| Insert/index build time for 20k points | Cost of a native geo index vs. a plain integer index |
| Radius query latency at 3 radii (100m, 1km, 10km) | Native indexes and cell-based approaches scale differently |
| Precision (points wrongly included/excluded vs. Postgres ground truth) | Geohash and grid cells both have boundary artifacts |
| Query code complexity | KNN is one line in Postgres, manual everywhere else |
| Result-set transfer cost | Separate "find the rows" from "return the rows" — a store tuned for top-N pages looks slow when asked for thousands |

## Deliverable structure

```
geo-poc/
├── points.csv              # shared dataset, generated once
├── generate_data.py        # dataset generator
├── postgres_geo.py         # load_data(), radius_query(), knn_query(), h3_radius_query()
├── redis_geo.py            # same interface
├── aerospike_geo.py        # same interface
├── elastic_geo.py          # same interface
├── s2_layer.py             # S2 covering logic, usable against any store
└── compare.py              # runs all queries, prints results table + generates map plots
```

Keep the same four function names (`load_data`, `radius_query`, `knn_query`, `h3_radius_query`) across `postgres_geo.py`, `redis_geo.py`, `aerospike_geo.py` and `elastic_geo.py` so `compare.py` can call them uniformly and produce a single apples-to-apples results table.

## The "aha" artifact

The final comparison table (latency + precision per system per query type) plus the map plots of geohash cells vs. H3 hexagons vs. S2 squares over the same query area — that's what turns the theoretical tradeoffs into something you can actually see and reason about.
