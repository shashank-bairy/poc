# Findings — how each store behaves

How to run any of this: [`README.md`](./README.md). What the mechanics mean:
[`HOW-IT-WORKS.md`](./HOW-IT-WORKS.md). How one document and one query move
through each engine: [`WALKTHROUGH.md`](./WALKTHROUGH.md).

Everything here was measured on one machine: M-series laptop, Docker Desktop,
each engine single-node, Postgres 16 + pgvector, Redis 8, Lucene 9.11.1,
Elasticsearch 8.15.3, OpenSearch 2.17.1, Solr 9.7.

The corpus is 200,000 arXiv CS papers from the Kaggle snapshot, 208 MB of raw
text. Latencies are p50 of 20 warm runs — warm on purpose, since a cold-cache
number on a laptop measures the page cache, not the engine.

The numbers below are a record of a run that has since been removed from the
code: the POC no longer ships a benchmark harness, because the point of it now
is the data model and the query syntax, not the measurement. Reproduce the
behaviour with `uv run python -m engines.<name>`; output in `results/`.

## What came out of it

1. **The four Lucene engines return the same documents in pairs.** Raw Lucene
   and Solr agree with each other, Elasticsearch and OpenSearch agree with each
   other. The pairing follows the *query path*, not the product: Lucene and
   Solr send the same literal string through the same QueryParser; ES and
   OpenSearch send the same `match` body. The REST layer changes nothing about
   retrieval.
2. **Postgres is 10-70x slower at 200k documents** on any query touching many
   rows, and is the only engine here with no doc-values equivalent.
3. **Faceting on Postgres works**, in 12 ms with correct counts. What it lacks
   is a facet *engine*: cost tracks the match count instead of staying flat.
4. **Redis is the only store where the index is a view over your data.** You
   write ordinary hashes; `FT.CREATE` tells Redis to watch a key prefix. Every
   other engine here owns its documents.
5. **Type systems differ more than query syntax does.** Postgres has `text[]`
   and `date`; Redis has neither, so categories become a pipe-joined string and
   dates become integers of epoch days. That conversion is in `index()`.

## Performance — index build

200,000 arXiv CS papers, 208 MB of raw text.

```
engine          docs     build  index size  vs corpus  notes
postgres        200,000  50.7s  108.4 MB    0.52x      insert 38.8s + index 11.9s
redis           200,000  31.3s  220.1 MB    1.06x      in RAM; 25.9M records
lucene          200,000  14.8s  189.8 MB    0.91x      forceMerge(1)
elasticsearch   200,000  34.8s  230.3 MB    1.10x      1 segment after forcemerge
opensearch      200,000  31.3s  231.2 MB    1.11x      1 segment; same bodies as ES
solr            200,000  20.6s  211.8 MB    1.02x      after optimize
```

Index size is the sum of the live segments, not `indices.stats` store size.
That distinction cost a debugging session: read right after `forcemerge`, the
store still counts the segments the merge has superseded but not yet deleted,
and it reported 464 MB for an index whose live segment is 231 MB. Elasticsearch
and OpenSearch land within 1 MB of each other, which is what identical Lucene
over identical documents should do.

Raw Lucene builds in 15s where Elasticsearch takes 46s over the same documents
and the same analyzer — that gap is bulk HTTP, JSON parsing and the translog,
not indexing. Postgres' index is the smallest at 0.52x because `tsvector`
stores lexemes and positions but no term dictionary of its own per segment;
the table behind it is another 478 MB. Redis' 220 MB is RAM, which is the
number that decides whether Redis is an option at all.

## Per-query latency

p50 ms, 20 warm runs. Every query
runs twice — count-only (matching cost) and fetch (matching + fetching) —
because one number hides the split.

