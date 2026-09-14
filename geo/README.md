# geo — how databases answer "what's near me?"

A hands-on POC. 20,000 pretend shops scattered around Bangalore, stored in four
different databases, and one question asked twelve different ways:

> **Which shops are within 1 km of where I'm standing?**

There's a web UI where you click a spot on a map and watch each method answer,
so you can see what each one actually does instead of reading about it.

Two companion docs:

- [`SCHEMA.md`](./SCHEMA.md) — exactly how one point is stored in each database
  and each layout, with real values and the queries that read them back.
- [`geospatial-indexing-poc.md`](./geospatial-indexing-poc.md) — the original
  design brief.

---

## Part 1 — Why is this even hard?

You have a table of 20,000 shops, each with a latitude and longitude.

The obvious way to answer "what's within 1 km" is: go through all 20,000 shops,
measure the distance to each one, keep the close ones. That works. It's also
the thing you can never do in production — at 20 million shops with a thousand
people asking at once, you're doing 20 billion distance calculations a second.

So you want an **index** — a shortcut that lets the database skip most of the
data without looking at it.

**The problem: normal indexes only work on one dimension.**

If you had a list of people sorted by age and I asked for everyone aged 30–35,
that's easy: jump to 30, read forward until 35, stop. All the matches sit next
to each other because there's one number and it has an order.

Location has *two* numbers. Sort by latitude and you get shops that are at the
same height but 500 km east and west of each other. Sort by longitude, same
problem the other way. **Two nearby shops have no reason to end up next to each
other in either sorted list.** There is no obvious way to sort a map.

Everything below is a different answer to that one problem: *how do you flatten
two dimensions into one, so ordinary database machinery works again?*

---

## Part 2 — The two families of answers

### Family A: "Let the database handle it"

Some databases ship with spatial support built in. You give them coordinates,
they build a special index, you ask for a radius, you get an answer. Four of
them are compared here:

**Postgres + PostGIS — nested boxes (an "R-tree")**

Think of a table of contents. Draw a box around every shop in North Bangalore.
Draw a bigger box around several of those boxes. Keep going until one box
covers the city. Now a search is: does the big box overlap my circle? If no,
skip millions of shops instantly. If yes, check the boxes inside it. Recurse.

Most searches throw away most of the map in the first two or three steps.
This is a proper, general-purpose spatial index — it handles circles, arbitrary
polygons, "which neighbourhood is this point in", and nearest-neighbour, all
natively. It is also the most expensive of the three to build.

**Redis — a geohash score**

Redis has no spatial index. What it has is a sorted list, and a trick for
squeezing both coordinates into one number so the sorted list becomes useful.

The trick (called a **geohash**): cut the world in half vertically. West half
gets bit `0`, east gets `1`. Now cut that half in half horizontally — south `0`,
north `1`. Keep alternating, halving, writing down bits. After 52 bits you've
narrowed down to a box a few metres across, and the bits you wrote down are a
number identifying that box.

The useful property: **two places that are close usually share a long prefix of
those bits**, because the early cuts put them on the same side every time. So
close-together places get close-together numbers, and a sorted list works again.

Redis stores that number as the sort key and `GEOSEARCH` reads a block of the
list around your location.

**Aerospike — a list of covering cells per record**

Aerospike divides the world into cells too, and its geo index remembers which
cells each record falls in. A radius query becomes "find every record tagged
with one of these cells." Fast for "what's inside this region", and that's the
only shape of question it does natively.

Aerospike also has a second, very different way in — fetching records straight
by primary key, thousands at a time in a single `batch_read`. Which way in you
pick turns out to matter enormously; see Part 5.

**Elasticsearch — a k-d tree over packed coordinates**

Elasticsearch is a search engine, and its geo support is the same machinery it
uses for numbers and dates: a **BKD tree**. Take all the points, sort them,
split them into blocks along whichever axis is currently widest, recurse. The
result is a tree where each node owns a rectangle of space, much like the R-tree
above, but built bottom-up from sorted data into an immutable file rather than
maintained in place.

That immutability is the whole character of the thing. Writes go into new
segments and only become visible on a **refresh** (once a second by default),
which is why the loader here turns refresh off during the bulk and turns it back
on afterwards. The payoff is that every read is against a frozen, tightly packed
structure.

