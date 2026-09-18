# How text search actually works

A companion to [`README.md`](./README.md). That file says what the POC is and how to run it; this
one explains *how the six systems work underneath*, in plain words, starting from nothing.

Read it top to bottom once. Part 1 is the machinery all six share — and it is most of the
learning. Parts 2 onward are the six systems, and by then each one is mostly "which of these
choices did they make?"

---

## Part 1 — The one idea underneath all of it

### The problem

You have 500,000 documents. Someone types `rust database`. You need the ones that are *about*
that, ranked best-first, in under 50 ms.

The obvious approach is `WHERE text LIKE '%rust%'`. Three things go wrong:

1. **It reads everything.** No index can help a leading `%`, so the database opens all 500,000
   documents and scans each one. That is seconds, not milliseconds.
2. **It matches the wrong things.** `rust` matches "trust", "crusty", "rustic".
3. **It has no idea what's good.** It gives you 8,000 documents in whatever order it found them.
   The *ranking* is the actual product — everyone only looks at the first ten.

Every system in this POC exists to fix those three things, and each fixes them the same way.

### The fix: turn the problem inside out

A normal index answers "given this document, what's in it?" A search index answers the reverse:
**"given this word, which documents contain it?"** Hence *inverted* index.

Three documents:

```
doc 1: "Rust is a systems programming language"
doc 2: "Postgres is a relational database"
doc 3: "Writing a database in Rust"
```

The inverted index:

```
database    -> [2, 3]
language    -> [1]
postgres    -> [2]
programming -> [1]
relational  -> [2]
rust        -> [1, 3]
systems     -> [1]
writing     -> [3]
```

Now `rust database` is: look up `rust` -> `[1, 3]`. Look up `database` -> `[2, 3]`. Intersect ->
`[3]`. Two lookups and a list merge. **The size of your corpus barely matters** — you touched two
small lists instead of 500,000 documents. That is the entire trick, and every system here
implements it.

Two names you need:

- Each list (`[1, 3]`) is a **postings list**.
- The sorted set of all terms on the left is the **term dictionary**.

### Why those lists are fast to merge

Postings lists are kept **sorted by document ID**, which is what makes intersecting them cheap: walk
both with two pointers, advance whichever is behind, emit matches. Linear in the size of the lists,
never quadratic.

For long lists there is a **skip list** on top — every 128th entry, say, records "next doc is 4,096,
jump this many bytes" — so intersecting a huge list with a tiny one skips most of the huge one
instead of walking it.

And the IDs are **delta-encoded**: `[4, 9, 21, 22]` is stored as `[4, 5, 12, 1]`, because small
numbers compress into fewer bits. A postings list is usually a fraction of the size of the text it
indexes.

### Getting from raw text to those terms: analysis

Notice the index above says `rust`, not `Rust`. Something normalized it. That something is the
**analysis chain**, and it is where most real-world search bugs live. It has three stages:

**1. Tokenizer** — split the text into words.

```
"Rust is a systems programming language"
  -> ["Rust", "is", "a", "systems", "programming", "language"]
```

Splitting on whitespace and punctuation is fine for English. It is completely wrong for Chinese
(no spaces), and awkward for `C++`, `AK-47`, `user@example.com` and `192.168.1.1`.

**2. Token filters** — clean up each token, in order:

- **Lowercase:** `Rust` -> `rust`. Why searches are case-insensitive.
- **Stopwords:** drop `is`, `a`, `the`. Cheap and common, but it means a phrase search for
  `"to be or not to be"` can find nothing at all. Most modern setups keep stopwords and let the
  ranking handle them — a word in every document scores near zero anyway.
- **Stemming:** chop words back to a root so `databases`, `database` and `database's` all become
  `databas`. Yes, that stem is not a real word — it doesn't need to be, it just needs to be *the
  same string* for every variant. This is why searching `running` finds `run`.

**3. The same chain runs at query time.**

This is the rule that catches everyone. If indexing produced `databas` but your query is looked up
as `database`, you match nothing. **The query must go through the same analysis as the document.**

Nearly every "why does my search return zero results" bug is a mismatch between these two paths.

