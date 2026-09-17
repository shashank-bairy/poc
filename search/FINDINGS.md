# Findings — measured behaviour and results

Design brief: [`search-poc.md`](./search-poc.md). How to run any of this:
[`README.md`](./README.md). What the mechanics mean:
[`HOW-IT-WORKS.md`](./HOW-IT-WORKS.md).

Everything here was measured on one machine: M-series laptop, Docker Desktop,
each engine single-node, Postgres 16 + pgvector, Redis 8, Lucene 9.11.1,
Elasticsearch 8.15.3, OpenSearch 2.17.1, Solr 9.7. Raw output in `results/`.

Two corpora, deliberately kept apart:

| | corpus | size | what it answers |
|---|---|---|---|
| **Performance** | arXiv CS papers (Kaggle snapshot) | 200,000 docs / 208 MB text | build cost, index size, per-query latency, agreement |
| **Relevance** | BEIR SciFact | 5,183 docs / 300 judged queries | nDCG@10, recall@10, MRR |

arXiv has no relevance judgements and SciFact is too small to stress any
engine, so the two tables are never merged. Reproduce with:

```bash
uv run python -m bench.compare                   # performance, arXiv
uv run python -m bench.evaluate                  # relevance, SciFact
uv run python -m bench.evaluate --engines elasticsearch,dense,hybrid
```

Latencies are p50 of 20 warm runs. Warm on purpose: a cold-cache number on a
laptop measures the page cache, not the engine.

## The five findings

1. **Raw Lucene, Elasticsearch and OpenSearch score identically** — 0.6684
   nDCG@10 to four decimals, on all 300 queries. The REST layer changes nothing
   about retrieval.
2. **Redis' scorer flag is worth 0.58 nDCG.** TFIDF (the default) 0.076 vs
   BM25STD 0.655, same index, one parameter.
3. **Postgres ranks at 0.36 against 0.67**, because `ts_rank` has no IDF term
   and no length normalization — and at 200k docs it is 10-70x slower than the
   Lucene engines on any query touching many rows.
4. **Faceting on Postgres works**, in 12 ms with correct counts. What it lacks
   is a facet *engine*: cost tracks the match count instead of staying flat.
5. **RRF beats both its inputs** — BM25 0.668, dense 0.647, fused 0.708 — which
   is the whole argument for hybrid search.

## Relevance — BEIR SciFact, 5,183 docs, 300 judged queries

```
engine         nDCG@10  recall@10  MRR     ms/query
postgres       0.3620   0.5239     0.3162  28.19
redis-tfidf    0.0761   0.1280     0.0642   2.52
redis-bm25     0.0718   0.1172     0.0597   2.22
redis-bm25std  0.6549   0.7793     0.6213   2.34
lucene         0.6684   0.8013     0.6316   1.94
elasticsearch  0.6684   0.8013     0.6316   3.04
opensearch     0.6684   0.8013     0.6316   2.32
solr           0.6668   0.8037     0.6274   4.54
```

Four things in that table are worth more than the rest of the POC put together.

**Raw Lucene, Elasticsearch and OpenSearch agree to four decimal places.** Not
approximately — identically, on every one of the 300 queries. It is the same
BM25 over the same analyzer chain, and the REST layer changes nothing about
retrieval. Solr differs in the fourth decimal because its `text_en` analyzer
chain is not byte-identical to Lucene's `EnglishAnalyzer`, not because anything
deeper diverges. Whatever separates these four products, it is not relevance.

**Redis' scorer flag is worth 0.58 nDCG.** Same index, same postings, one
parameter apart:

| `SCORER` | nDCG@10 | |
|---|---|---|
| `TFIDF` | 0.076 | the default if you pass nothing |
| `BM25` | 0.072 | Redis' older BM25 variant |
| `BM25STD` | 0.655 | standard BM25, the formulation Lucene implements |
| `DISMAX` | 0.062 | |

That is the difference between a search box that works and one that does not,
hiding behind a parameter most people never pass. On Redis 8, use `BM25STD`.

**Postgres scores 0.36 against 0.67.** `ts_rank_cd` has no IDF term and no
document-length normalization: a match on a rare word and a match on a common
one are weighted the same, and a long abstract is not penalized for being long.
Those two missing factors *are* the gap. It is also the slowest engine in the
table at 28 ms per query — the BEIR queries are whole sentences, and OR-ing
twenty lexemes across a GIN index is real work.