It answers radius, bounding box, polygon containment and "which region is this
point in" natively — the same breadth as PostGIS. What it does *not* do is
nearest-neighbour: sorting by `_geo_distance` computes the distance for every
matching document, so KNN here is exact but is a scan, not an index walk.

### Family B: "Do it yourself with cell IDs"

Here's the idea that makes H3 and S2 interesting: **you don't actually need a
spatial database at all.**

Chop the world into fixed cells once, up front. Number every cell. For each
shop, work out which cell it's in and store that number in a plain ordinary
column — the kind of column any database on earth can index.

Now "find shops within 1 km" becomes:

1. Work out which cells the 1 km circle touches. *(pure maths, no database)*
2. Ask the database for every shop in those cells. *(a plain lookup on a plain column)*
3. Measure the actual distance to each result and drop the ones outside the
   circle. *(a bit of arithmetic in your own code)*

Step 3 is necessary because cells are not circles. The cells covering your
circle always stick out past its edges, so you get back **more shops than you
asked for** and throw the extras away. In the UI those extras are the orange
dots — read from the database, then discarded. That waste is the whole cost of
this approach, and it's exactly what the two schemes differ on.

**H3 (Uber) — hexagons, all the same size**

The world as a honeycomb. To cover a circle you take your centre hexagon, then
the ring of 6 around it, then the ring of 12 around those, and so on until
you've covered the radius. You get a "flower" of hexagons.