| query | postgres | redis | lucene | elastic | opensearch | solr |
|---|---|---|---|---|---|---|
| 1-term | 12.02 | 2.59 | 5.63 | 2.59 | 3.46 | 1.65 |
| 4-prefix | **114.24** | 4.29 | 1.74 | 5.32 | 4.07 | 1.93 |
| 5-fuzzy | **52.19** | 7.39 | 6.28 | 5.57 | 13.16 | 2.77 |
| 9-deep-page | **~175** | ~14 | ~5 | ~6 | ~6 | ~8 |
| 10-highlight | **~114** | ~4 | ~4 | ~7 | ~7 | ~6 |
| 11-boosted | **~235** | ~17 | ~3 | ~4 | ~3 | ~2 |

At 5k documents Postgres looked competitive. At 200k it is 10-70x slower than
the Lucene engines on every query that touches many rows, and the reasons are
all structural rather than tunable:

* **Query 11, 235 ms.** 74,880 matches, each one scored by `ts_rank`, which
  must read the `tsvector` column for every matching row. Lucene scores from
  the postings it is already walking and never touches a stored field until
  the final ten. 1.02 ms count vs 3.35 ms fetch there, against 153 ms vs
  235 ms.
* **Query 9, 175 ms.** `LIMIT 10 OFFSET 4990` sorts 37,277 rows by rank to
  discard 4,990. Lucene's `offset+limit` has the same shape and costs 5 ms,
  because a priority queue over postings is not a sort over materialized rows.
* **Query 10, 114 ms.** `ts_headline` re-analyzes the original abstract at
  query time, for every returned row, with no index involved.
* **Query 4, 116 ms.** A prefix expands to thousands of lexemes and GIN must
  union all their posting lists eagerly.

The count/fetch split is the other thing worth reading. Every engine has a
`q1_term_count()` beside `q1_term()` — the same match with nothing fetched:

```
engine          count ms  fetch ms  ratio
postgres        2.46      12.44     5.1x
redis           0.85      2.00      2.3x
lucene          0.81      1.28      1.6x
elasticsearch   0.97      1.49      1.5x
opensearch      1.33      2.04      1.5x
solr            1.68      1.82      1.1x
```

Matching is cheap everywhere; the gap is stored-field decompression. Postgres'
5.1x is the outlier because its "fetch" also re-reads the `tsvector` of every
matching row to rank it.

Solr is quietly the most consistent engine in the table: never the fastest,
never above 8 ms on anything.

## Faceting

Top `categories` for `retrieval` on the arXiv corpus — the query Postgres was
supposed to be unable to answer:

```
postgres        cs.CV:1207, cs.IR:1180, cs.CL:566, cs.LG:534, math.IT:383 12.4 ms
redis           cs.CV:1212, cs.IR:1182, cs.CL:568, cs.LG:537, cs.IT:383    1.4 ms
lucene          cs.CV:1195, cs.IR:1137, cs.CL:554, cs.LG:532, cs.IT:363    1.9 ms
elasticsearch   cs.CV:1195, cs.IR:1137, cs.CL:554, cs.LG:532, cs.IT:363    4.2 ms
opensearch      cs.CV:1195, cs.IR:1137, cs.CL:554, cs.LG:532, cs.IT:363    4.8 ms
solr            cs.CV:1195, cs.IR:1137, cs.CL:554, cs.LG:532, cs.IT:363    2.3 ms
```

It answers it, in 12 ms, and the counts are right. The small spread is match-set
size, not aggregation error: Postgres and Redis match a few more documents than
the Lucene engines because their analyzers stem slightly differently, so they
have more rows to count. Counts sum to 3,684 against 4,076 hits — a paper
belongs to several categories, which is exactly the multi-valued behaviour
worth understanding before discussing facets.

The honest version of "Postgres cannot facet" is therefore: it has no facet
*engine*: no doc-values column, no cached ordinals. It unnests arrays over
matching rows on every query, so the cost tracks the match count. At 4,076
matches that is 12 ms; the Lucene engines read a column and are flat.

## Observed behaviour, engine by engine

