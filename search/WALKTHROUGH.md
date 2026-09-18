# Walkthrough — what actually happens on save and on search

One document, one query, six engines, followed all the way down.

Every box and token list in this file was printed by a real command
against the running stack, not sketched from memory. The commands are shown so
you can re-run them.

Read [`README.md`](./README.md) for how to start things, [`FINDINGS.md`](./FINDINGS.md)
for the measurements, and [`HOW-IT-WORKS.md`](./HOW-IT-WORKS.md) for the theory
behind the words used here.

---

## The example

One real paper out of the arXiv corpus:

```
id            0705.0751
title         Approximate textual retrieval
abstract      An approximate textual retrieval algorithm for searching sources
              with high levels of defects is presented. It considers splitting
              the words in a query into two overlapping segments and ...
categories    cs.IR, cs.DL          <- two of them; this matters for faceting
update_date   2007-05-23
```

And one query — the simplest possible, so the machinery is visible rather than
the cleverness:

> **find documents about `retrieval`**

Set it up yourself with:

```bash
for e in postgres redis lucene elasticsearch opensearch solr; do
  uv run python -m "engines.$e"
done
```

---

## The one idea behind all six

Every engine here does the same two things. The differences are *where* each
step happens and *what it is called*.

```
SAVE                                    SEARCH

  "Approximate textual retrieval"         "retrieval"
            |                                  |
            v                                  v
      +-----------+                      +-----------+
      | ANALYZE   |  same chain, and     | ANALYZE   |
      | text ->   |  it MUST be the      | text ->   |
      | terms     |  same one <--------> | terms     |
      +-----------+                      +-----------+
            |                                  |
            v                                  v
      approxim                             retriev
      textual                                  |
      retriev  <------ these must match -------+
            |                                  |
            v                                  v
   +------------------+              +------------------+
   | INVERTED INDEX   |              | LOOK UP the term, |
   | retriev -> [266, |------------->| get its doc list  |
   |             411] |              | then SCORE them   |
   +------------------+              +------------------+
```

The single most common bug in all of search lives in that middle arrow: if the
text was stored as `retriev` and you search for `Retrieval`, you get nothing.
The query was never wrong — it just never went through the same analyzer.

**Analyzing** means: split into words, lowercase, drop stopwords, chop to
stems. Solr can print each step, and since Solr, Lucene, Elasticsearch and
OpenSearch all use the same `EnglishAnalyzer`, this is what all four do:

```bash
curl -s 'localhost:8984/solr/papers/analysis/field?analysis.fieldname=abstract\
&analysis.fieldvalue=Approximate%20textual%20retrieval&wt=json'
```
```
StandardTokenizer          ['Approximate', 'textual', 'retrieval']   split on word boundaries
StopFilter                 ['Approximate', 'textual', 'retrieval']   drop the/of/and (none here)
LowerCaseFilter            ['approximate', 'textual', 'retrieval']   case folded
EnglishPossessiveFilter    ['approximate', 'textual', 'retrieval']   strip 's
PorterStemFilter           ['approxim',    'textual', 'retriev']     <- stems, and this is
                                                                        what lands on disk
```

Postgres does the same job with different spelling (`to_tsvector`), and Redis
does it inside its own C code. Nobody stores the word you typed.

---

## 1. Postgres — [`engines/postgres.py`](./engines/postgres.py)

### Save

```
  Doc(id='0705.0751', title='Approximate textual retrieval', ...)
        |
        |  index()                                       postgres.py:224
        v
  1. cur.execute(SCHEMA)          create the table
        |
        v
  2. execute_values(INSERT ...)   plain INSERT of 3,000 rows
        |                         *no index exists yet* -- deliberate
        v
  3. GENERATED ALWAYS AS (...)    Postgres computes the tsvector per row,
        |                         at write time, and stores it in a column
        v
  4. cur.execute(INDEXES)         NOW build the GIN index, once, over the
                                  finished table
```

Step 2-then-4 is the point: building one GIN index over a full table is far
cheaper than updating an index 3,000 times.