### Positions, for phrase search

`"distributed systems"` as a phrase can't be answered by intersection alone — a document with
`distributed` in the first paragraph and `systems` in the last would match, wrongly.

So the postings list stores **where** in the document each term appeared:

```
distributed -> doc 3 at positions [7, 41]
systems     -> doc 3 at positions [8, 92]
```

`systems` at 8 directly follows `distributed` at 7 — a real phrase. This costs index size and is
usually optional per field.

### The last piece: why search indexes are usually immutable

Here is the awkward bit. Postings lists are packed, delta-encoded, compressed arrays. Inserting one
document means inserting a doc ID into the *middle* of thousands of those lists — one per term in
the document — and rewriting each. That is brutal.

Two strategies exist, and **this is the single biggest architectural split** between the six
systems:

- **Update in place.** Keep the lists in a structure built for insertion — a B-tree of lists,
  roughly. Cheap writes, slightly slower reads. **Postgres and Redis do this.**
- **Never update. Write new, merge later.** Buffer incoming documents, and when the buffer fills,
  write a brand-new, perfectly packed, read-only mini-index. Searches query all of them and merge
  the results. A background job periodically merges small ones into big ones. **Lucene does this**,
  and those mini-indexes are called **segments**.

Immutability buys a lot: no locking (readers never see a half-written file), aggressive compression
(the file never changes), and safe OS-level file caching. It costs: a document isn't searchable the
instant you write it, deletes are deferred, and merging burns background I/O.

Everything distinctive about Lucene, Elasticsearch, OpenSearch and Solr follows from that one
choice.

---

## Part 2 — Postgres: search bolted onto a relational database

### What `tsvector` actually is

Postgres stores the analysis output in a column type called `tsvector`:

```sql
SELECT to_tsvector('english', 'Rust is a systems programming language');
```
```
'languag':6 'program':5 'rust':1 'system':4
```

Read that carefully — it shows you the whole pipeline in one line:

- `is` and `a` are gone (stopwords).
- `programming` became `program`, `language` became `languag` (stemming).
- The numbers are positions (for phrase search).
- They're sorted alphabetically.

Postgres calls these **lexemes**. Same thing everyone else calls terms.

`'english'` is the analysis configuration — and it is a *choice you must repeat identically* at
query time. Index with `'english'`, query with `'simple'` (which does no stemming), and you get
silence. Postgres' version of the analysis-mismatch bug.

### The query side: `tsquery`

```sql
SELECT to_tsvector('english', 'Writing a database in Rust')
    @@ to_tsquery('english', 'rust & database');
```
```
 true
```

`@@` is the match operator. `tsquery` supports `&` (and), `|` (or), `!` (not), `<->` (followed by),
and `:*` (prefix).

Three helpers convert human input into a `tsquery`:

| Function | Input | Behaviour |
|---|---|---|
| `to_tsquery` | `'rust & database'` | Strict operator syntax. Throws on malformed input. **Never feed it raw user text.** |
| `plainto_tsquery` | `'rust database'` | Every word ANDed. Safe, dumb. |
| `websearch_to_tsquery` | `'rust "in practice" -toy'` | Google-style: quotes for phrases, `-` to exclude, `or` for OR. **Use this one** for a search box. |

### GIN: the index

A plain B-tree can't help, because one row holds many lexemes. **GIN** (Generalized INverted index)
is Postgres' inverted index: a B-tree of *keys* (the lexemes), where each key points at a sorted
list of row pointers (the postings list). Identical shape to Part 1.

```sql
-- Store the analysis result so it isn't recomputed on every query.
ALTER TABLE docs ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED;

CREATE INDEX docs_tsv_gin ON docs USING GIN (tsv);
```

Two things worth knowing:

- **GIN vs GiST.** GiST also works and is smaller and faster to update, but it is *lossy*: it can
  return false positives that Postgres must then re-check against the actual row. GIN is exact and
  faster to read. For text search, use GIN unless writes dominate.
- **The pending list.** GIN updates are expensive, so by default new entries go into an unsorted
  "pending list" and get merged in bulk later (`fastupdate`). This makes writes fast but means an
  occasional query pays for a big merge, and a query hitting the pending list must scan it linearly.
  A latency spike in Postgres FTS is usually this.

