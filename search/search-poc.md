# Search POC — Postgres FTS, Redis, Lucene, Elasticsearch, OpenSearch, Solr (+ embeddings)

## Goal

Understand six search approaches by running the **same corpus and the same query suite** through
each one, then comparing index size, latency, and — more importantly — result quality measured
against human relevance judgements rather than opinion.

The framing that matters:

- **Lucene** is the engine. A Java library. Inverted index, postings lists, BM25, analyzers.
- **Elasticsearch**, **OpenSearch**, **Solr** are three servers wrapping that same Lucene.
  They differ in API, cluster coordination, and licensing — not in core retrieval.
- **Postgres full-text search** is a genuinely separate implementation: `tsvector` + GIN index.
  Weaker ranking, no faceting, but zero extra infrastructure and it joins with your real data.
- **Redis** is the other separate implementation: the query engine (formerly the RediSearch module,
  in core since Redis 8) builds its own inverted index, in memory, with no Lucene anywhere.

So this is *one library, three wrappers, and two outsiders* — not six peers. Interviewers probe
exactly this distinction.

## Background: how each system indexes text

> A detailed, plain-language walkthrough of everything in this table — inverted indexes, analysis
> chains, TF-IDF vs BM25, segments, sharding — is in [`HOW-IT-WORKS.md`](./HOW-IT-WORKS.md).

| System | Indexing approach | Notes |
|---|---|---|
| **Postgres** | GIN index over `tsvector` | `to_tsvector` normalizes/stems into lexemes; `tsquery` matches. `ts_rank` is frequency-based, not BM25. `pg_trgm` adds fuzzy/typo matching via trigram GIN. |
| **Redis** | Its own inverted index, held in memory | `FT.CREATE` defines a schema over existing hashes or JSON documents. Real ranking (TFIDF default, BM25 selectable), faceting via `FT.AGGREGATE`, and vector search in the same index. Bounded by RAM. |
| **Lucene (raw)** | Inverted index in immutable segments | `IndexWriter` -> segments -> background merge. Term dictionary + postings + norms. BM25 default since Lucene 6. The three rows below are REST veneers on this one; the two above are not. |
| **Elasticsearch** | Lucene + JSON REST + sharding | Mapping (field types) vs analysis (analyzer chain). `text` vs `keyword` dual-field is the central concept. Built-in cluster coordination. |
| **OpenSearch** | Fork of Elasticsearch 7.10 | ~95% identical API. Diverges on licensing and plugins — k-NN vector search is built in and free. |
| **Solr** | Lucene + schema-first + SolrCloud | `managed-schema`, cores vs collections, ZooKeeper for coordination, `edismax` query parser. Historically the strongest faceting story. |

Core idea behind all of them: an inverted index maps *term -> list of documents containing it*, so a
text query reduces to intersecting/unioning sorted integer lists, then scoring the survivors.

## Datasets

**Two datasets, two different jobs.** This is the most important design decision in the POC, and the
reason is worth stating plainly: latency is easy to measure and relevance is not. One corpus gives
you realistic engine behaviour; the other gives you a relevance number that isn't your own opinion.

### 1. The corpus — arXiv metadata

Kaggle: `Cornell-University/arxiv` (CC0, ~2.7M papers, one JSON-lines file). Subset to ~200k CS
papers. **Verify the slug when downloading — Kaggle dataset names do move.**

| Field | Type | Exercises |
|---|---|---|
| `title` | short text | term/phrase match, autocomplete |
| `abstract` | long text | relevance scoring, highlighting |
| `authors` | keyword, **multi-valued** | exact match, faceting |
| `categories` | keyword, **multi-valued** | real faceting — `cs.DB`, `cs.IR`, `cs.LG` |
| `update_date` | date | range filter, recency boosting |
| `version_count` | integer | range filter, sort-by-field |
| `doi` | keyword | stored-not-indexed field |

The multi-valued keyword fields are why this beats a single-author corpus: faceting over
`categories` is genuinely interesting — a paper belongs to several, so the counts don't sum to the
result count, which is exactly the behaviour you need to understand before discussing facets in an
interview.

Why a downloaded snapshot rather than an API: **reproducibility**. Fetching from an API means a slow
paginated script, rate limits, and a corpus that differs between runs — so today's numbers can't be
compared with next week's. A snapshot fixes all three.

Cost: ~4 GB raw, and Kaggle needs an account plus an API token at `~/.kaggle/kaggle.json` before
`kaggle datasets download` works. Put that in the README; it will block anyone cloning the repo.
Gitignore the data and regenerate from the script, the same way `geo/points.csv` works.

Smaller alternative if 4 GB is annoying: `rmisra/news-category-dataset` (~210k docs, clean single
category facet).

### 2. The relevance harness — one small BEIR set