Hexagons have a genuinely nice property: every neighbour is the same distance
away. (With squares, diagonal neighbours are further than side neighbours —
annoying when you're doing "spread outward from here" style analysis.)

The catch: all the hexagons are the same size. Covering a big circle takes
*hundreds* of them — 547 at a 10 km radius here — and you need to name every
single one in your query.

**S2 (Google) — squares, mixed sizes, laid out along a curve**

S2 wraps the globe onto a cube, then repeatedly quarters each face like a
chessboard. Cells come in 31 sizes, from the whole face down to a centimetre.

Two things follow from this, and they're the reason S2 keeps winning below.

*First, it can mix sizes.* To cover a circle it uses a few big cells in the
middle where there's no edge to worry about, and small cells around the rim
where precision matters. The same 10 km circle that costs 547 hexagons costs
**32 S2 cells**.

*Second — and this is the clever part — the cell numbers form continuous
ranges.* S2 numbers its cells by walking a specific snaking path (a Hilbert
curve) through the grid, and the numbering is nested: every cell's children get
numbers that fall entirely inside the parent's number range, with nothing else
in between.

That means one big S2 cell isn't a list of thousands of numbers — it's a single
`from X to Y`. Which is exactly the shape of query a plain database index is
already brilliant at, the "ages 30–35" case from Part 1. So S2 turns a
two-dimensional search back into the one-dimensional range scan that databases
have been optimising for fifty years.

| | H3 | S2 |
|---|---|---|
| Shape | hexagons | squares |
| All one size? | yes | no, mixes freely |
| Cells for a 10 km circle | 547 | 32 |
| Query shape | "cell = A or B or C…" (547 values) | "between X and Y" (32 ranges) |
| Best at | neighbour/ring analysis, even grids | covering shapes, big areas |

There's one more decision hiding here, separate from which scheme you pick:
**where the cell ID lives.** You can store it as a column *next to* each point
and search that column — or you can turn it around and make the cell ID the
record's own key, with the points that fall inside stored in it.

That choice has nothing to do with H3 vs S2 and it's worth more than either.
Part 5 opens with it.

---

## Part 3 — Try it yourself

```bash
./run.sh        # starts the databases, loads the data, prints the results table
```

Then in two terminals:

```bash
uv run uvicorn api:app --reload --port 8000     # the API
cd ui && npm run dev                            # the web UI
```

Open **http://localhost:5173**.

What you can do there:

- **Click anywhere on the map** to move the search.
- **Pick a database** (Postgres / Redis / Aerospike / Elasticsearch) and a **method** (its own
  built-in index, or H3, or S2). Every combination runs against the real
  database and gets timed. The panel shows how each store actually fetched the
  cells — the wording differs per database, and that difference is Part 5.
- **Drag the radius slider** from 100 m to 10 km.
- **Tick the grid checkboxes** to draw the actual cells each scheme would look
  inside, on top of your circle. This is the single most useful thing in the
  UI — turn on "Geohash boxes" and see how enormous Redis' search area is
  compared to your circle.
- **"Race them all"** runs all twelve combinations at your current spot and
  ranks them.

Dot colours on the map:

| | |
|---|---|
| 🔵 **cyan** | inside your circle — the actual answer |
| 🟠 **orange** | the database read these, then threw them away (the waste) |
| ⚪ **grey** | everything else in the dataset |

There's also a standalone map generator if you don't want to run the UI:

```bash
uv run maps.py        # writes maps/coverage_100m.html etc.
```

### Manual setup, if you'd rather not use `run.sh`

```bash
docker compose up -d          # postgis :55432, redis :6380, aerospike :3000, elastic :9201
uv sync
uv run generate_data.py       # writes points.csv
uv run compare.py             # loads all four stores, prints the tables
```

Ports are deliberately shifted off the defaults so the containers don't fight
with a Postgres or Redis you already have installed.

---

## Part 4 — What actually happened

20,000 points, searching from the city centre, median of 5 runs, everything on
one laptop. "Correct answer" means a slow, exhaustive check of all 20,000 points
— so we're comparing against the truth, not against whichever database we
decided to trust.

### How long does a search take?

Times in milliseconds. Lower is better; the best in each column is bold.

| Database | Method | 100 m | 1 km | 10 km | how the cells are fetched |
|---|---|---|---|---|---|
| Postgres | its own index | 4.4 | 5.2 | 305.8 | — |
| Postgres | H3 | 2.9 | 5.4 | 74.5 | one query, `= ANY(cells)` |
| Postgres | S2 | 1.6 | 2.6 | 54.5 | one query, joined on ranges |
| Redis | its own index | **0.5** | **0.6** | 48.5 | — |
| Redis | H3 | 2.1 | 2.1 | 35.9 | one `SUNION` |
| Redis | S2 | 1.0 | 1.5 | 45.6 | one pipelined batch |
| Aerospike | its own index | 1.1 | 1.3 | 22.5 | — |
| Aerospike | H3 | 0.6 | 1.2 | **15.0** | one `batch_read`, cell ID is the key |
| Aerospike | S2 | 1.3 | 10.8 | 56.0 | 32 separate queries |
| Elasticsearch | its own index | 4.8 | 5.2 | 108.6 | — |
| Elasticsearch | H3 | 6.2 | 7.9 | 163.6 | one `terms` clause |
| Elasticsearch | S2 | 4.3 | 8.4 | 109.8 | one `bool.should` of ranges |

Every cell lookup above is a single round trip except Aerospike's S2 row — and
that one exception explains most of what's interesting here.

The Elasticsearch rows look bad and are the most misleading numbers in the
table. Nearly all of that time is shipping 7,438 documents back as JSON, not
finding them. Part 5, lesson 6.

### How much work was wasted?

At a 1 km search that should return 23 shops:

| Method | Shops actually read from the database | Waste |
|---|---|---|
| H3 | 186 | read 8x more than needed |
| S2 | 33 | read 1.4x more than needed |

Same query, same answer, and H3 makes the database do six times more work.
This is the orange dots in the UI, and it's the cost of fixed-size cells.

This number is about the *cells*, so it's the same on all three databases. What
differs between them is how fast they can hand those 186 rows over.

### Was anything wrong?

Almost nothing. Every method in all four databases returned the exactly correct
set of shops, at every radius, with one exception: **Redis missed 3 shops out of
7,438** at the 10 km search. More on why in Part 5 — it's not the reason you'd guess.

### "Find me the nearest 10"

| Database | Time | Correct? | How it's done |
|---|---|---|---|
| Postgres | 3.2 ms | yes | one line of SQL, built in |
| Redis | 1.3 ms | yes | hand-written loop |
| Aerospike | 3.4 ms | yes | hand-written loop |
| Elasticsearch | 3.7 ms | yes | `sort: _geo_distance` — exact, but a full scan |

Postgres has a real nearest-neighbour search. Redis and Aerospike don't, so
both have to fake it: search 200 m, did we find 10? No — try 400 m. Still no —
800 m. Keep doubling, then sort what you got.

Elasticsearch is a third case, and the most deceptive: one clean query, an exact
answer, and no index involved at all. `sort: _geo_distance` computes the
distance for all 20,000 documents and sorts them. It looks fine at this size and
does not scale — a real system would put a `geo_distance` filter in front of it,
which is the expanding-radius trick again, just written more nicely. (The `knn`
section of the Elasticsearch API is for `dense_vector` similarity, not geo. Easy
and common thing to confuse.)

That works fine in a dense city and falls apart in the countryside, where you
might double eight times before finding anything. It's also code *you* have to
write, test and get right, in every service that needs it.

---

## Part 5 — The six things worth remembering

### 1. Round trips are the whole game, and the data layout decides how many you make

Covering a 10 km circle takes 547 hexagons, so the database gets asked about 547
cell IDs. Everything depends on whether it can answer all 547 **in one request**.

All three can — but only if you ask the way each one wants to be asked:

| Database | The right way to ask | 10 km |
|---|---|---|
| Postgres | `WHERE h3_cell = ANY(<547 values>)` — one query | 74 ms |
| Redis | `SUNION` over 547 keys — one command | 36 ms |
| Aerospike | `batch_read` of 547 keys — one call | 15 ms |
| Elasticsearch | `terms` clause with 547 values — one query | 164 ms |

Elasticsearch takes the whole k-ring in one `terms` clause, ceiling 65,536
values. It is last in this table for reasons that have nothing to do with the
lookup — lesson 6.

The Aerospike row is the one that took a rethink. The obvious translation of
"store the cell ID and search it" is a secondary index on a bin — and Aerospike
secondary-index queries accept exactly one condition, no "or". Written that way,
547 hexagons means **547 separate round trips: 676 ms**, 30x slower than
Aerospike's own geo index and the worst result in the POC.

Easy to conclude "Aerospike is bad at this." Wrong, and instructively so. The
limitation is on *secondary-index queries*, not the database. Aerospike is built
around primary-key access, and `batch_read` pulls thousands of keys at once. So
invert the layout: don't store the cell ID as a field on each point — make the
**cell ID the primary key of its own record**, holding the points inside it. The
k-ring stops being 547 conditions to query and becomes 547 keys to fetch.

**676 ms → 15 ms.** Same hexagons, same answers, same H3 maths. Only the layout
changed. It's now the fastest method in the POC, beating Aerospike's own geo
index.

What it costs: the cell records duplicate the coordinates, so writes maintain
both copies; a dense cell becomes a large hot record (at resolution 8 the biggest
here holds 319 points — a coarser resolution would have you rewriting an enormous
record on every insert); and a point that *moves* has to be pulled out of one
cell record and pushed into another, which a plain field update would have done
for free. It's a read-optimised layout and you pay for it on writes.

**The lesson:** "can it fetch many keys at once?" is the right question to ask of
any database before building on cell IDs — but ask it about *every* access path,
not just the first one that looks familiar. The POC only ships the fast version;
the 676 ms number above is what the obvious version measured.

### 2. S2 needs 17x fewer lookups — which matters less than you'd think, and sometimes backfires

Same circle: H3 needs 547 cells, S2 needs 32 ranges. And they're a *better kind*
of lookup — H3 hands the database a list of exact values, while S2 says
"everything between X and Y", which is what a plain index is fastest at.

I expected that to dominate. It mostly doesn't, for a simple reason: once
everything fits in one round trip anyway, having 17x fewer things in that round
trip barely registers. At 10 km, Postgres reads 547 H3 cells in 74 ms and 32 S2
ranges in 54 ms. Real, but not the landslide the 17x suggests.

Elasticsearch behaves like Postgres here — it takes 32 range clauses in one
`bool.should`, and the S2 row beats the H3 row (110 ms vs 164 ms) because 32
range clauses are less query-parsing and less postings-list work than 547 exact
terms.

And on Aerospike it **reverses**:

| Aerospike, 10 km | Lookups | Time |
|---|---|---|
| S2 | 32 ranges | 56 ms |
| H3 | 547 hexagons | **15 ms** |

Because a range isn't a key. H3's cells can be enumerated, so they can be
primary keys, so they can be batched. S2's coverings are `from X to Y` — there's
nothing to batch-fetch, so Aerospike runs 32 separate queries and loses to the
scheme that needed 17x more lookups.

That's a genuinely useful property to have noticed, and it's the opposite of the
usual pitch for S2:

- **S2 wins** where range scans are cheap and native — a B-tree column, a sorted
  key space, anything you'd express as `BETWEEN`. Also for covering big or
  irregular regions, and for hierarchy ("is this point in this country").
- **H3 wins** on pure key-value stores, precisely *because* its cells are
  enumerable keys. Also for neighbour and ring analysis, since all its cells are
  the same size.

Three of the four databases here can batch ranges; one cannot. That split, not
the cell geometry, is what decides the winner.

Pick the scheme that matches how your database likes to be read, not the one
with the better-sounding cell count.

### 3. H3 wastes a lot of effort on small searches

A 100 m search still has to look inside 19 hexagons, because at the resolution
used here each hexagon is about 460 m across — the smallest possible ring of
them is already far bigger than the thing you asked for. S2 just picks a finer
cell size and returns **1** cell.

Practical upshot: choosing an H3 resolution is secretly choosing what search
radius you're good at. Real systems store several resolutions per point and
pick one per query. S2's coverer does that choosing for you.

### 4. The only real errors came from disagreeing about the shape of the earth

This was the surprise of the whole exercise.

The cell schemes had **zero** errors at every radius — which makes sense once
you see why: the cells only ever return *too much*, and the distance check at
the end is exact, so nothing correct ever gets lost.

Both discrepancies that did show up were about geometry, not indexing:

- **Redis missed 3 shops at 10 km.** Redis calculates distances assuming the
  earth's radius is 6,372,797 m. Our checking code assumes 6,371,009 m. A 0.03%
  disagreement — about 2.8 m at a 10 km radius. Three shops happened to be
  sitting within 2.8 m of the boundary, and the two systems disagreed about
  which side they were on.

- **Postgres returned 23 *extra* shops at 10 km**, until it was told not to. By
  default PostGIS models the earth as a slightly squashed sphere (the real
  shape — it bulges at the equator). Everything else here treats it as a
  perfect sphere. The gap between the two is up to 0.5%, which at 10 km is 50 m
  — enough to move 23 boundary shops in or out.

  The main comparison switches PostGIS to sphere mode so the numbers measure
  *indexing* rather than geography. `compare.py` prints the difference as its
  own separate table.

- **Elasticsearch has the same knob, and it made no difference here.**
  `geo_distance` takes `distance_type: arc` (Haversine, the default) or `plane`
  (flat-earth, cheaper). Same query, same answers at 100 m, 1 km *and* 10 km —
  delta zero at every radius. `plane` is accurate while the search area is small
  relative to the curvature it ignores, and 10 km at 13° latitude is well inside
  that. Push the radius into the hundreds of kilometres, or run it near a pole,
  and it falls apart. `compare.py` prints this table too, and printing a row of
  zeroes is the useful part: the knob exists, and knowing *when* it stops being
  free is the actual skill.

**The lesson worth carrying out of here:** when two systems disagree about
points near your boundary, suspect the earth model before you suspect the index.
Everyone loses an afternoon to this once.

### 5. Redis is extremely fast and the table undersells it

0.5 ms for a 100 m search, in memory, with zero tuning. Nothing else is close at
small radii.

It degrades at 10 km (49 ms) because of how `GEOSEARCH` works: it picks a
geohash box big enough to contain your radius, then scans that box and its eight
neighbours. At 10 km that 3×3 block of boxes is *vastly* bigger than your
circle. Turn on "Geohash boxes" in the UI at any radius and the orange
rectangles will dwarf your circle — that picture is the 49 ms.


### 6. Elasticsearch's time went almost entirely into *returning* the rows, not finding them

The 10 km Elasticsearch numbers (109–164 ms) are the worst in the POC, and taken
at face value they say the BKD tree is slow. It isn't. Same query, same index,
only the amount of data sent back changes:

| 10 km `geo_distance` query | Time |
|---|---|
| `_count` — match everything, return only the number | **2.7 ms** |
| `size: 1` — match everything, return one document | 5.7 ms |
| Full result — all 7,438 documents | 87.3 ms |

Matching 7,438 of 20,000 documents costs under 3 ms. Handing them over costs
another 85. That is JSON serialisation, HTTP, and the fact that 7,438 rows do
not fit in one response — the reads here page with `search_after`, because the
default `from`/`size` window stops at 10,000 and `from: 10000` is a bad idea
long before that.

This is not a flaw, it's the design. Elasticsearch is built to rank documents
and return **the top N of them** — a page of search results, or an aggregation
computed server-side. "Give me all 7,438 matching rows" is the one access
pattern it is least suited to, and it is exactly what this POC asks of every
store.

Two things follow, and both are load-bearing in an interview:

- **Compare like for like.** The Postgres and Aerospike numbers include their
  rows crossing the wire too, but through binary protocols rather than JSON over
  HTTP. Part of the gap is genuinely the protocol.
- **Ask what the query is for.** If the answer feeds a map viewport, you want
  the nearest 50 and a count — Elasticsearch does that in ~3 ms and the ranking
  above is meaningless. If you genuinely need every row, you would reach for
  `_count` plus aggregations, or a different store.

The general version: **a benchmark measures the access pattern you wrote, not
the database.** Write the pattern a store is worst at and you will reliably
"prove" it is slow.

---

## Part 6 — The files

Python (the backend):

| File | What it does |
|---|---|
| `common.py` | Shared settings, the distance formula, and the slow-but-correct brute-force check everything is graded against |
| `generate_data.py` | Makes `points.csv` — 20,000 points, mostly clustered in hotspots so the data isn't unrealistically even |
| `postgres_geo.py` | Postgres: built-in spatial search, plus H3 and S2 on plain columns |
| `redis_geo.py` | Redis: `GEOSEARCH`, plus H3 and S2 using ordinary Redis data structures |
| `aerospike_geo.py` | Aerospike: its geo index, S2 on a plain numeric bin, and H3 stored inverted — cell ID as the primary key, read with `batch_read` |
| `elastic_geo.py` | Elasticsearch: `geo_point` on a BKD tree, plus H3 as a `keyword` and S2 as a `long`, paged with `search_after` |
| `h3_layer.py` | Works out which hexagons cover a circle |
| `s2_layer.py` | Works out which S2 cells cover a circle, and converts them to number ranges |
| `geohash_layer.py` | A small geohash implementation, used only to *draw* what Redis searches |
| `compare.py` | Runs everything, prints the results tables |
| `maps.py` | Writes standalone HTML maps of the three cell shapes |
| `api.py` | The HTTP API the web UI talks to |

The four database files deliberately expose the same handful of methods
(`load_data`, `radius_query`, `knn_query`, `h3_radius_query`, `s2_radius_query`),
which is what lets `compare.py` and the API treat them interchangeably. What
differs is *how* each implements them — `h3_radius_query` is a `SUNION` in Redis,
an `= ANY` in Postgres, a `batch_read` in Aerospike and a `terms` clause in
Elasticsearch — and each store advertises
the methods it actually has, so the comparison table and the UI pick them up
rather than having them hardcoded.

Frontend (`ui/`): React + Leaflet.

| File | What it does |
|---|---|
| `src/App.jsx` | Holds the state and decides what to fetch when |
| `src/components/MapView.jsx` | The map, the dots, the circle, the cell outlines |
| `src/components/ControlPanel.jsx` | Database / method / radius / grid controls |
| `src/components/ResultsPanel.jsx` | Timing, count, mistakes, wasted reads |
| `src/components/CompareTable.jsx` | The "race all nine" table |
| `src/api.js` | Talks to the Python API |

`points.csv`, `maps/`, `node_modules/` and the virtualenv are gitignored —
everything regenerates from the scripts.

---

## A caveat on the numbers

These timings include Python overhead and come from four containers sharing one
laptop. They are honest for comparing the *methods against each other on the
same machine*, which is the whole point. They are **not** benchmarks of how fast
Postgres, Redis, Aerospike or Elasticsearch are — don't quote them as such, and
see lesson 6 for how badly that can mislead.

Useful flags:

- `uv run compare.py --stores redis,postgres` — run a subset
- `uv run compare.py --skip-load` — re-query without reloading the data
- `uv run compare.py --repeat 20` — more runs, steadier numbers

The numbers in Part 4 come from `uv run compare.py --repeat 7`.