### `pg_trgm`: typo tolerance, a completely different mechanism

Stemming doesn't help with `kubernets`. `pg_trgm` handles it by ignoring words entirely and indexing
**trigrams** — every 3-character sliding window, with padding:

```sql
SELECT show_trgm('rust');
```
```
{"  r"," ru",rus,ust,"st "}
```

Two strings are similar if they share many trigrams. `kubernets` and `kubernetes` share nearly all
of theirs, so `similarity()` is high. Put a GIN index on the trigrams and fuzzy matching becomes an
index lookup instead of an edit-distance scan.

This is a *separate index on the same column*, serving a different question. `tsvector` answers
"which documents are about this?"; `pg_trgm` answers "which strings look like this?" Autocomplete
and typo correction usually want the second.

### Where Postgres lands

| | |
|---|---|
| **Wins** | No new service. Search results join to your real tables transactionally. Consistent immediately — no refresh delay. Good enough for most internal tools and many products. |
| **Loses** | No IDF in `ts_rank`. No facet engine (`GROUP BY` over 8,000 matched rows is not the same thing). No built-in highlighting worth the name (`ts_headline` re-parses the document at query time and is slow). Scaling means scaling your primary database. |

---

## Part 3 — Redis: the same idea, in memory

Redis' query engine (the RediSearch module; in core since Redis 8) is a **from-scratch inverted
index that lives in RAM**. No Lucene, no B-tree-on-disk. It is genuinely its own implementation, and
that makes it the second outsider in this POC alongside Postgres.

### The mental model: an index is a view over data you already have

You do not load documents "into the index". You keep writing ordinary Redis hashes or JSON, and you
declare an index that *watches* a key prefix:

```
HSET doc:1 title "Writing a database in Rust" author "alice" points 142

FT.CREATE idx
  ON HASH PREFIX 1 doc:
  SCHEMA
    title  TEXT    WEIGHT 5.0
    body   TEXT
    author TAG
    points NUMERIC SORTABLE
```

From then on, every `HSET` under `doc:` updates the index **synchronously, as part of the write**.
There is no refresh interval and no eventual consistency: write it, and the next `FT.SEARCH` sees
it. That is the sharpest contrast with everything Lucene-based in this document.

### Field types are the schema

| Type | Indexed as | Query syntax | Notes |
|---|---|---|---|
| `TEXT` | Analyzed — tokenized, lowercased, stemmed | `@title:(rust database)` | `WEIGHT` boosts the field's contribution at score time |
| `TAG` | Stored whole, no analysis | `@author:{alice}` | Exact match. **Braces, not parens.** Redis' `keyword`. |
| `NUMERIC` | A range tree | `@points:[100 +inf]` | `SORTABLE` keeps a copy for sorting |
| `GEO` | Geohash | `@loc:[77.59 12.97 5 km]` | Same family as the geo POC |
| `VECTOR` | HNSW or FLAT graph | `*=>[KNN 10 @vec $blob]` | Embeddings, in the same index |

The `TEXT` vs `TAG` split is exactly the `text` vs `keyword` distinction Elasticsearch makes, under
different names — and the same trap. Index an author as `TEXT` and `@author:{alice smith}` fails,
because the value was split into two tokens. Names, IDs, statuses, enums: always `TAG`.

### Faceting: `FT.AGGREGATE`

This is the thing Postgres simply cannot do. `FT.SEARCH` returns documents; `FT.AGGREGATE` runs a
pipeline over the *matching set* and returns computed groups:

```
FT.AGGREGATE idx "@title:(database)"
  GROUPBY 1 @author
    REDUCE COUNT 0 AS n
  SORTBY 2 @n DESC
  LIMIT 0 10
```

"Top 10 authors among documents matching `database`" — computed inside Redis over the whole match
set, not by dragging 8,000 documents to your application and counting there. That distinction is the
entire value of a faceting engine.

### Vector search in the same index

A `VECTOR` field puts embeddings alongside the text fields, which enables something genuinely useful
in one round trip:

