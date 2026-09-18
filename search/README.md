# Search POC — six stores, one corpus, the same eleven queries

Six ways to search the same 200,000 arXiv papers, written so the query syntax
of each engine is readable directly. One library, three wrappers, two
outsiders: **Lucene** is the engine, **Elasticsearch** / **OpenSearch** /
**Solr** are servers wrapped around that same library, and **Postgres FTS**
(`tsvector` + GIN) and **Redis** (its own in-memory inverted index) are
separate implementations.

## Start here

**Open any file in [`engines/`](./engines).** Each is a flat list of functions —
`q1_term()`, `q2_phrase()`, … `q11_boosted()` — with the query written out
inside, in that engine's own syntax. No dicts, no config files, no lookup by
name:

```python
def q5_fuzzy(self):
    # Trigrams, not lexemes: tsvector cannot do edit distance at all,
    # so this goes through a completely separate index (pg_trgm).
    return self.search("""
        SELECT id, word_similarity('transfomer', title) AS score, title, '' AS hl
        FROM docs
        WHERE 'transfomer' <% title
        ORDER BY score DESC
        LIMIT 10
    """)
```

Run any engine on its own. It indexes a small corpus, prints **what that engine
physically stored** for one document, then every query with its source and its
results:

```bash
uv run python -m engines.postgres     # or .redis .lucene .elasticsearch .opensearch .solr
```

Captured output for all six is in [`results/`](./results) if you want to read
before running anything.

## What each store actually holds

That first block is the part worth comparing. The same document, six shapes:

| engine | stored as | searched against |
|---|---|---|
| postgres | the row: `text`, `text[]`, `date` | a generated `tsvector` column in the same row |
| redis | a HASH at `doc:<id>` — no arrays, no dates | a separate index Redis maintains over the key prefix |
| lucene | stored fields, verbatim | the terms the `Analyzer` emitted |
| elasticsearch | `_source`, the JSON you sent | per-field analyzer output (`text` stems, `keyword` does not) |
| opensearch | same as Elasticsearch | same as Elasticsearch |
| solr | the stored document | `text_en`, a chain you can print stage by stage |

Two things in every one of them: the original text, kept so it can be shown
back to you, and a derived form that is the only thing a query can match. The
demo prints both side by side.

## Running it

```bash
./run.sh                      # services, jars, then all six engines in turn
```

Or by hand:

```bash
docker compose up -d
uv sync
./lucene_raw/fetch_jars.sh    # Lucene + gson from Maven Central
uv run python -m corpora.arxiv --limit 200000
uv run python -m engines.postgres
```

The corpus needs a Kaggle account and an API token, so it is first here rather
than buried:

```bash
# Kaggle -> Settings -> API -> Create New Token, then chmod 600 the file:
#   ~/.kaggle/kaggle.json     (username + key), or
#   ~/.kaggle/access_token    (the newer KGAT_ form)
uv run python -m corpora.arxiv --limit 200000   # 1.8 GB download, CS subset
```

Vector search (`vector_search()` on every engine) needs embeddings, which pull
in ~2 GB of torch, so they are opt-in:

```bash
uv sync --extra embed
uv run python -m core.embed --limit 20000
```

Ports are offset from the geo POC's so both can run at once: Postgres 55433,
Redis 6381, Elasticsearch 9202, OpenSearch 9203, Solr 8984. Data and the Lucene
jars are gitignored and regenerate from the scripts.

## Measured, once, on 200k documents

The benchmark harness has been removed — this POC is about the data model and
the query syntax, not the measurement. The numbers it produced are kept here as
a record, with the full write-up in [`FINDINGS.md`](./FINDINGS.md):

| engine | build · 200k | index size | 1-term p50 | 4-prefix p50 |
|---|---|---|---|---|
| postgres | 50.7s | 108 MB (0.52x) | 12.02 ms | 114.24 ms |
| redis | 31.3s | 220 MB RAM (1.06x) | 2.59 ms | 4.29 ms |
| lucene | 14.8s | 190 MB (0.91x) | 5.63 ms | 1.74 ms |
| elasticsearch | 34.8s | 230 MB (1.10x) | 2.59 ms | 5.32 ms |
| opensearch | 31.3s | 231 MB (1.11x) | 3.46 ms | 4.07 ms |
| solr | 20.6s | 212 MB (1.02x) | 1.65 ms | 1.93 ms |

Postgres is 10-70x slower at 200k on any query touching many rows, and that is
structural, not tuning. Elasticsearch and OpenSearch land within 1 MB of each
other, because they are identical Lucene over identical documents.

## Layout

```
engines/postgres.py        q1_term() ... q11_boosted(), SQL inside each    <- read these
engines/redis.py           the same eleven, FT.SEARCH commands inside
engines/lucene.py          the same eleven, QueryParser strings inside
engines/elasticsearch.py   the same eleven, request bodies inside
engines/opensearch.py      inherits all eleven unchanged; only vector_search differs
engines/solr.py            the same eleven, select params inside

docker-compose.yml         postgres(+pgvector), redis 8, elasticsearch, opensearch, solr
core/common.py             Doc, corpus loading, the per-engine demo runner
core/embed.py              all-MiniLM-L6-v2 -> vectors, for vector_search()
corpora/arxiv.py           kaggle snapshot -> CS subset -> data/arxiv/docs.jsonl
lucene_raw/Search.java     raw Lucene: IndexWriter, analyzers, facets, QueryParser
results/                   captured output from each engine

WALKTHROUGH.md             one document + one query traced through all six engines
FINDINGS.md                how each store behaves, and the traps
HOW-IT-WORKS.md            inverted indexes, analysis, segments -- the theory
```

The eleven function names are identical across all six files, so `diff` shows
the same question in six dialects.

Two deviations worth knowing: the Lucene driver sits in `engines/` with the
other five while only the Java lives in `lucene_raw/`; and PyLucene was skipped
in favour of a small JSON-over-HTTP JVM process, because a JCC build is a day
of work that teaches nothing about Lucene. That bridge adds ~0.3-0.8 ms per
query and is the only engine here whose latency includes a transport it does
not need.
