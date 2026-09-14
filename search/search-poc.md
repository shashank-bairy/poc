# Search POC — Postgres FTS, Lucene, Elasticsearch, OpenSearch, Solr (+ embeddings)

## Goal

Understand five search approaches by running the **same corpus and the same query suite** through
each one, then comparing index size, latency, and — more importantly — result quality.

The framing that matters:

- **Lucene** is the engine. A Java library. Inverted index, postings lists, BM25, analyzers.
- **Elasticsearch**, **OpenSearch**, **Solr** are three servers wrapping that same Lucene.
  They differ in API, cluster coordination, and licensing — not in core retrieval.
- **Postgres full-text search** is a genuinely separate implementation: `tsvector` + GIN index.
  Weaker ranking, no faceting, but zero extra infrastructure and it joins with your real data.

So this is *one library, three wrappers, and one outsider* — not five peers. Interviewers probe
exactly this distinction.

## Background: how each system indexes text

| System | Indexing approach | Notes |
|---|---|---|
| **Postgres** | GIN index over `tsvector` | `to_tsvector` normalizes/stems into lexemes; `tsquery` matches. `ts_rank` is frequency-based, not BM25. `pg_trgm` adds fuzzy/typo matching via trigram GIN. |
| **Lucene (raw)** | Inverted index in immutable segments | `IndexWriter` -> segments -> background merge. Term dictionary + postings + norms. BM25 default since Lucene 6. Everything below is a REST veneer on this. |
| **Elasticsearch** | Lucene + JSON REST + sharding | Mapping (field types) vs analysis (analyzer chain). `text` vs `keyword` dual-field is the central concept. Built-in cluster coordination. |
| **OpenSearch** | Fork of Elasticsearch 7.10 | ~95% identical API. Diverges on licensing and plugins — k-NN vector search is built in and free. |
| **Solr** | Lucene + schema-first + SolrCloud | `managed-schema`, cores vs collections, ZooKeeper for coordination, `edismax` query parser. Historically the strongest faceting story. |

Core idea behind all of them: an inverted index maps *term -> list of documents containing it*, so a
text query reduces to intersecting/unioning sorted integer lists, then scoring the survivors.

## Dataset

One corpus, all engines. Target ~100k–500k documents with text, keyword, and numeric fields so every
query type is exercised.

**Hacker News posts** (free Algolia/Firebase API) is the best fit:

| Field | Type | Exercises |
|---|---|---|
| `title` | short text | term/phrase match, autocomplete |
| `text` | long text | relevance scoring, highlighting |
| `author` | keyword | exact term match, faceting |
| `points` | integer | range filter, sort-by-field |
| `created_at` | timestamp | date range, recency boosting |
| `url` | keyword | stored-not-indexed field |

Alternatives: Wikipedia abstracts dump, Amazon product reviews.

Output a single `docs.jsonl` — every engine loader reads that same file. No per-engine dataset drift.

## Query suite

Identical ten queries across all five engines. This is where the differences actually appear.

| # | Query | What it reveals |
|---|---|---|
| 1 | Single term match | Baseline latency, baseline scoring |
| 2 | Phrase `"distributed systems"` | Whether positions are indexed |
| 3 | Boolean `rust AND (database OR storage) NOT mongodb` | Query DSL expressiveness |
| 4 | Prefix `elas*` | Autocomplete strategy (n-grams vs prefix query vs suggester) |
| 5 | Fuzzy `kubernets~` | Edit-distance support; Postgres needs `pg_trgm` |
| 6 | Filter + full-text (`points > 100` AND match) | Filter/query separation, filter caching |
| 7 | Facet: top authors for a query | Postgres has no real answer here |
| 8 | Sort by relevance vs sort by `points` | Scoring vs doc-value sorting |
| 9 | Deep pagination: page 1 vs page 500 | Exposes the deep-paging problem; `search_after` vs `from/size` |
| 10 | Highlighting snippets | Stored fields, term vectors |

## What each stage teaches

### 1. Postgres

`to_tsvector`, `tsquery`, `websearch_to_tsquery`, GIN vs GiST, generated stored column vs
expression index, `ts_rank` vs `ts_rank_cd`, `pg_trgm` for fuzzy.

The lesson is the ceiling: no BM25, no faceting, ranking is weak — but it is transactional, joins
with your relational data, and needs no new service. That tradeoff *is* the interview answer.

### 2. Lucene, raw

No server. `IndexWriter`, `Document`, `Field`, `IndexSearcher`, `QueryParser` — Java directly (or
PyLucene).

Highest learning payoff in the whole POC. Segments, merge policy, postings, term dictionary, norms,
the BM25 formula, and the analyzer chain (tokenizer + token filters). Do this stage properly and the
next three become configuration exercises.

### 3. Elasticsearch

Mapping vs analysis. `text` vs `keyword`. `match` vs `term` (the single most common gotcha —
`term` does not analyze the input, so it silently misses on `text` fields). Bool query DSL,
aggregations, shards/replicas, refresh interval, `search_after`.

### 4. OpenSearch

Deliberately a short stage. Point it at the same queries, show the API is nearly identical, and
document precisely where it diverges. Brevity here is the finding.

### 5. Solr

Same Lucene, different culture. Schema-first config, cores vs collections, SolrCloud + ZooKeeper
(versus Elasticsearch's built-in coordination), `dismax`/`edismax`.

## Phase 2 — embeddings and hybrid search

Same corpus, vector retrieval:

- **pgvector** in Postgres, HNSW index
- **Elasticsearch** `dense_vector` + kNN search
- **OpenSearch** k-NN plugin

Embed locally with `sentence-transformers/all-MiniLM-L6-v2` (384 dims, CPU-friendly).

Then build **hybrid search**: run BM25 and vector retrieval in parallel, fuse with Reciprocal Rank
Fusion. This is the current production answer and the strongest note to end an interview on —
keyword search nails exact/rare terms, vectors nail paraphrase, RRF needs no score normalization.

## Metrics to record

| Metric | Why |
|---|---|
| Index build time | Ingest cost, and whether it's single-threaded |
| Index size on disk | Storage multiplier vs raw corpus |
| p50 / p95 query latency | Warm cache, per query type — not just an average |
| recall@10 vs ground truth | Hand-label a small query set; the only relevance number that means anything |
| Top-10 result diff between engines | Qualitative. The most interesting column. |

Latency alone misleads. Two engines can both answer in 5ms and return different documents — that
difference is the actual subject of this POC.

## Proposed layout

```
search/
  docker-compose.yml        # postgres(+pgvector), elasticsearch, opensearch, solr
  generate_data.py          # fetch HN -> docs.jsonl
  common.py                 # Doc model, timing harness, result comparison
  pg_search.py              # tsvector + GIN, pg_trgm, pgvector
  lucene_raw/               # Java (or PyLucene) — direct Lucene, no server
  es_search.py
  opensearch_search.py
  solr_search.py
  embed.py                  # sentence-transformers -> vectors
  hybrid.py                 # BM25 + vector, RRF fusion
  compare.py                # one query -> all engines -> latency + top-10 diff
  README.md                 # results and analysis
```

## Build order

1. Dataset generator + Postgres FTS — fastest win, familiar ground
2. Raw Lucene — hardest, highest payoff; do it while motivated
3. Elasticsearch
4. OpenSearch + Solr — quick once Elasticsearch is done
5. Embeddings + hybrid search