```
FT.SEARCH idx "@points:[100 +inf]=>[KNN 10 @vec $blob AS score]"
  PARAMS 2 blob "<384 floats as bytes>"
  DIALECT 2
```

Read it as: *filter to documents with more than 100 points, then find the 10 nearest vectors among
those.* That `=>` is a **prefilter**, and it matters more than it looks.

The naive alternative is post-filtering: ask for the 10 nearest overall, then throw away those under
100 points — and possibly end up with 2 results. Every vector database has to solve this, and
comparing how Redis, pgvector and Elasticsearch each do it is one of the more interesting things
this POC can show.

### The honest limits

- **It's all in RAM.** The index is memory-resident. Your ceiling is machine memory, and indexes
  are not small.
- **Durability is Redis' durability.** The index is rebuilt from RDB/AOF at startup — which takes
  time proportional to the data.
- **Deep paging is capped.** `MAXSEARCHRESULTS` limits how far `LIMIT offset num` can reach.
- **Open-source Redis doesn't shard the index.** Scaling out is an Enterprise feature.
- **`DIALECT` matters.** Query parsing semantics changed between dialect versions; some syntax needs
  `DIALECT 2` or higher explicitly. Set it and pin it, or you'll be debugging a query that "worked
  yesterday".

### Where Redis lands

Sits precisely between Postgres and Elasticsearch: real BM25, real faceting, filters and vectors in
one round trip against data already in Redis, updated synchronously — paid for with RAM and no
open-source sharding.

---

## Part 4 — Lucene: the engine everyone else wraps

Lucene is a **Java library**, not a server. No HTTP, no cluster, no JSON. You call it in-process and
it gives you an index on local disk. Elasticsearch, OpenSearch and Solr are servers that embed this
library and add networking, clustering and an API on top.

Which means: **everything about retrieval quality in those three is Lucene.** If you understand this
part, the remaining three sections are configuration.

### Segments: the immutability decision

Lucene never modifies an index file. Writing works like this:

1. `IndexWriter` buffers incoming documents in memory.
2. When the buffer fills (or you ask), it writes a **segment** — a complete, self-contained,
   read-only mini-index: its own term dictionary, its own postings lists, its own norms.
3. A search runs against **every** segment and merges the results.
4. A background **merge policy** combines small segments into bigger ones.

So an index is not one structure — it is a pile of immutable mini-indexes plus a list of which ones
are live.

**Deletes don't delete.** Deleting document 7 sets a bit in a "deleted documents" bitmap next to the
segment. The document stays in the postings lists; it's filtered out of results. The space comes back
only when that segment is merged. **Updates are delete + re-add**, same deal. This is why a
write-heavy Lucene index needs merging headroom, and why an index can be much larger on disk than
the live data in it.

**Merging is the tax.** Merging two 1 GB segments means reading 2 GB and writing 2 GB, in the
background, while serving queries. Merge policy tuning is most of Lucene operations work.

What immutability buys:

- **No read locks.** A searcher holds a fixed set of segments and is unaffected by concurrent
  writes.
- **Hard compression.** The file never changes, so you can pack it as tightly as you like.
- **Free OS caching.** Immutable files sit in the page cache permanently and never get invalidated.

### What's inside one segment

**Term dictionary.** Every term in the segment, sorted, stored as an **FST** (finite state
transducer) — a compressed automaton that shares prefixes *and* suffixes between terms. `database`,
`databases` and `datacenter` overlap almost entirely in the structure. It's small enough to live in
memory and maps a term to its offset in the postings file.

The FST is also why `elas*` is fast: prefix matching is just walking the automaton from a node.

**Postings.** Sorted, delta-encoded doc IDs, block-compressed (PFOR-delta), with skip lists over
them. Optionally with positions (for phrases) and offsets (for highlighting).

**Norms.** One byte per field per document: the length, lossily encoded, for BM25's length
normalization.

**Stored fields.** The original field values, row-oriented and compressed, so you can show the
document you matched. Read only for the documents you actually return.

