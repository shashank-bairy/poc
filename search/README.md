# Search POC — Postgres FTS, Redis, raw Lucene, Elasticsearch, OpenSearch, Solr (+ hybrid)

Design brief: [`search-poc.md`](./search-poc.md). Mechanics in plain language:
[`HOW-IT-WORKS.md`](./HOW-IT-WORKS.md). Measurements, per-engine behaviour and
the traps: [`FINDINGS.md`](./FINDINGS.md).

One library, three wrappers, two outsiders. **Lucene** is the engine;
**Elasticsearch**, **OpenSearch** and **Solr** are servers around that same
library; **Postgres FTS** (`tsvector` + GIN) and **Redis** (its own in-memory
inverted index) are separate implementations. The POC runs the same corpus and
the same eleven-query suite through all six, then scores relevance against human
judgements rather than opinion.

## Running it

```bash
./run.sh                      # services, jars, BEIR data, both comparison passes
```

Or by hand:

```bash
docker compose up -d
uv sync
./lucene_raw/fetch_jars.sh              # Lucene + gson from Maven Central
uv run python -m corpora.beir           # SciFact: corpus, queries, judgements
uv run python -m bench.compare --docs data/beir/scifact/docs.jsonl   # performance
uv run python -m bench.evaluate                                      # relevance
```

The arXiv corpus needs a Kaggle account and an API token — this will block a
fresh clone, so it is first here rather than buried:

```bash
# Kaggle -> Settings -> API -> Create New Token, then chmod 600 the file:
#   ~/.kaggle/kaggle.json     (username + key), or
#   ~/.kaggle/access_token    (the newer KGAT_ form)
uv run python -m corpora.arxiv --limit 200000   # 1.8 GB download, CS subset
uv run python -m bench.compare                  # the real performance pass
```

Phase 2 pulls in ~2 GB of torch, so it is opt-in:

```bash
uv sync --extra embed
uv run python -m bench.evaluate --engines elasticsearch,dense,hybrid
```

Ports are offset from the geo POC's so both can run at once: Postgres 55433,
Redis 6381, Elasticsearch 9202, OpenSearch 9203, Solr 8984. Data and the Lucene
jars are gitignored and regenerate from the scripts.

## Results

Full measurements, per-engine behaviour and the traps hit along the way:
**[`FINDINGS.md`](./FINDINGS.md)**. Raw output in `results/`.

Performance is measured on 200k arXiv CS papers, relevance on BEIR SciFact
(arXiv ships no relevance judgements). The headline:

| engine | nDCG@10 · SciFact | build · 200k | q11 boosted p50 | index size |
|---|---|---|---|---|
| postgres | 0.3620 | 53.5s | 234.98 ms | 108 MB (0.52x) |
| redis (bm25std) | 0.6549 | 37.3s | 16.84 ms | 220 MB RAM (1.06x) |
| lucene | 0.6684 | 15.3s | 3.35 ms | 190 MB (0.91x) |
| elasticsearch | 0.6684 | 45.9s | 4.23 ms | 230 MB (1.10x) |
| opensearch | 0.6684 | 35.8s | 3.34 ms | 182 MB (0.87x) |
| solr | 0.6668 | 21.6s | 2.22 ms | 212 MB (1.02x) |

Five things that came out of it:

1. Raw Lucene, Elasticsearch and OpenSearch score **identically** to four
   decimals. The REST layer changes nothing about retrieval.
2. Redis' scorer flag is worth **0.58 nDCG** — TFIDF (the default) 0.076 vs
   BM25STD 0.655, same index, one parameter.
3. Postgres ranks at 0.36 because `ts_rank` has no IDF term, and at 200k docs
   it is 10-70x slower on any query touching many rows.
4. Postgres faceting *works* (12 ms, correct counts). What it lacks is a facet
   engine: cost tracks the match count instead of staying flat.
5. Hybrid RRF beats both its inputs — BM25 0.668, dense 0.647, fused **0.708**.

## Layout

```
docker-compose.yml       postgres(+pgvector), redis 8, elasticsearch, opensearch, solr
core/common.py           Doc, the structural Query suite, SearchEngine protocol, timing
core/embed.py            all-MiniLM-L6-v2 -> vectors
corpora/arxiv.py         kaggle snapshot -> CS subset -> data/arxiv/docs.jsonl
corpora/beir.py          SciFact/NFCorpus -> docs.jsonl + queries.jsonl + qrels.tsv
engines/postgres.py      tsvector + GIN, pg_trgm, pgvector
engines/redis.py         FT.CREATE, FT.SEARCH, FT.AGGREGATE, VECTOR with prefiltered KNN
engines/lucene.py        driver for the JVM below
engines/elasticsearch.py mapping vs analysis; every query body lives here
engines/opensearch.py    the same bodies, a different client
engines/solr.py          schema API, edismax, fq, facet.field
engines/hybrid.py        RRF fusion, plus a dense-only baseline
lucene_raw/Search.java   raw Lucene: IndexWriter, analyzers, BM25, facets, highlighting
lucene_raw/fetch_jars.sh
bench/compare.py         one corpus -> all engines -> latency + top-10 diff
bench/evaluate.py        BEIR qrels -> nDCG@10, recall@10, MRR
results/                 captured output from the runs quoted above
```

Everything runs as a module (`python -m bench.compare`) so the packages import
cleanly; `corpora`, not `datasets`, because HuggingFace `datasets` arrives with
the embedding extra and would shadow it.

Two deviations from the brief's proposed layout, both deliberate: the Lucene
driver sits in `engines/` with the other five while only the Java lives in
`lucene_raw/`; and PyLucene was skipped in favour of a small JSON-over-HTTP JVM
process, because a JCC build is a day of work that teaches nothing about
Lucene. That bridge adds ~0.3-0.8 ms per query and is the only engine here
whose latency includes a transport it does not need.

What was left undone, and why, is at the end of [`FINDINGS.md`](./FINDINGS.md).