What the `tsv` column holds for our document:

```bash
docker exec search-postgres psql -U postgres -d search \
  -c "SELECT tsv FROM docs WHERE id='0705.0751'"
```
```
'approxim':1A,5B  'retriev':3A,7B,58B  'textual':2A,6B  'algorithm':8B
'defect':16B,54B  'search':10B  'sourc':11B,53B  ...
    ^        ^  ^
    |        |  +-- B = it came from the abstract (A = from the title)
    |        +----- positions in the text: word 3 of the title, words 7 and 58
    +--------------- the stem, not the word
```

So one row stores: stem, every position it appears at, and a letter saying
which field it came from. The `A`/`B` letters are how `q11_boosted` can weight
the title 5x *at query time* without reindexing.

### Search

```
  q1_term()                                             postgres.py:32
        |
        |  one literal SQL string
        v
  SELECT id, ts_rank(tsv, q) AS score, title
  FROM docs, plainto_tsquery('english','retrieval') AS q
  WHERE tsv @@ q
  ORDER BY score DESC LIMIT 10
        |
        v
  self.search(sql)  ->  psycopg2  ->  Postgres
```

Inside Postgres, the plan says exactly what happened:

```bash
docker exec search-postgres psql -U postgres -d search \
  -c "EXPLAIN ANALYZE SELECT ... WHERE tsv @@ q ORDER BY score DESC LIMIT 10"
```
```
Limit
  Sort  (Sort Key: ts_rank(...) DESC)             <- 4. order the survivors
    Bitmap Heap Scan on docs                      <- 3. go fetch those rows
      Recheck Cond: (tsv @@ 'retriev'::tsquery)
      Bitmap Index Scan on docs_tsv_gin           <- 2. ask GIN for doc ids
        Index Cond: (tsv @@ 'retriev'::tsquery)   <- 1. 'retrieval' became 'retriev'
```

Bottom-to-top, that is the whole story:

1. `plainto_tsquery` stems your word to `retriev` — the same stem the save path
   produced.
2. GIN hands back the 30 matching rows.
3. The heap scan reads those rows off disk.
4. `ts_rank` orders them, and only then are the top 10 kept.

**The thing worth noticing:** step 4 has to read the `tsv` column of every one
of the 30 rows to rank them. On a query matching 74,902 rows, that is 74,902
column reads — which is why Postgres takes 235 ms on query 11 where Lucene
takes 3 ms. Lucene orders hits from the postings it is already walking and
never touches a stored value until the final ten.

---

## 2. Redis — [`engines/redis.py`](./engines/redis.py)

### Save

The mental model that trips everyone: `FT.CREATE` does **not** create a box you
put documents into. It creates a *standing view* over hashes that already exist
(or will exist) under a key prefix.

```
  1. FT.CREATE idx:docs ON HASH PREFIX 1 doc: SCHEMA title TEXT ...
        |
        |  "watch every key starting with doc:, and index these fields"
        v
  2. HSET doc:0705.0751 title "Approximate textual retrieval"
                        abstract "An approximate textual ..."
                        categories "cs.IR|cs.DL"
                        update_date 13656
        |
        |  you are writing an ORDINARY HASH. The index updates inside
        |  this same command -- there is no separate "index" step.
        v
  3. the inverted index now lives in RAM alongside the hash
```

Both steps are in `index()` (redis.py:234). The document as stored:

```bash
docker exec search-redis redis-cli HGETALL doc:0705.0751
```
```
title        Approximate textual retrieval
abstract     An approximate textual retrieval algorithm for searching ...
categories   cs.IR|cs.DL        <- one string, pipe-separated, not a list
update_date  13656              <- epoch DAYS. Redis has no date type,
                                   only NUMERIC. 13656 = 2007-05-23
doi          (empty)            <- in the hash but NOT in the schema,
                                   so it is returned but never searchable
```

Two consequences fall straight out of that storage shape:

* `update_date` being a plain number is why `q6_filter_text` says
  `@update_date:[19358 +inf]` instead of a date literal.