**Doc values.** A *column-oriented* copy of a field, written for sorting, faceting and
aggregation. If you want "sort by points" or "count by author", reading a packed column of 20,000
values beats decompressing 20,000 stored documents. The `_source: false` + `docvalue_fields`
optimization in the geo POC's Elasticsearch store is exactly this, and it is worth understanding
because the same trick appears everywhere.

### The API, small enough to hold in your head

```java
// Writing
Directory dir = FSDirectory.open(Paths.get("index"));
IndexWriter writer = new IndexWriter(dir, new IndexWriterConfig(new StandardAnalyzer()));

Document doc = new Document();
doc.add(new TextField("title", "Writing a database in Rust", Field.Store.YES));  // analyzed
doc.add(new StringField("author", "alice", Field.Store.YES));                    // NOT analyzed
doc.add(new NumericDocValuesField("points", 142));                               // sortable
writer.addDocument(doc);
writer.close();   // commit: makes segments durable

// Reading
IndexSearcher searcher = new IndexSearcher(DirectoryReader.open(dir));
Query q = new QueryParser("title", new StandardAnalyzer()).parse("rust AND database");
TopDocs hits = searcher.search(q, 10);
```

`TextField` vs `StringField` in that snippet **is** the `text` vs `keyword` distinction that
Elasticsearch is famous for, and `TAG` vs `TEXT` in Redis. Same idea, three vocabularies.

Note `search(q, 10)`: Lucene is built to return the **top N**, and it prunes aggressively —
scoring can skip documents that provably can't reach the current 10th-best score. Ask for all 8,000
matches and you lose that optimization entirely. This is the mechanism behind the geo POC's finding
that Elasticsearch was fast at matching and slow at returning everything.

### Why do this stage at all

Because everything above appears in all three servers below, renamed. Doing it raw once means the
next three are "which knobs did they expose?" instead of magic.

---

## Part 5 — Elasticsearch: Lucene, distributed, behind JSON

Take Lucene. Put it in a server. Add an HTTP/JSON API, a clustering layer, and a query language.
That's Elasticsearch.

### Sharding: the core structural idea

An Elasticsearch **index** is split into **shards**, and *each shard is one complete Lucene index*.
Five shards means five independent Lucene indexes, spread across machines. **Replicas** are copies
of a shard, for redundancy and read throughput.

A search works by **scatter-gather**:

1. Your request hits any node; it becomes the **coordinating node**.
2. It fans the query out to one copy of every shard.
3. Each shard runs Lucene locally and returns its own top 10 — just IDs and scores.
4. The coordinator merges those lists, keeps the global top 10, and fetches the full documents for
   *only those 10*.

That two-phase design (`query_then_fetch`) is why `from: 10000, size: 10` is so expensive: to skip
10,000 results globally, every shard must produce and return its top 10,010. Deep paging cost grows
with shard count. `search_after` avoids it by passing "the last result I saw" instead of an offset,
so each shard resumes rather than recounting.

**Shard count is decided at index creation and cannot be changed** (only reindexed). Too few, you
can't spread load; too many, every query pays fixed per-shard overhead. One of the few genuinely
irreversible decisions in the system.

### Mapping vs analysis

Two words that get confused constantly:

- **Mapping** = the schema. Which fields exist, what type each is.
- **Analysis** = the tokenizer-plus-filters chain applied to `text` fields.

### `text` vs `keyword`, and the `match`/`term` trap

The central concept, and the most common bug in the entire ecosystem.

| | `text` | `keyword` |
|---|---|---|
| Analyzed? | Yes — tokenized, lowercased, stemmed | No — stored as one exact string |
| `"Alice Smith"` indexed as | `alice`, `smith` | `Alice Smith` |
| Good for | Prose you search *within* | IDs, statuses, tags, enums, anything you filter/sort/facet on |
| Sort/aggregate? | No (needs `fielddata`, avoid) | Yes |

Because you often need both, the default dynamic mapping creates a **multi-field**: `author` as
`text`, plus `author.keyword` as `keyword`. Same source value, two indexes, two purposes.

Now the trap:

- **`match`** analyzes your query text before looking it up.
- **`term`** does *not* — it looks up your string byte-for-byte.

