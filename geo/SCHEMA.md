# Schema reference

Exactly how the same point is stored in each database, and in each of the three
storage layouts. Every value below was read back out of the running stores — none
of it is illustrative.

**The point used throughout:**

```
id   = 18427
lat  = 12.9716343
lng  = 77.593828        (a few metres from the query centre in the POC)
```

**The three cell IDs derived from it:**

| Scheme | Value | Type |
|---|---|---|
| H3, resolution 8 | `8860145b49fffff` | 15-char hex string (also a uint64) |
| S2, level 30 leaf | `4300399395143733155` | uint64 |
| S2, shifted for signed columns | `-4922972641711042653` | int64 |
| S2, lexicographic form | `3bae167723d8cfa3` | 16-char zero-padded hex |
| Geohash (what Redis encodes internally) | `tdr1v9qj5pz` | base32 string |

Two encoding notes that matter:

- **S2 IDs are unsigned 64-bit**, and cells on cube faces 4–5 exceed
  2<sup>63</sup>. Most databases only have *signed* 64-bit integers, so
  `s2_layer.to_signed()` subtracts 2<sup>63</sup>. That's order-preserving, which
  is the only property the range queries need.
- **Redis sorted-set scores are IEEE doubles**, which carry 53 bits of integer
  precision — not enough for a 64-bit cell ID. So the S2 ID goes in the *member
  string* as zero-padded hex, where lexicographic order matches numeric order,
  and the score is left at 0. That's what makes `ZRANGEBYLEX` work as a range
  scan.

---

## Postgres + PostGIS

One table holds all three layouts side by side: a PostGIS `geography` column for
the native index, and two perfectly ordinary columns for the cell IDs.

### DDL

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE points (
    id      integer PRIMARY KEY,
    lat     double precision NOT NULL,
    lng     double precision NOT NULL,
    geog    geography(Point, 4326) NOT NULL,   -- native spatial
    h3_cell text   NOT NULL,                   -- plain string
    s2_cell bigint NOT NULL                    -- plain integer, shifted
);

-- The spatial index: a GiST R-tree.
CREATE INDEX points_geog_gist ON points USING GIST (geog);

-- Deliberately NOT spatial. Ordinary btrees over the cell IDs.
CREATE INDEX points_h3_btree  ON points USING BTREE (h3_cell);
CREATE INDEX points_s2_btree  ON points USING BTREE (s2_cell);
```

### One row

| id | lat | lng | geog | h3_cell | s2_cell |
|---|---|---|---|---|---|
| 18427 | 12.9716343 | 77.593828 | `SRID=4326;POINT(77.593828 12.9716343)` | `8860145b49fffff` | `-4922972641711042653` |

Note the coordinate order inside `geog`: PostGIS takes **(longitude, latitude)**,
x before y. Getting this backwards puts your data in the Indian Ocean and is
probably the single most common PostGIS mistake.

### Insert

```sql
INSERT INTO points (id, lat, lng, geog, h3_cell, s2_cell)
VALUES (
    18427, 12.9716343, 77.593828,
    ST_SetSRID(ST_MakePoint(77.593828, 12.9716343), 4326)::geography,
    '8860145b49fffff',
    -4922972641711042653
);
```

### The three queries

```sql
-- 1. Native: GiST R-tree. `false` = use a sphere, not the WGS84 spheroid.
SELECT id, lat, lng, ST_Distance(geog, :pt, false) AS d
FROM points
WHERE ST_DWithin(geog, :pt, :radius_m, false)
ORDER BY d;

-- 2. H3: all 547 cells as one predicate against a plain btree.
SELECT id, lat, lng FROM points WHERE h3_cell = ANY(:cells);

-- 3. S2: the covering as (lo, hi) pairs, joined as range predicates.
SELECT p.id, p.lat, p.lng
FROM points p
JOIN unnest(:los::bigint[], :his::bigint[]) AS r(lo, hi)
  ON p.s2_cell BETWEEN r.lo AND r.hi;