* `categories` being *one string* is why `q7_facet` has to split and re-add the
  counts in Python: Redis groups by `"cs.IR|cs.DL"` as a single bucket.

### Search

```
  q1_term()                                              redis.py:43
        |
        |  one literal command, exactly as you would type it in redis-cli
        v
  FT.SEARCH idx:docs 'retrieval' SCORER BM25STD WITHSCORES LIMIT 0 10 DIALECT 2
        |
        |  self.search() -> self.run()                   redis.py:175
        |  shlex.split() turns it into argv, like a shell would
        v
  execute_command('FT.SEARCH', 'idx:docs', 'retrieval', 'SCORER', ...)
        |
        v
  Redis parses, expands, looks up, scores -- all in RAM, one round trip
```

Redis will show you its parse:

```bash
docker exec search-redis redis-cli FT.EXPLAIN idx:docs retrieval DIALECT 2
```
```
UNION {
  retrieval              <- the word exactly as typed
  +retriev(expanded)     <- the stem
  retriev(expanded)
}
```

It searches for the raw word *and* the stem, then unions the results. And the
index itself:

```bash
docker exec search-redis redis-cli FT.INFO idx:docs
```
```
num_docs      3000        the documents
num_terms     22835       distinct stems in the dictionary
num_records   323077      (term, document) pairs -- the postings
inverted_sz   4.68 MB     all of it in RAM, and rebuilt from RDB/AOF on restart
```

**The thing worth noticing:** `SCORER BM25STD`. Leave it out and you get
TFIDF, which is Redis' default and not what any other engine here uses. One
missing word in that command is the difference between a search box that works
and one that does not.

---

## 3. Raw Lucene — [`engines/lucene.py`](./engines/lucene.py) + [`lucene_raw/Search.java`](./lucene_raw/Search.java)

This is the library the next three all wrap. No server, no REST — just Java
objects.

### Save

```
  Doc(...)
    |
    |  index()                                          lucene.py:154
    |  writes docs.jsonl, then runs: java Search.java index docs.jsonl index/
    v
  ----------------------------- inside the JVM -----------------------------
    IndexWriter writer = new IndexWriter(dir, cfg);
        |
        |  for each document, build a Document of typed Fields:
        v
    new StringField("id", "0705.0751")        one exact term, no analysis
    new TextField("title", "Approximate ...") ANALYZED -> approxim|textual|retriev
    new TextField("abstract", "An approx...") ANALYZED
    new StringField("categories", "cs.IR")    exact, one per value
    new SortedSetDocValuesFacetField(...)     a SECOND copy, columnar, for counting
    new LongPoint("update_date", 13656)       a BKD tree, for range queries
    new NumericDocValuesField("update_date_dv", 13656)   ANOTHER copy, for sorting
    new StoredField("doi", "")                returned, never searched
        |
        v
    writer.addDocument(...)   buffers in RAM
        |
        v
    writer.forceMerge(1)      squash everything into ONE segment
    writer.commit()           make it durable and visible
```

Notice `update_date` is written **twice**, as a `LongPoint` and as a
`NumericDocValuesField`. That is not redundancy — a `LongPoint` answers "is it
after 2023?" and cannot sort, while doc values sort and cannot range-query.
Two questions, two data structures, same number.

What ends up on disk:

```bash
ls -1 lucene_raw/index
```
```
_0.cfs        2.6 MB   segment 0, compound file: the term dictionary, the
                       postings, the stored fields, the doc values -- all of it
_0.cfe          4 KB   the table of contents for that compound file
_0.si           4 KB   segment info
segments_1      4 KB   which segments are live right now
write.lock        0 B  one writer at a time
```

One segment, because we asked for one. Normally there are many: a segment is an
**immutable** mini-index, flushed when the buffer fills, and merged in the
background. "Refresh interval" in Elasticsearch is exactly this, exposed as a
knob.

### Search