So `{"term": {"title": "Rust"}}` on a `text` field finds **nothing**, because the index contains
`rust` (lowercased) and you asked for `Rust`. It fails silently with zero results and no error.

The rule: **`match` on `text`, `term` on `keyword`.** Almost every "my query returns nothing" report
is this.

### Near-real-time, refresh, and durability

A newly indexed document is not immediately searchable. It sits in an in-memory buffer until a
**refresh** turns it into a new searchable segment — **once per second** by default.

That one-second delay is why Elasticsearch is called *near*-real-time, and why the geo POC's loader
sets `refresh_interval: -1` during bulk loading and refreshes once at the end: building a segment
every second while nothing is querying is pure waste.

Durability is separate. Every write also appends to a **translog**, fsynced per request by default,
so an unrefreshed document survives a crash. A **flush** performs a real Lucene commit and truncates
the translog.

Three distinct concepts people merge into one:

| | Makes documents... | Costs |
|---|---|---|
| **Refresh** | ...searchable | New segment; more segments to merge |
| **Flush** | ...durably committed to Lucene | fsync, translog truncation |
| **Force merge** | ...faster to search | Heavy I/O; only for indexes that stopped changing |

### Aggregations

The faceting engine, and a large part of why people choose Elasticsearch. Aggregations compute
over the *matching set* inside the cluster, nest arbitrarily, and come back alongside the hits:

```json
{
  "query": { "match": { "title": "database" } },
  "aggs": {
    "top_authors": {
      "terms": { "field": "author.keyword", "size": 10 },
      "aggs": { "avg_points": { "avg": { "field": "points" } } }
    }
  }
}
```

"Top 10 authors among matches, with each one's average points" — and note it reads
`author.keyword`, not `author`, for the reason in the table above.

Aggregations run on **doc values** — the column-oriented copy from Part 4. That's the connection
worth holding onto: a feature people think of as an Elasticsearch capability is really a Lucene
storage format.

---

## Part 6 — OpenSearch: the fork

Short section, and the brevity is the point.

In January 2021, Elastic changed Elasticsearch's license from Apache 2.0 to SSPL — not an
OSI-approved open-source license, and specifically aimed at cloud providers offering it as a managed
service. AWS forked the last Apache-2.0 release, **Elasticsearch 7.10.2**, and renamed it
OpenSearch.

So OpenSearch is not a competing design. It is *the same code*, diverging since 2021. Same Lucene
underneath, same inverted index, same BM25, same shards, near-identical REST API.

What actually differs:

| | Elasticsearch | OpenSearch |
|---|---|---|
| **License** | Elastic License / SSPL (AGPL also offered since 2024) | Apache 2.0 |
| **Governance** | Elastic | Linux Foundation (since 2024) |
| **Vector search** | Built in, tiered by license level | k-NN plugin, fully free |
| **Security (auth, TLS, RBAC)** | Basic tier free, advanced paid | Free |
| **Clients** | `elasticsearch-py` etc. | `opensearch-py` — forked, mostly drop-in |

The one gotcha worth knowing: **official Elasticsearch clients version-check the server** and newer
ones refuse to talk to OpenSearch. The protocol works; the client deliberately objects. Use the
matching client.

For this POC the stage is deliberately thin: point the same queries at it, observe that they work
unchanged, document precisely where it diverges. That *is* the finding, and it is exactly the answer
an interviewer wants — not a list of differences, but "same Lucene, forked at 7.10, the split is
licensing and plugins, not retrieval."

---

## Part 7 — Solr: the same engine, a different culture

Solr predates Elasticsearch by years and comes from the same Apache project as Lucene. Same engine,
noticeably different philosophy.

### Schema-first

Elasticsearch defaults to guessing a field's type when it first sees it (dynamic mapping). Solr's
tradition is the opposite: define the schema up front, in `managed-schema`.