**Postgres** — cheapest index (0.52x the raw text) and the slowest queries by a
wide margin once the corpus is real. Its cost profile is unlike every other
engine here: the count/fetch split on query 11 is 153 ms → 235 ms, meaning the
*matching* itself is expensive, not just the row fetch. `ts_rank` must read the
`tsvector` of every matching row; there is no "score from the postings you are
already walking" path. Fuzzy matching goes through a second, unrelated index
(`pg_trgm`), so it answers a different question from everyone else and returns
1,740 hits where Lucene returns 14,492. What it buys: no new service, joins
against real relational data, and transactional writes.

**Redis** — fastest build (37s) after Lucene, and the most consistent low
latency in the table except on query 11 (16.8 ms, its weakest). Everything about
it is a choice you must make explicitly: the scorer (`SCORER BM25STD`, since
the default is TFIDF), the dialect (2, pinned), the AND/OR semantics of a
space. Faceting a
multi-valued TAG needs client-side expansion. The index is 220 MB of RAM for
200k abstracts — a 1.06x multiplier on the raw text, which is the number that
decides whether Redis is an option at all.

**Lucene, raw** — 15.3s to build where Elasticsearch needs 45.9s over the same
documents with the same analyzer; that 3x gap is bulk HTTP, JSON parsing and
the translog, not indexing. It is also the fastest or second-fastest on 9 of 11
queries despite paying a loopback HTTP hop this POC added. Its count-only
numbers are the cleanest demonstration in the whole exercise that matching is
cheap and fetching stored fields is not: 1.02 ms vs 3.35 ms on query 11.

**Elasticsearch** — largest index (1.10x) and a consistently wider count/fetch
gap than raw Lucene (1.35 → 4.23 ms on query 11), because the fetch phase is a
second internal round trip. Nothing in its *results* differs from raw Lucene.
What it adds is operational: mapping management, coordination, the bulk API,
and query-DSL ergonomics.

**OpenSearch** — every query body reused from `engines/elasticsearch.py`
unchanged, and the same documents come back. Index 231.2 MB against ES's
230.3 MB: identical Lucene over identical documents, as it should be.
Occasional p95 spikes this POC did not chase (61 ms on highlight, 71 ms on
prefix in one run). The one code-level divergence is vectors: `knn_vector` + a
`knn` query clause rather than `dense_vector` + a top-level `knn`.

**Solr** — quietly the best-behaved engine in the latency table: never the
fastest, never above 8 ms on anything, and the tightest p95 spread. Builds in
21.6s. Its results differ from raw Lucene's only where `text_en` is not
byte-identical to `EnglishAnalyzer`. The cultural
difference is real though: schema declared up front, `fq` for filters,
`edismax` for user text, and its analysis API is exposed as an endpoint you can
query — which this POC uses to stem fuzzy terms.

| Engine | The thing worth remembering |
|---|---|
| **Postgres** | `tsvector` + GIN, no facet engine — and no new service. 235 ms vs 3 ms is the price. |
| **Redis** | Index is a *view over hashes that already exist*. No array type, no date type — `index()` converts both. |
| **Lucene** | Segments, postings, analyzers. Understand this one and the next three are configuration. |
| **Elasticsearch** | Mapping vs analysis; `match` analyzes, `term` does not. The `text`+`keyword` multi-field exists for that. |
| **OpenSearch** | Reuses every query body from `engines/elasticsearch.py` unchanged. The brevity of `engines/opensearch.py` is the finding. |
| **Solr** | Same Lucene, schema-first culture. `fq`, `edismax`, cores vs collections, ZooKeeper instead of built-in coordination. |

## How the queries are written

Every query is a function in its engine's module, with the query written out
inside it. The same question, six dialects -- `q5_fuzzy()`, matching a
misspelling:

```sql
-- engines/postgres.py
SELECT id, word_similarity('transfomer', title) AS score, title
FROM docs WHERE 'transfomer' <% title ORDER BY score DESC LIMIT 10;
```
```
# engines/redis.py
FT.SEARCH idx:docs '%%transfomer%%' SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2
```
```
# engines/lucene.py            # engines/solr.py
abstract:transfom~2            q=abstract:transfom~
```
```json
// engines/elasticsearch.py -- opensearch.py imports this dict unchanged
{"size": 10, "query": {"match": {"abstract": {"query": "transfomer", "fuzziness": "AUTO"}}}}
```

Five different notions of "fuzzy" in one row: trigram word similarity against
`title`, a two-edit walk of Redis' own dictionary, a Levenshtein automaton over
stemmed terms, and an analyze-then-fuzzify `match`. They return 1,740, 8,953
and 14,492 hits respectively and overlap 0/10 in the top ten. Averaging that
into "fuzzy search latency" would have been meaningless, which is why the
queries are visible rather than generated.

`uv run python -m engines.redis` prints every one of them with its results.

## Traps this POC actually hit

Each of these cost a debugging session and is now a comment in the code.

1. **Fuzzy matching runs against stems.** The index holds `transform`; the query
   `transfomer` is 3 edits from it and matches nothing. Elasticsearch's `match`
   + `fuzziness` hides this by analyzing first. Lucene's QueryParser, Solr's
   multiterm chain and Redis' `%term%` do not — so the raw Lucene stage stems
   the term itself, Solr asks its own analysis API, and Redis needs `%%` rather
   than `%`.
2. **A space means AND in Redis.** A sentence-long query requires every term and
   returns nothing — including inside a field clause, where
   `@title:(language model)` matched 6 documents against everyone else's 1,014.
3. **Postgres `plainto_tsquery` also ANDs.** Same trap, different syntax; every
   other engine's default is OR plus a ranker.
4. **`redis-py`'s `Document.id` is the key, not your `id` field** — it silently
   shadows the stored field, so every result ID came back as `doc:123`.
5. **Redis cannot facet a multi-valued TAG server-side.** `GROUPBY` groups by
   the whole stored string: `cs.IR|cs.LG` is one bucket, not two. The counts are
   expanded client-side here. Lucene-based engines read per-value doc values.
6. **Lucene's `SortedSetDocValuesReaderState` throws on a corpus with no facet
   values.** Faceting has to degrade, not crash.
7. **`LongPoint` is not sortable.** Range queries need the BKD tree, sorting
   needs a separate doc-values field over the same number.
8. **`indices.stats` store size lies right after a forcemerge.** It counts the
   segments the merge replaced until they are deleted, and files open readers
   still hold: 464 MB reported for a 231 MB index. Summing the live segments
   from `_cat/segments` is the honest measurement.
9. **redis-py speaks RESP3 against Redis 8**, so `FT.SEARCH` returns a dict
   (`{"total_results": n, "results": [...]}`) rather than the flat
   `[total, key, [f, v...], ...]` list every tutorial shows. `FT.AGGREGATE`
   nests its groups one level deeper again, under `extra_attributes`.

## Not done

* **Deep paging (query 9) is measured but not solved.** `search_after` and
  `cursorMark` are the real answers and neither is implemented; the suite shows
  the `from`/`size` cost and stops there. Redis additionally caps this with
  `MAXSEARCHRESULTS` (default 10,000).
* **Sharding and cluster behaviour.** Everything runs single-node, so
  distributed scoring (IDF is per-shard by default, `dfs_query_then_fetch`
  fixes it), rebalancing and SolrCloud/ZooKeeper coordination are untested.
* **Ranking quality.** Which engine returns *better* results is not measured
  here and not the point of this POC. The engines rank differently — Postgres'
  `ts_rank` has no IDF term, Redis' default scorer is TFIDF, the four Lucene
  engines use BM25 — but comparing those properly needs judged queries and a
  harness, which were removed to keep this readable.
* **Vector search is present but not exercised.** `vector_search()` exists on
  every engine and `core/embed.py` produces the vectors, so the *syntax* of
  kNN per engine is here. No corpus was embedded at 200k: that is roughly half
  an hour on CPU.