```
  q1_term()                                             lucene.py:42
        |
        |  self.search("abstract:retrieval")            lucene.py:123
        v
  POST {"q": "abstract:retrieval", "limit": 10}  ->  the JVM
        |
        v
  ----------------------------- inside the JVM -----------------------------
    QueryParser.parse("abstract:retrieval")
        |  runs the analyzer on your text, so this becomes
        v
    TermQuery(abstract:retriev)
        |
        v
    searcher.search(query, 10)
        |    walks the postings list for 'retriev'
        |    scores each hit with BM25Similarity
        |    keeps a priority queue of the best 10 -- never sorts all 30
        v
    StoredFields.document(docId)   <- only NOW are titles read off disk,
                                      for the 10 survivors only
```

That last step is why `q1_term_count()` exists: counting skips it entirely.
Measured on 200k documents, matching costs 0.81 ms and matching-plus-fetching
costs 1.28 ms.

---

## 4. Elasticsearch — [`engines/elasticsearch.py`](./engines/elasticsearch.py)

Same Lucene as above. What ES adds is JSON over HTTP, a mapping you declare up
front, and cluster machinery.

### Save

```
  Doc(...) -> d.to_json()
        |
        |  index()                                      elasticsearch.py:223
        v
  1. PUT /papers  with MAPPING
        |          field TYPES decided here, before any document arrives:
        |            "abstract": {"type": "text", "analyzer": "english"}   analyzed
        |            "categories": {"type": "keyword"}                     not analyzed
        |            "doi": {"type": "keyword", "index": false}            not searchable
        v
  2. refresh_interval = -1        turn refreshing OFF for the bulk load
        |
        v
  3. _bulk  ->  {"_id": "0705.0751", "_source": {...}}
        |        ES hands each field to Lucene exactly as section 3 described
        v
  4. refresh        NOW make it searchable (this cuts a Lucene segment)
  5. forcemerge     squash to one segment
  6. refresh_interval = 1s        back to normal
```

Step 2 and 4 are the same "segments are immutable" fact from the Lucene
section, wearing a setting's clothes. A document is invisible to search until a
refresh happens — by default up to one second after you wrote it.

`text` vs `keyword` is the whole mapping story, and you can watch the
difference:

```bash
curl -s localhost:9202/papers/_analyze -H 'content-type: application/json' \
  -d '{"field":"abstract","text":"Approximate textual retrieval"}'
# -> ['approxim', 'textual', 'retriev']      three terms, stemmed

curl -s localhost:9202/papers/_analyze -H 'content-type: application/json' \
  -d '{"field":"categories","text":"cs.IR"}'
# -> ['cs.IR']                               ONE term, untouched
```

That is why you can facet on `categories` but not on `abstract`, and why
`match` finds things that `term` cannot.

### Search

```
  q1_term()                                             elasticsearch.py:33
        |
        |  a literal request body
        v
  {"size": 10, "query": {"match": {"abstract": "retrieval"}}}
        |
        |  self.search(body) -> self._search(body)      elasticsearch.py:205
        v
  POST /papers/_search
        |
        v
  ES analyzes "retrieval" -> retriev      <- `match` does this; `term` does NOT
  Lucene walks the postings, scores with BM25, returns the top 10
```

ES can also show you exactly which terms it matched and why a document was
retrieved, without any scoring arithmetic:

```bash
curl -s 'localhost:9202/papers/_validate/query?explain=true' \
  -H 'content-type: application/json' \
  -d '{"query":{"match":{"abstract":"retrieval"}}}'
```
```
"explanation": "abstract:retriev"
```

The `match` you wrote became a term query on the stem. That is the whole
translation, and it is the reason `match` finds this document while a `term`
query for `"retrieval"` finds nothing.

---

## 5. OpenSearch — [`engines/opensearch.py`](./engines/opensearch.py)

The whole file is 69 lines and it defines **no query functions at all**:

```python
class OpenSearchEngine(ElasticEngine):
    ...
    def vector_search(self, vector, k: int = 10):   # the only query it overrides
```

`q1_term` through `q11_boosted` are inherited from Elasticsearch unchanged, and
they work, and the same documents come back in the same order.
Save and search flows: re-read section 4 — they are the same flows, against a
different port, through a client library that still wants `body=`.