-- Bonus: nearest-neighbour, which only Postgres has natively.
SELECT id FROM points ORDER BY geog <-> :pt LIMIT 10;
```

Both cell queries are followed by a Haversine distance filter in Python, because
cells over-cover. `:pt` is
`ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography`.

---

## Redis

Redis has no tables. Each layout is a different key and a different data type.

### Key map

| Key | Type | Holds | Used by |
|---|---|---|---|
| `geo:points` | sorted set | member = point ID, score = 52-bit geohash | native `GEOSEARCH` |
| `geo:coords` | hash | field = point ID, value = `"lat,lng"` | coordinate lookup for both cell schemes |
| `geo:h3:<cell>` | set (one key per cell) | point IDs in that hexagon | H3 |
| `geo:s2` | sorted set | member = `"<hex cell id>:<point id>"`, score = 0 | S2 |

### Writing the point

```redis
GEOADD geo:points 77.593828 12.9716343 18427
HSET   geo:coords 18427 "12.9716343,77.593828"
SADD   geo:h3:8860145b49fffff 18427
ZADD   geo:s2 0 "3bae167723d8cfa3:18427"
```

### What's actually stored

```redis
127.0.0.1:6380> ZSCORE geo:points 18427
"3574463098274502"                      # the geohash, as a double

127.0.0.1:6380> GEOHASH geo:points 18427
1) "tdr1v9qj5p0"                        # the same thing, base32

127.0.0.1:6380> HGET geo:coords 18427
"12.9716343,77.593828"

127.0.0.1:6380> SMEMBERS geo:h3:8860145b49fffff
1) "4345"  2) "7121"  3) "7918"  4) "17304"  5) "17417"  6) "18427"

127.0.0.1:6380> ZRANGEBYLEX geo:s2 "[3bae167723d8" "[3bae167723d9"
1) "3bae167723d8cfa3:18427"
```

That `ZSCORE` is the whole geohash idea in one line: a single number that encodes
both coordinates, with nearby places getting nearby numbers.

### The three queries

```redis
# 1. Native.
GEOSEARCH geo:points FROMLONLAT 77.5946 12.9716 BYRADIUS 1000 m ASC WITHCOORD WITHDIST

# 2. H3 — one command for the entire k-ring, however many cells it has.
SUNION geo:h3:<cell1> geo:h3:<cell2> ... geo:h3:<cell547>
HMGET  geo:coords <id> <id> ...