Note what this does *not* say: Postgres FTS is not useless at 0.36. It needs no
new service, it is transactional, and it joins with your actual data. The
tradeoff is now a number instead of an opinion.

## Phase 2 — hybrid retrieval

```
engine         nDCG@10  recall@10  MRR     ms/query
elasticsearch  0.6684   0.8013     0.6316   2.87   BM25 alone
dense          0.6472   0.7900     0.6048  16.38   MiniLM vectors alone
hybrid         0.7076   0.8401     0.6700  40.90   RRF over both
```

Dense retrieval *loses* to BM25 here, and fusing the two still beats both. That
is the entire argument for hybrid search in one table: RRF is not averaging two
rankings, it is exploiting the fact that keyword and vector retrieval fail on
*different* queries. Keyword nails exact and rare terms, vectors nail
paraphrase, and Reciprocal Rank Fusion combines them using ranks only — so no
score normalization is needed between an unbounded BM25 score and a cosine
similarity.

The 41 ms is almost entirely the query embedding (a forward pass through MiniLM
on CPU), not retrieval.

## Performance — index build

200,000 arXiv CS papers, 208 MB of raw text.

```
engine          docs     build  index size  vs corpus  notes
postgres        200,000  53.5s  108.4 MB    0.52x      insert 40.6s + index 12.9s
redis[bm25std]  200,000  37.3s  220.1 MB    1.06x      in RAM; 25.9M records
lucene          200,000  15.3s  189.8 MB    0.91x      forceMerge(1)
elasticsearch   200,000  45.9s  230.3 MB    1.10x      1 segment after forcemerge
opensearch      200,000  35.8s  181.9 MB    0.87x      1 segment; same bodies as ES
solr            200,000  21.6s  212.0 MB    1.02x      after optimize
```

Raw Lucene builds in 15s where Elasticsearch takes 46s over the same documents
and the same analyzer — that gap is bulk HTTP, JSON parsing and the translog,
not indexing. Postgres' index is the smallest at 0.52x because `tsvector`
stores lexemes and positions but no term dictionary of its own per segment;
the table behind it is another 478 MB. Redis' 220 MB is RAM, which is the
number that decides whether Redis is an option at all.

## Per-query latency

p50 ms, 20 warm runs, full table in `results/compare-arxiv.txt`. Every query
runs twice — count-only (matching cost) and fetch (matching + fetching) —
because one number hides the split.

| query | postgres | redis | lucene | elastic | opensearch | solr |
|---|---|---|---|---|---|---|
| 1 term | 11.85 | 2.63 | 2.04 | 1.66 | 4.48 | 2.77 |
| 4 prefix | **115.65** | 4.10 | 1.66 | 6.88 | 5.05 | 2.28 |
| 5 fuzzy | **51.75** | 7.45 | 5.88 | 7.16 | 7.00 | 2.37 |
| 6 filter+text | 13.33 | 4.92 | 2.29 | 3.53 | 5.81 | 3.44 |
| 9 deep page | **174.85** | 14.03 | 5.37 | 6.44 | 6.35 | 7.64 |
| 10 highlight | **114.23** | 3.91 | 4.29 | 7.30 | 6.96 | 5.77 |
| 11 boosted | **234.98** | 16.84 | 3.35 | 4.23 | 3.34 | 2.22 |

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

The count/fetch split is the other thing worth reading. Lucene answers query 11
in 1.02 ms when only counting and 3.35 ms when returning ten documents —
matching is cheap, decompressing stored fields is not. Elasticsearch's gap is
wider still (1.35 → 4.23 ms) because the fetch phase is a second round trip
internally.

Solr is quietly the most consistent engine in the table: never the fastest,
never above 8 ms on anything.

## Faceting

Top `categories` for `retrieval` on the arXiv corpus — the query Postgres was
supposed to be unable to answer:

```
postgres        cs.CV:1207, cs.IR:1180, cs.CL:566, cs.LG:534, cs.IT:383   12.4 ms
redis[bm25std]  cs.CV:1212, cs.IR:1182, cs.CL:568, cs.LG:537, cs.IT:383    1.4 ms
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

## Top-10 agreement

Same query, same corpus, different documents. Against Postgres as baseline, on
the 200k arXiv corpus:

```
query           postgres  redis  lucene  elasticsearch  opensearch  solr
1 term          --        2/10   4/10    3/10           3/10        4/10
2 phrase        --        3/10   6/10    6/10           6/10        6/10
4 prefix        --        0/10   0/10    1/10           1/10        0/10
5 fuzzy         --        0/10   0/10    0/10           0/10        0/10
8 sort by date  --        2/10   10/10   9/10           2/10        10/10
9 deep page     --        0/10   0/10    0/10           0/10        0/10
11 boosted      --        3/10   1/10    1/10           1/10        1/10
```

The whole table shrank when the corpus grew from 5k to 200k, and that is the
finding: with 74,902 documents matching query 11 instead of 1,014, the scorer
decides everything and the scorers genuinely differ. On a small corpus engines
agree because there is barely a choice to make.

Query 5 is 0/10 everywhere because the engines are not even answering the same
question — Postgres matches trigrams against `title` (1,740 hits), Lucene
family runs a Levenshtein automaton over `abstract` (14,492), Redis a two-edit
walk of its own dictionary (8,953). Query 9 is 0/10 because page 500 of a
37,000-hit result is pure scorer noise.

Query 8 splits interestingly: Lucene and Solr agree 10/10 with Postgres, ES
9/10, OpenSearch 2/10. All four sort by the same date field — the difference is
tie-breaking among the many papers sharing an `update_date`, which is
unspecified behaviour every engine resolves by internal doc ID.

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
it is a choice you must make explicitly: the scorer (default TFIDF is unusable),
the dialect (2, pinned), the AND/OR semantics of a space. Faceting a
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
unchanged, identical relevance to four decimals, index 181.9 MB against ES's
230.3 MB (different default codec settings, same content). Occasional p95 spikes
this POC did not chase (61 ms on highlight, 71 ms on prefix in one run). The one
code-level divergence is vectors: `knn_vector` + a `knn` query clause rather
than `dense_vector` + a top-level `knn`.

**Solr** — quietly the best-behaved engine in the latency table: never the
fastest, never above 8 ms on anything, and the tightest p95 spread. Builds in
21.6s. Its relevance differs from Lucene's in the fourth decimal only, from
`text_en` not being byte-identical to `EnglishAnalyzer`. The cultural
difference is real though: schema declared up front, `fq` for filters,
`edismax` for user text, and its analysis API is exposed as an endpoint you can
query — which this POC uses to stem fuzzy terms.

| Engine | The thing worth remembering |
|---|---|
| **Postgres** | `tsvector` + GIN, no IDF, no facet engine — and no new service. 0.36 vs 0.67, and 235 ms vs 3 ms, is the price. |
| **Redis** | Index is a *view over hashes that already exist*. Scoring is a choice, and the default is the wrong one. |
| **Lucene** | Segments, postings, analyzers, BM25. Do this stage properly and the next three are configuration. |
| **Elasticsearch** | Mapping vs analysis; `match` analyzes, `term` does not. The `text`+`keyword` multi-field exists for that. |
| **OpenSearch** | Reuses every query body from `engines/elasticsearch.py` unchanged. The brevity of `engines/opensearch.py` is the finding. |
| **Solr** | Same Lucene, schema-first culture. `fq`, `edismax`, cores vs collections, ZooKeeper instead of built-in coordination. |

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
   values**, which the BEIR sets are. Faceting has to degrade, not crash.
7. **`LongPoint` is not sortable.** Range queries need the BKD tree, sorting
   needs a separate doc-values field over the same number.

## Not done

* **Deep paging (query 9) is measured but not solved.** `search_after` and
  `cursorMark` are the real answers and neither is implemented; the suite shows
  the `from`/`size` cost and stops there. Redis additionally caps this with
  `MAXSEARCHRESULTS` (default 10,000).
* **Sharding and cluster behaviour.** Everything runs single-node, so
  distributed scoring (IDF is per-shard by default, `dfs_query_then_fetch`
  fixes it), rebalancing and SolrCloud/ZooKeeper coordination are untested.
* **Relevance on arXiv.** The nDCG numbers are SciFact only, because arXiv
  ships no relevance judgements. Latency is arXiv, relevance is BEIR, and the
  two tables are deliberately not merged.
* **Phase 2 at 200k.** Embeddings and hybrid were measured on SciFact's 5k
  docs; embedding 200k abstracts on CPU is roughly half an hour.