What genuinely differs:

```
                   Elasticsearch                  OpenSearch
  vector field     dense_vector                   knn_vector
  vector query     "knn" beside the query         "knn" INSIDE the query
  index setting    (none needed)                  index.knn: true
  licence          SSPL / Elastic (from 7.11)     Apache 2.0
```

That last row is why the fork exists. Nothing in the first three rows would
have needed one.

---

## 6. Solr — [`engines/solr.py`](./engines/solr.py)

Same Lucene again, with a schema-first culture and different vocabulary.

### Save

```
  Doc(...)
        |
        |  index()                                      solr.py:162
        v
  1. POST /solr/papers/schema   {"add-field": [...]}
        |     declare fields explicitly. Solr's schemaless mode would guess
        |     types from the first document; guessing is how you end up with
        |     a date stored as a string forever.
        v
  2. solr.add(batch)            1,000 documents at a time
        |
        v
  3. solr.commit()              <- nothing is searchable until this
  4. solr.optimize()            <- forceMerge by another name
```

`commit` here is `refresh` in Elasticsearch, which is `IndexWriter.commit` in
Lucene. Three names, one operation.

### Search

```
  q1_term()                                             solr.py:35
        |
        |  literal parameters
        v
  self.search(q="abstract:(retrieval)", rows=10, fl="id,title,score")
        |
        v
  GET /solr/papers/select?q=abstract:(retrieval)&rows=10&fl=id,title,score
```

Ask Solr what it made of that:

```bash
curl -s 'localhost:8984/solr/papers/select?q=abstract:(retrieval)&rows=1&debugQuery=true'
```
```
parsedquery: abstract:retriev        <- your word, stemmed, same as everyone else
```

Solr's names for things you already met:

```
  core / collection   =  index
  managed-schema      =  mapping
  string              =  keyword
  fq                  =  a filter clause (no score, cached in filterCache)
  edismax + qf        =  multi_match with boosts
  cursorMark          =  search_after
  ZooKeeper           =  the cluster coordination ES has built in
```

---

## Same document, six storage shapes

After saving that one paper, here is what each engine is actually holding:

```
POSTGRES   a table row, plus a tsvector column:
           'retriev':3A,7B,58B  'approxim':1A,5B  ...
           and a GIN index mapping each stem -> the rows containing it

REDIS      a plain hash at key doc:0705.0751
           plus an in-RAM inverted index: 22,835 terms, 323,077 postings

LUCENE     one immutable segment file, _0.cfs, containing the term
           dictionary, postings, stored fields and doc-value columns

ELASTIC    the same Lucene segment, plus _source (the original JSON),
SEARCH     plus a mapping declaring each field's type

OPENSEARCH byte-for-byte the same as Elasticsearch

SOLR       the same Lucene segment again, plus managed-schema on disk
```

And the same query, six spellings of "find `retrieval`":

```
postgres   plainto_tsquery('english','retrieval')  ->  'retriev'
redis      FT.SEARCH idx:docs 'retrieval'          ->  UNION{retrieval, retriev}
lucene     abstract:retrieval                      ->  TermQuery(abstract:retriev)
elastic    {"match": {"abstract": "retrieval"}}    ->  retriev
opensearch identical to elasticsearch
solr       q=abstract:(retrieval)                  ->  abstract:retriev
```

Six syntaxes. One stem. Four of them are the same Lucene underneath, and they
return the same documents in two pairs — raw Lucene with Solr, Elasticsearch
with OpenSearch. The split follows the *query path*, not the product: one pair
is handed the literal string `abstract:(retrieval)` for its QueryParser, the
other a `match` body.

---

## Try it on one query

```bash
# Index a small corpus, print what the engine stored, then every query
# with its source and its results:
uv run python -m engines.redis

# Or read the captured output without starting anything:
cat results/redis.txt
```

To see the same question in six dialects, open the same function name in all
six files — `q5_fuzzy` in `engines/postgres.py`, `engines/redis.py`, and so on.