```xml
<field name="title"  type="text_general" indexed="true" stored="true"/>
<field name="author" type="string"       indexed="true" stored="true" docValues="true"/>
<field name="points" type="pint"         indexed="true" stored="true" docValues="true"/>

<fieldType name="text_general" class="solr.TextField">
  <analyzer type="index">
    <tokenizer class="solr.StandardTokenizerFactory"/>
    <filter class="solr.LowerCaseFilterFactory"/>
    <filter class="solr.PorterStemFilterFactory"/>
  </analyzer>
  <analyzer type="query">  <!-- often identical; here you can see it explicitly -->
    ...
  </analyzer>
</fieldType>
```

That XML is the most *legible* representation of Part 1 in this whole document: the tokenizer, the
filter chain, in order, and separate index-time and query-time analyzers spelled out as two blocks.
Elasticsearch has exactly the same machinery in JSON, less visibly. `text_general` here is `text`;
`string` is `keyword`.

(Solr does support schemaless mode. The schema-first habit is the culture, not a hard constraint.)

### Cores and collections

- A **core** is a single Lucene index on one node. Fine for a single-server deployment.
- A **collection** is a distributed index in **SolrCloud** mode — sharded and replicated across
  nodes, made of many cores.

The distribution model matches Elasticsearch's closely (scatter-gather over shards, replicas for
redundancy). The difference is who keeps the cluster state.

### ZooKeeper — the real architectural difference

SolrCloud uses **Apache ZooKeeper** to hold cluster state: which shards exist, where their replicas
live, who the leader is, and the configuration itself. It is an external, separately operated
service.

Elasticsearch built its own coordination layer (Zen, later a Raft-like protocol) into the product.

So the honest trade:

- **Solr:** one more distributed system to run, understand and keep quorate — but it is a mature,
  battle-tested one with well-understood failure modes.
- **Elasticsearch:** simpler to operate, one thing instead of two — but the coordination logic is
  bespoke and historically had a rougher path to correctness.

This is a genuinely good interview answer, because it is a real trade rather than "X is better".

### `edismax`

Solr's query parsers are a visible, chosen component. The one you want is **eDisMax** (Extended
Disjunction Max):

```
q=rust database
defType=edismax
qf=title^5 body^1        # search these fields, title weighted 5x
pf=title^10              # bonus if the terms appear as a phrase in the title
mm=2<75%                 # "minimum should match": 2 terms required, beyond that 75%
```

Two things are going on:

- **"Disjunction max":** a document's score for a term is the *best* field score, not the sum. A
  document matching `rust` strongly in the title beats one matching it weakly in five fields.
  Prevents field-count from dominating relevance.
- **It tolerates whatever a user types.** Unbalanced quotes, stray `AND`, a lone `+` — eDisMax
  copes instead of throwing. The strict parser does not, which is why raw user input should never
  reach it.

Elasticsearch's `multi_match` with `type: best_fields` is the same idea, and `minimum_should_match`
is the same knob.

### Faceting

Solr's historical claim to fame, and still excellent:

```
q=database&facet=true&facet.field=author&facet.limit=10
```

Plus a richer JSON Facet API for nested and computed facets. Functionally Elasticsearch's
aggregations caught up long ago — both sit on Lucene doc values — but Solr's facet syntax is
terser for the common case, and e-commerce faceted navigation is where Solr earned its reputation.

---

## Part 8 — Putting it side by side

| | Postgres | Redis | Lucene (raw) | Elasticsearch | OpenSearch | Solr |
|---|---|---|---|---|---|---|
| **Engine** | Own (`tsvector` + GIN) | Own, in-memory | — *is* the engine | Lucene | Lucene | Lucene |
| **Index storage** | Updated in place, on disk | Updated in place, in RAM | Immutable segments | Immutable segments | Immutable segments | Immutable segments |
| **Default ranking** | `ts_rank` — **no IDF** | TF-IDF (BM25 selectable) | BM25 | BM25 | BM25 | BM25 |
| **Analyzed vs exact** | `tsvector` vs plain column | `TEXT` vs `TAG` | `TextField` vs `StringField` | `text` vs `keyword` | `text` vs `keyword` | `text_general` vs `string` |
| **Write visibility** | Immediate (transactional) | Immediate (synchronous) | On commit | ~1s refresh | ~1s refresh | ~1s soft commit |
| **Faceting** | `GROUP BY`, no facet engine | `FT.AGGREGATE` | Build it yourself | Aggregations | Aggregations | Facets / JSON Facet API |
| **Distribution** | Your Postgres story | Enterprise only | None | Built in | Built in | SolrCloud + ZooKeeper |
| **Vectors** | `pgvector` | Built in, prefilterable | Built in (`KnnVectorField`) | `dense_vector` | k-NN plugin, free | Dense vector field |
| **Operational cost** | Zero extra | One service you likely run already | A library, no service | A cluster | A cluster | A cluster + ZooKeeper |