# 3. S2 — one ZRANGEBYLEX per covering range, pipelined into one round trip.
ZRANGEBYLEX geo:s2 [<lo hex> [<hi hex>\xff
```

The upper bound has to reach past the last cell's own points: `redis_geo.py`
appends `\xff` to it (`f"[{hi_hex}:\xff"`), since every member is
`"<hex>:<id>"` and a bare `[<hi hex>` would stop just short of them. The
`redis-cli` example above sidesteps quoting by using the next prefix instead.

Redis has **no KNN command**. "Nearest 10" is a client-side loop: search 200 m,
then 400 m, then 800 m, until 10 results appear, then sort.

---

## Aerospike

Two sets in the `test` namespace, because H3 and S2 need opposite layouts.

### Set 1 — `points`, keyed by point ID

Used by the native geo index and by S2.

| Bin | Type | Value |
|---|---|---|
| `id` | integer | `18427` |
| `lat` | double | `12.9716343` |
| `lng` | double | `77.593828` |
| `loc` | GeoJSON | `{"type": "Point", "coordinates": [77.593828, 12.9716343]}` |
| `s2` | integer | `-4922972641711042653` |

```python
client.put(
    ("test", "points", 18427),
    {
        "id": 18427,
        "lat": 12.9716343,
        "lng": 77.593828,
        # GeoJSON coordinates are [longitude, latitude], same trap as PostGIS.
        "loc": aerospike.GeoJSON({"type": "Point", "coordinates": [77.593828, 12.9716343]}),
        "s2": -4922972641711042653,
    },
)

client.index_single_value_create("test", "points", "loc", aerospike.INDEX_GEO2DSPHERE, "pt_loc_geo")
client.index_single_value_create("test", "points", "s2",  aerospike.INDEX_NUMERIC,      "pt_s2_int")
```

### Set 2 — `h3idx`, keyed by the H3 cell ID

This is the inverted layout, and the reason Aerospike is fastest in the POC. The
**cell ID is the primary key**, and the record carries the points inside it.

| Key | Bin `pts` |
|---|---|
| `8860145b49fffff` | `[[4345, 12.974537, 77.5945127], [7121, 12.973672, 77.5927779], [7918, 12.9742494, 77.5951932], …]` |

```python
client.put(
    ("test", "h3idx", "8860145b49fffff"),
    {"pts": [[4345, 12.974537, 77.5945127], [7121, 12.973672, 77.5927779], ...]},
)
```

No secondary index on this set at all — primary key only.

For the POC's 20,000 points at resolution 8 that's **1,254 cell records**, the
largest holding **319 points**.

### The three queries

```python
from aerospike import predicates as p

# 1. Native: GEO2DSPHERE secondary index.
q = client.query("test", "points")
q.where(p.geo_within_radius("loc", 77.5946, 12.9716, 1000.0))
rows = q.results()

# 2. H3: every cell in the k-ring as a key, one call.
keys = [("test", "h3idx", cell) for cell in k_ring]      # 547 keys
batch = client.batch_read(keys, ["pts"])                 # ONE round trip
for record in batch.batch_records:
    if record.result == 0 and record.record:             # 0 = found
        rows.extend(record.record[2]["pts"])

# 3. S2: one query per covering range. Not batchable -- a range is not a key.
for lo, hi in ranges:                                    # 32 queries
    q = client.query("test", "points")
    q.where(p.between("s2", to_signed(lo), to_signed(hi)))
    rows.extend(q.results())
```

**The constraint driving all of this:** an Aerospike secondary-index query takes
exactly **one** predicate. There is no `IN` and no `OR`. So a 547-cell k-ring
expressed as a secondary-index query on an `h3` bin costs 547 round trips
(measured: 676 ms). Expressed as 547 primary keys in one `batch_read`: 15 ms.
Same hexagons, same answers.

S2 can't take that escape route — its coverings are ranges, not enumerable keys
— so it stays on the secondary index and runs one query per range.

Aerospike also has **no KNN**, same expanding-radius loop as Redis.

---

## Elasticsearch

One index, three layouts again: a `geo_point` field for the native search, and
two perfectly ordinary fields — a `keyword` and a `long` — for the cell IDs.

### Mapping

```json
PUT /geo_points
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "-1"
  },
  "mappings": {
    "properties": {
      "id":       { "type": "long" },
      "lat":      { "type": "double" },
      "lng":      { "type": "double" },
      "location": { "type": "geo_point" },
      "h3_cell":  { "type": "keyword" },
      "s2_cell":  { "type": "long"    }
    }
  }
}
```

`refresh_interval: -1` is a bulk-load setting, not a permanent one. New documents
land in new segments and stay invisible to search until a refresh; switching it
off means the loader is not building searchable segments every second while
nothing is querying. The loader refreshes once at the end, force-merges to a
single segment, and puts the interval back to `1s`.

There is no `CREATE INDEX` step — the BKD tree is written as part of each
segment. What the load table calls "index build" is refresh plus force-merge.

### One document

```json
GET /geo_points/_doc/18427

{
  "id": 18427,
  "lat": 12.9716343,
  "lng": 77.593828,
  "location": { "lat": 12.9716343, "lon": 77.593828 },
  "h3_cell": "8860145b49fffff",
  "s2_cell": -4922972641711042653
}
```

Note `location` uses **`lon`**, not `lng` — Elasticsearch's geo formats are a
well-known foot-gun. The object form above is unambiguous; the array form
`[77.593828, 12.9716343]` is **[lon, lat]**, GeoJSON order, the reverse of the
string form `"12.9716343,77.593828"` which is **"lat,lon"**. Three formats, two
orderings, silently accepted either way.

`s2_cell` is the signed-shifted value: Elasticsearch `long` is signed 64-bit,
same constraint as a Postgres `bigint`.

### The three queries

```json
// 1. Native: geo_distance over the BKD tree.
//    distance_type defaults to "arc" (Haversine); "plane" is the cheap
//    flat-earth approximation.
{ "geo_distance": {
    "distance": "1000m",
    "distance_type": "arc",
    "location": { "lat": 12.9716, "lon": 77.5946 }
}}

// 2. H3: one terms clause carries the whole k-ring. Ceiling is 65,536 terms.
{ "terms": { "h3_cell": ["8860145b49fffff", "..."] } }   // 547 values

// 3. S2: one bool.should of range clauses -- all 32 ranges in ONE request.
{ "bool": {
    "should": [
      { "range": { "s2_cell": { "gte": -4922972641711042653,
                                "lte": -4922972641708945408 } } }
      // ... 31 more
    ],
    "minimum_should_match": 1
}}
```

**Reading the results back is the part that costs.** A 10 km disc matches 7,438
documents, past the 10,000-document `from`/`size` window and well past the point
where `from` is sane. Every read here pages with `search_after`:

```python
body = {
    "query": query,
    "size": 5000,
    "sort": [{"id": "asc"}],       # a total order, so paging is stable
    "_source": False,              # do not read or parse the stored JSON
    "docvalue_fields": ["id", "lat", "lng"],   # read the columnar copy instead
    "track_total_hits": False,     # do not count what we are about to fetch
}
# then: body["search_after"] = last_hit["sort"], repeat until a short page
```

`_source: false` plus `docvalue_fields` is the meaningful optimisation: doc
values are a columnar, on-disk copy of the field written for sorting and
aggregation, and reading three numbers out of it beats decompressing and parsing
the whole `_source` JSON for thousands of hits.

**No KNN.** `sort: _geo_distance` gives an exact answer by computing the
distance for every matching document — a scan, not an index walk. Elasticsearch's
`knn` query is for `dense_vector` similarity and does not apply to `geo_point`.

---

## Side by side

| | Postgres | Redis | Aerospike | Elasticsearch |
|---|---|---|---|---|
| **Native geo** | `geography` + GiST R-tree | sorted set, geohash as score | GeoJSON bin + GEO2DSPHERE index | `geo_point` + BKD tree |
| **H3 stored as** | `text` column, btree | one set per cell | **primary key of its own record** | `keyword` field |
| **H3 fetched by** | `= ANY(cells)`, one query | `SUNION`, one command | `batch_read`, one call | one `terms` clause |
| **S2 stored as** | `bigint` column, btree | hex prefix in a zset member | `integer` bin, numeric index | `long` field |
| **S2 fetched by** | join on `BETWEEN`, one query | pipelined `ZRANGEBYLEX` | one query per range | one `bool.should` of ranges |
| **Native KNN** | yes, `<->` | no | no | no (exact, but a scan-and-sort) |
| **Signed 64-bit issue** | shift needed | avoided via hex strings | shift needed | shift needed |
| **Reading many rows** | cursor, binary protocol | pipelined, binary protocol | `batch_read`, binary protocol | `search_after` paging, JSON over HTTP |

The H3 row is the interesting one: same scheme, four completely different
physical layouts, each chosen to fit how that database wants to be read.

The last row is the one that explains Elasticsearch's timings — see lesson 6 in
the README.

---

## Regenerating any of this

```bash
uv run postgres_geo.py     # rebuilds the table and prints load stats
uv run redis_geo.py        # rebuilds all four Redis keys
uv run aerospike_geo.py    # rebuilds both sets and the indexes
uv run elastic_geo.py      # recreates the index, bulk-loads, force-merges
```

Inspecting by hand:

```bash
docker exec -it geo-postgis psql -U postgres -d geo -c '\d points'
docker exec -it geo-redis redis-cli TYPE geo:points
docker exec -it geo-aerospike aql -c "select * from test.points where PK = 18427"
```