[BEIR](https://github.com/beir-cellar/beir) datasets (HuggingFace, not Kaggle) ship three files:
a corpus, a set of queries, and **qrels** — human judgements of "document D is relevant to query Q".

Use **SciFact** (~5k docs, ~300 queries) or **NFCorpus** (~3.6k docs). Both download in seconds.

This is not a benchmark corpus and is not where latency gets measured. It exists so that
`recall@10` and `nDCG@10` are computed against judgements someone else made. Hand-labeling your own
query set is slow and quietly biased — you will label in favour of whichever engine you built first.

Both datasets normalize to the same `docs.jsonl` shape, and every engine loader reads that file. No
per-engine dataset drift.

## Query suite

Identical queries across all six engines, run against the arXiv corpus. This is where the
differences actually appear.

| # | Query | What it reveals |
|---|---|---|
| 1 | Single term `retrieval` | Baseline latency, baseline scoring |
| 2 | Phrase `"attention mechanism"` | Whether positions are indexed |
| 3 | Boolean `retrieval AND (dense OR sparse) NOT image` | Query DSL expressiveness |
| 4 | Prefix `quant*` | Autocomplete strategy (n-grams vs prefix query vs suggester) |
| 5 | Fuzzy `transfomer~` | Edit-distance support; Postgres needs `pg_trgm` |
| 6 | Filter + full-text (`update_date >= 2023` AND match) | Filter/query separation, filter caching |
| 7 | Facet: top `categories` for a query | Postgres has no real answer here. Multi-valued, so counts exceed the result count |
| 8 | Sort by relevance vs sort by `update_date` | Scoring vs doc-value sorting |
| 9 | Deep pagination: page 1 vs page 500 | Exposes the deep-paging problem; `search_after` vs `from/size` |
| 10 | Highlighting snippets from `abstract` | Stored fields, term vectors |
| 11 | Multi-field with boost (`title^5`, `abstract^1`) | Field weighting; `edismax` / `multi_match` / Redis `WEIGHT` |

Query 11 is worth running last and comparing carefully — every engine here expresses "title matters
five times more than body" differently, and the results diverge more than the syntax suggests.

## What each stage teaches

### 1. Postgres

`to_tsvector`, `tsquery`, `websearch_to_tsquery`, GIN vs GiST, generated stored column vs
expression index, `ts_rank` vs `ts_rank_cd`, `pg_trgm` for fuzzy.

The lesson is the ceiling: no BM25, no faceting, ranking is weak — but it is transactional, joins
with your relational data, and needs no new service. That tradeoff *is* the interview answer.

### 2. Redis

`FT.CREATE idx ON HASH PREFIX 1 doc: SCHEMA ...` — the schema is a *view over documents that already
exist*, which is the mental model to get right. You keep writing plain hashes or JSON; the index
updates synchronously as you write.

Field types map onto the query suite directly:

| Type | Query syntax | Notes |
|---|---|---|
| `TEXT` | `@title:(rust database)` | Stemmed and analyzed. `WEIGHT` boosts a field at index time. |
| `TAG` | `@author:{alice}` | Exact match, no analysis — Redis' equivalent of `keyword`. Braces, not parens. |
| `NUMERIC` | `@points:[100 +inf]` | Range filter, `SORTABLE` to sort on it. |
| `GEO` | `@loc:[77.59 12.97 5 km]` | Same engine as the geo POC's other half. |
| `VECTOR` | `*=>[KNN 10 @vec $blob]` | HNSW or FLAT, in the same index as the text fields. |

Things worth learning here specifically:

- **Scoring is a choice, not a default.** `FT.SEARCH` ranks with TFIDF unless told otherwise;
  `SCORER BM25` (and `BM25STD` on Redis 8) switches it. Compare the two on the same query — most
  people have never seen the difference on real documents.
- **`FT.AGGREGATE`** does faceting with `GROUPBY` / `REDUCE`, a genuinely different pipeline from
  `FT.SEARCH`. This is what Postgres cannot do at all.
- **Prefix `elas*` and fuzzy `%kubernets%`** are query-syntax level, no separate index needed.
- **`DIALECT`** changes query parsing semantics between versions. A real operational footgun.
- **The index lives in RAM**, is rebuilt from RDB/AOF on restart, and `MAXSEARCHRESULTS` caps deep
  paging. Those three facts are the honest limits.

The lesson to carry out: Redis gives you BM25, faceting, filtering and vector search in a single
round trip against data you were already storing there — at the cost of holding the whole index in
memory, with no sharding in open-source Redis. That tradeoff sits precisely between Postgres FTS
and Elasticsearch, which is why it is worth having a measured opinion on.

### 3. Lucene, raw

No server. `IndexWriter`, `Document`, `Field`, `IndexSearcher`, `QueryParser` — Java directly (or
PyLucene).

Highest learning payoff in the whole POC. Segments, merge policy, postings, term dictionary, norms,
the BM25 formula, and the analyzer chain (tokenizer + token filters). Do this stage properly and the
next three become configuration exercises.

### 4. Elasticsearch

Mapping vs analysis. `text` vs `keyword`. `match` vs `term` (the single most common gotcha —
`term` does not analyze the input, so it silently misses on `text` fields). Bool query DSL,
aggregations, shards/replicas, refresh interval, `search_after`.

### 5. OpenSearch

Deliberately a short stage. Point it at the same queries, show the API is nearly identical, and
document precisely where it diverges. Brevity here is the finding.

### 6. Solr

Same Lucene, different culture. Schema-first config, cores vs collections, SolrCloud + ZooKeeper
(versus Elasticsearch's built-in coordination), `dismax`/`edismax`.

## Phase 2 — embeddings and hybrid search

Same corpus, vector retrieval:

- **pgvector** in Postgres, HNSW index
- **Redis** `VECTOR` field, HNSW — and note it supports a *prefiltered* KNN in one query
  (`@points:[100 +inf]=>[KNN 10 @vec $blob AS score]`), which is exactly the filter-then-search
  problem every vector database has to solve
- **Elasticsearch** `dense_vector` + kNN search
- **OpenSearch** k-NN plugin

Embed locally with `sentence-transformers/all-MiniLM-L6-v2` (384 dims, CPU-friendly).

Then build **hybrid search**: run BM25 and vector retrieval in parallel, fuse with Reciprocal Rank
Fusion. This is the current production answer and the strongest note to end an interview on —
keyword search nails exact/rare terms, vectors nail paraphrase, RRF needs no score normalization.

## Metrics to record

Two tables, because the two datasets answer different questions. Keeping them separate is the point
— reporting a relevance score next to a latency measured on a different corpus is how benchmarks
become misleading.

### Performance — on the arXiv corpus

| Metric | Why |
|---|---|
| Index build time | Ingest cost, and whether it's single-threaded |
| Index size on disk (or in RAM, for Redis) | Storage multiplier vs raw corpus |
| p50 / p95 query latency, **per query type** | Warm cache. A single average hides that fuzzy costs 20x what a term match does |
| Matching cost vs fetching cost | Run each query as a count-only and as a full fetch. The geo POC found Elasticsearch spent 3ms matching and 85ms returning rows — that split is invisible in a latency number |
| Top-10 result diff between engines | Qualitative, and the most interesting column. Same query, same corpus, different documents |

### Relevance — on the BEIR set

| Metric | Why |
|---|---|
| **nDCG@10** | The standard IR measure. Rewards putting relevant documents higher, not merely including them. This is the number to lead with |
| **recall@10** | Did the relevant documents appear at all, regardless of order |
| **MRR** | How far down is the first relevant result — the metric that matches how people actually use a search box |
| Per-engine, per-scorer | Run Redis under both `TFIDF` and `BM25`. The delta on the same corpus is the clearest possible demonstration of what BM25 buys |

The comparison that makes the whole POC worth doing: **Postgres `ts_rank` (no IDF) vs BM25
everywhere else, on the same queries with the same judgements.** That turns "Postgres ranking is
worse" from something you read into a number you measured.

Latency alone misleads. Two engines can both answer in 5ms and return different documents — that
difference is the actual subject of this POC.

## Proposed layout

```
search/
  docker-compose.yml        # postgres(+pgvector), redis, elasticsearch, opensearch, solr
  fetch_arxiv.py            # kaggle download + subset to ~200k CS papers -> docs.jsonl
  fetch_beir.py             # SciFact/NFCorpus -> docs.jsonl + queries.jsonl + qrels.tsv
  common.py                 # Doc model, timing harness, result comparison
  pg_search.py              # tsvector + GIN, pg_trgm, pgvector
  redis_search.py           # FT.CREATE schema, FT.SEARCH, FT.AGGREGATE, VECTOR field
  lucene_raw/               # Java (or PyLucene) — direct Lucene, no server
  es_search.py
  opensearch_search.py
  solr_search.py
  embed.py                  # sentence-transformers -> vectors
  hybrid.py                 # BM25 + vector, RRF fusion
  compare.py                # one query -> all engines -> latency + top-10 diff
  evaluate.py               # BEIR qrels -> nDCG@10, recall@10, MRR per engine
  README.md                 # results and analysis
```

## Build order

1. Both fetch scripts + Postgres FTS — fastest win, familiar ground
2. `evaluate.py` against the BEIR set, with Postgres as the only engine — get the relevance harness
   working while there is exactly one thing that can be wrong with it. Every later engine then
   plugs into a scorer you already trust
3. Redis — second-fastest win, and the first engine here with real BM25 and real faceting. Run it
   under both `TFIDF` and `BM25` through `evaluate.py`
4. Raw Lucene — hardest, highest payoff; do it while motivated
5. Elasticsearch
6. OpenSearch + Solr — quick once Elasticsearch is done
7. Embeddings + hybrid search, scored with the same harness — the RRF fusion either beats both
   inputs on nDCG@10 or it doesn't, and now you can tell