### The four rows that matter

**Ranking.** Postgres has no IDF. Redis defaults to TF-IDF but BM25 is one word away. Everything
Lucene-based is BM25 by default. That ordering is roughly the ordering of out-of-the-box result
quality, and it is the honest reason to add a search engine to a stack that already has a database.

**Write visibility.** Postgres and Redis show you a document the instant you write it. Everything
Lucene-based makes you wait for a refresh. If your product needs "user edits a title, immediately
searches for it, finds it", that is a real constraint, not a detail.

**Faceting.** Postgres *can* do it — `unnest` + `GROUP BY` returns correct counts in 12 ms on a
4,000-row match. What it has no equivalent of is a facet engine: everything else keeps a
column-oriented copy of the field (doc values, or Redis' sortable fields) and reads that, so their
cost is flat while Postgres' tracks the match count.

**Operational cost.** A cluster is a thing you run, monitor, upgrade and page someone about at 3 am.
Being able to say "Postgres FTS is enough here, and here's the specific quality we give up" is a
stronger answer than reaching for Elasticsearch reflexively.

### How to actually choose

- **Postgres FTS** — data's already there, moderate corpus, ranking quality isn't the product, and
  you value one fewer service more than you value ranking quality.
- **Redis** — already running Redis, need real ranking and faceting and low latency, the index fits
  in RAM, and synchronous write visibility is worth something.
- **Elasticsearch / OpenSearch** — search *is* the product, or you need faceting, highlighting,
  aggregations, and horizontal scale. Pick OpenSearch when the Apache-2.0 license or free security
  features decide it.
- **Solr** — same engine; choose it for existing expertise, an existing ZooKeeper footprint, or
  faceted navigation as the central use case.
- **Raw Lucene** — almost never in production. Always worth doing once, because it makes the other
  three legible.

---

## Part 9 — Vocabulary

| Term | Meaning |
|---|---|
| **Inverted index** | Term -> list of documents containing it. The central data structure. |
| **Term / lexeme / token** | One indexed unit of text, post-analysis. Three names for the same thing. |
| **Postings list** | The sorted document IDs for one term. |
| **Term dictionary** | The sorted set of all terms; Lucene stores it as an FST. |
| **Analysis chain** | Tokenizer + filters that turn raw text into terms. Must match at index and query time. |
| **Stemming** | Reducing words to a common root (`databases` -> `databas`). |
| **Stopwords** | Very common words optionally dropped at index time. |
| **TF** | Term frequency — how often a term occurs in a document. |
| **IDF** | Inverse document frequency — how rare a term is across the corpus. |
| **BM25** | The modern ranking formula. TF-IDF plus saturation and length normalization. |
| **Norm** | Stored field length, used by BM25's length normalization. |
| **Positions** | Where in the document each term occurred. Enables phrase search. |
| **Segment** | One immutable Lucene mini-index. An index is a pile of them. |
| **Merge** | Background combining of small segments into larger ones. |
| **Refresh** | Making buffered documents searchable by writing a new segment (~1s default). |
| **Flush / commit** | Making writes durable in Lucene; truncates the translog. |
| **Doc values** | Column-oriented copy of a field, for sorting, faceting and aggregation. |
| **Stored fields** | Row-oriented original values, read only for returned documents. |
| **Shard** | One complete Lucene index; an Elasticsearch/Solr index is several. |
| **Scatter-gather** | Fan a query to all shards, merge their top-N centrally. |
| **Faceting / aggregation** | Counting and grouping over the matching set, inside the engine. |
| **Prefilter (vectors)** | Restricting candidates *before* nearest-neighbour search, not after. |
