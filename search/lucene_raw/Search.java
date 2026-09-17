/*
 * Raw Lucene: IndexWriter, Document, IndexSearcher, Query, analyzer chain.
 * Everything the three REST engines do for retrieval happens in this file.
 *
 * What is visible here that the REST engines hide:
 *
 *   IndexWriter        buffers documents in RAM, flushes a *segment* -- an
 *                      immutable mini-index -- and merges segments in the
 *                      background per the MergePolicy. "Refresh interval" in
 *                      Elasticsearch is this, exposed as a setting.
 *   Analyzer           tokenizer + token filters. EnglishAnalyzer =
 *                      StandardTokenizer -> lowercase -> stopwords ->
 *                      PorterStemmer. Index time and query time must agree, or
 *                      the query terms never match the indexed terms.
 *   Field types        TextField (analyzed, inverted), StringField (one term,
 *                      verbatim -- ES calls this `keyword`), LongPoint (a BKD
 *                      tree for range queries), NumericDocValues / SortedSet-
 *                      DocValues (columnar, for sorting and faceting),
 *                      StoredField (returned, never searched).
 *   BM25Similarity     the default since Lucene 6. Swap it for
 *                      ClassicSimilarity to get TF-IDF back and watch the
 *                      ranking change on identical postings.
 *   DirectoryReader    a point-in-time snapshot. Documents written after it was
 *                      opened are invisible until it is reopened -- which is
 *                      exactly why "not searchable until refresh" is true of
 *                      every Lucene-based engine here.
 *
 *   java -cp "lib/*" Search.java index <docs.jsonl> <indexdir>
 *   java -cp "lib/*" Search.java serve <indexdir> <port>
 *
 * serve answers one JSON POST per query on the JDK's HTTP server so the Python
 * harness can time it without paying JVM startup per query.
 */

import com.google.gson.*;
import com.sun.net.httpserver.*;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.analysis.en.EnglishAnalyzer;
import org.apache.lucene.document.*;
import org.apache.lucene.facet.*;
import org.apache.lucene.facet.sortedset.*;
import org.apache.lucene.index.*;
import org.apache.lucene.queryparser.classic.QueryParser;
import org.apache.lucene.search.*;
import org.apache.lucene.search.highlight.*;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.search.similarities.ClassicSimilarity;
import org.apache.lucene.store.FSDirectory;
import org.apache.lucene.util.BytesRef;

import java.io.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.LocalDate;
import java.util.*;
import java.util.stream.*;

public class Search {

    static final Analyzer ANALYZER = new EnglishAnalyzer();
    static final String FACET_FIELD = "categories";

    /* Must be identical at index and read time: every dimension goes into one
     * column ($facets) with the dimension encoded in the ordinal. */
    static final FacetsConfig FACETS_CONFIG = new FacetsConfig();
    static {
        FACETS_CONFIG.setMultiValued(FACET_FIELD, true);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) { usage(); return; }
        switch (args[0]) {
            case "index" -> index(args[1], args[2]);
            case "serve" -> serve(args[1], Integer.parseInt(args[2]));
            default -> usage();
        }
    }

    static void usage() {
        System.err.println("usage: Search index <docs.jsonl> <indexdir> | Search serve <indexdir> <port>");
    }

    // ---------------------------------------------------------------- index

    static long epochDays(String iso) {
        try { return LocalDate.parse(iso).toEpochDay(); } catch (Exception e) { return 0L; }
    }

    static void index(String docsPath, String indexDir) throws Exception {
        Path dir = Paths.get(indexDir);
        if (Files.exists(dir)) {
            try (var walk = Files.walk(dir)) {
                walk.sorted(Comparator.reverseOrder()).forEach(p -> p.toFile().delete());
            }
        }
        Files.createDirectories(dir);

        IndexWriterConfig cfg = new IndexWriterConfig(ANALYZER);
        cfg.setOpenMode(IndexWriterConfig.OpenMode.CREATE);
        // Bigger buffer -> fewer, larger segments -> less merging while loading.
        cfg.setRAMBufferSizeMB(256.0);
        cfg.setSimilarity(new BM25Similarity());

        long t0 = System.nanoTime();
        int n = 0;
        try (FSDirectory fsDir = FSDirectory.open(dir);
             IndexWriter writer = new IndexWriter(fsDir, cfg);
             BufferedReader in = Files.newBufferedReader(Paths.get(docsPath))) {

            String line;
            while ((line = in.readLine()) != null) {
                if (line.isBlank()) continue;
                JsonObject o = JsonParser.parseString(line).getAsJsonObject();
                Document doc = new Document();

                String id = o.get("id").getAsString();
                // StringField: one verbatim term, no analysis (ES `keyword`).
                doc.add(new StringField("id", id, Field.Store.YES));

                // TextField: analyzed and inverted. The abstract is stored
                // because highlighting re-reads the original text.
                doc.add(new TextField("title", str(o, "title"), Field.Store.YES));
                doc.add(new TextField("abstract", str(o, "abstract"), Field.Store.YES));

                for (JsonElement a : arr(o, "authors")) {
                    doc.add(new StringField("authors", a.getAsString(), Field.Store.NO));
                }
                for (JsonElement c : arr(o, "categories")) {
                    String cat = c.getAsString();
                    doc.add(new StringField(FACET_FIELD, cat, Field.Store.YES));
                    // Two fields per value: inverted to filter, columnar to count.
                    doc.add(new SortedSetDocValuesFacetField(FACET_FIELD, cat));
                }

                long days = epochDays(str(o, "update_date"));
                // LongPoint is the BKD tree (ranges) and is NOT sortable;
                // sorting needs a separate doc-values field over the same data.
                doc.add(new LongPoint("update_date", days));
                doc.add(new NumericDocValuesField("update_date_dv", days));
                doc.add(new StoredField("update_date", str(o, "update_date")));

                int versions = o.has("version_count") ? o.get("version_count").getAsInt() : 1;
                doc.add(new IntPoint("version_count", versions));
                doc.add(new NumericDocValuesField("version_count_dv", versions));

                // StoredField only: returned, absent from the inverted index.
                doc.add(new StoredField("doi", str(o, "doi")));

                writer.addDocument(FACETS_CONFIG.build(doc));
                n++;
            }
            // One segment so the on-disk size is comparable between runs.
            writer.forceMerge(1);
            writer.commit();
        }
        double secs = (System.nanoTime() - t0) / 1e9;

        long bytes = 0;
        try (var files = Files.list(dir)) {
            bytes = files.mapToLong(p -> p.toFile().length()).sum();
        }
        JsonObject out = new JsonObject();
        out.addProperty("docs", n);
        out.addProperty("build_s", secs);
        out.addProperty("size_bytes", bytes);
        out.addProperty("notes", "forceMerge(1), BM25Similarity, EnglishAnalyzer");
        System.out.println(new Gson().toJson(out));
    }

    static String str(JsonObject o, String k) {
        return o.has(k) && !o.get(k).isJsonNull() ? o.get(k).getAsString() : "";
    }

    static JsonArray arr(JsonObject o, String k) {
        return o.has(k) && o.get(k).isJsonArray() ? o.getAsJsonArray(k) : new JsonArray();
    }

    // ---------------------------------------------------------------- serve

    static DirectoryReader reader;
    static IndexSearcher searcher;
    static SortedSetDocValuesReaderState facetState;

    static void serve(String indexDir, int port) throws Exception {
        FSDirectory dir = FSDirectory.open(Paths.get(indexDir));
        // Point-in-time snapshot: nothing written later is visible until reopen.
        reader = DirectoryReader.open(dir);
        searcher = new IndexSearcher(reader);
        searcher.setSimilarity(new BM25Similarity());
        // The *index* field ($facets), not the dimension. A corpus with no
        // categories (the BEIR sets) never writes it and the state throws --
        // faceting is then unavailable rather than fatal.
        try {
            facetState = new DefaultSortedSetDocValuesReaderState(
                    reader, FacetsConfig.DEFAULT_INDEX_FIELD_NAME, FACETS_CONFIG);
        } catch (IllegalArgumentException e) {
            facetState = null;
        }

        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/search", Search::handle);
        server.createContext("/health", ex -> respond(ex, "{\"ok\":true}"));
        server.setExecutor(null);  // single-threaded, so latencies stay honest
        server.start();
        System.out.println("{\"listening\":" + port + ",\"docs\":" + reader.numDocs() + "}");
    }

    static void handle(HttpExchange ex) throws IOException {
        try {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            JsonObject req = JsonParser.parseString(body).getAsJsonObject();
            respond(ex, new Gson().toJson(run(req)));
        } catch (Exception e) {
            JsonObject err = new JsonObject();
            err.addProperty("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            respond(ex, new Gson().toJson(err));
        }
    }

    static void respond(HttpExchange ex, String json) throws IOException {
        byte[] out = json.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(200, out.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(out); }
    }

    // ------------------------------------------------------- query building

    static List<String> strings(JsonObject o, String k) {
        List<String> out = new ArrayList<>();
        for (JsonElement e : arr(o, k)) out.add(e.getAsString());
        return out;
    }

    /** Analyze like the index did, so query terms match indexed terms. */
    static String analyzed(String field, String text) throws Exception {
        // 'Retrieval' has to become the indexed lexeme 'retriev'.
        Query q = new QueryParser(field, ANALYZER).parse(QueryParser.escape(text));
        if (q instanceof TermQuery tq) return tq.getTerm().text();
        return text.toLowerCase(Locale.ROOT);
    }

    static Query build(JsonObject req) throws Exception {
        String kind = str(req, "kind");
        String text = str(req, "text");
        QueryParser parser = new QueryParser("abstract", ANALYZER);

        switch (kind) {
            case "phrase" -> {
                // TextField stores positions by default; IndexOptions can turn
                // them off for a smaller index, and phrases then stop working.
                PhraseQuery.Builder pb = new PhraseQuery.Builder();
                int pos = 0;
                for (String w : text.split("\\s+")) pb.add(new Term("abstract", analyzed("abstract", w)), pos++);
                return pb.build();
            }
            case "boolean" -> {
                BooleanQuery.Builder bb = new BooleanQuery.Builder();
                for (String t : strings(req, "must"))
                    bb.add(new TermQuery(new Term("abstract", analyzed("abstract", t))), BooleanClause.Occur.MUST);
                List<String> should = strings(req, "should");
                if (!should.isEmpty()) {
                    BooleanQuery.Builder ob = new BooleanQuery.Builder();
                    for (String t : should)
                        ob.add(new TermQuery(new Term("abstract", analyzed("abstract", t))), BooleanClause.Occur.SHOULD);
                    bb.add(ob.build(), BooleanClause.Occur.MUST);
                }
                for (String t : strings(req, "must_not"))
                    bb.add(new TermQuery(new Term("abstract", analyzed("abstract", t))), BooleanClause.Occur.MUST_NOT);
                return bb.build();
            }
            case "prefix" -> {
                // Rewrites into a disjunction of dictionary terms; a common
                // prefix risks TooManyClauses. Not analyzed: the dictionary
                // holds stems, so 'quant' is matched against 'quantiz'.
                return new PrefixQuery(new Term("abstract", text.toLowerCase(Locale.ROOT)));
            }
            case "fuzzy" -> {
                // Levenshtein automaton over the term dictionary, maxEdits
                // capped at 2. The misspelling must be stemmed first: the index
                // holds 'transform', 3 edits from raw 'transfomer' but 1 from
                // stemmed 'transfom'. Every engine here has this trap.
                return new FuzzyQuery(new Term("abstract", analyzed("abstract", text)), 2);
            }
            case "filtered" -> {
                BooleanQuery.Builder bb = new BooleanQuery.Builder();
                bb.add(parser.parse(QueryParser.escape(text)), BooleanClause.Occur.MUST);
                // FILTER, not MUST: required, but contributes no score.
                bb.add(LongPoint.newRangeQuery("update_date", epochDays(str(req, "date_from")), Long.MAX_VALUE),
                       BooleanClause.Occur.FILTER);
                return bb.build();
            }
            case "boosted" -> {
                BooleanQuery.Builder bb = new BooleanQuery.Builder();
                JsonArray boosts = arr(req, "boosts");
                for (JsonElement el : boosts) {
                    JsonArray pair = el.getAsJsonArray();
                    String field = pair.get(0).getAsString();
                    float weight = pair.get(1).getAsFloat();
                    Query sub = new QueryParser(field, ANALYZER).parse(QueryParser.escape(text));
                    // Field norms already favour short fields, so a title
                    // boost compounds with that.
                    bb.add(new BoostQuery(sub, weight), BooleanClause.Occur.SHOULD);
                }
                return bb.build();
            }
            default -> {
                return parser.parse(QueryParser.escape(text));
            }
        }
    }

    // ------------------------------------------------------------- querying

    static JsonObject run(JsonObject req) throws Exception {
        String kind = str(req, "kind");
        Query query = build(req);
        JsonObject out = new JsonObject();

        if (req.has("count_only") && req.get("count_only").getAsBoolean()) {
            // Matching cost only: no stored fields read, no scores kept.
            out.addProperty("total", searcher.count(query));
            return out;
        }

        if ("facet".equals(kind) && facetState == null) {
            out.add("facets", new JsonArray());
        } else if ("facet".equals(kind)) {
            // Counts come from the doc-values column, not the postings.
            // Multi-valued, so they sum past the hit count.
            FacetsCollector fc = searcher.search(query, new FacetsCollectorManager());
            Facets facets = new SortedSetDocValuesFacetCounts(facetState, fc);
            FacetResult fr = facets.getTopChildren(10, FACET_FIELD);
            JsonArray buckets = new JsonArray();
            if (fr != null) {
                for (LabelAndValue lv : fr.labelValues) {
                    JsonArray b = new JsonArray();
                    b.add(lv.label);
                    b.add(lv.value.intValue());
                    buckets.add(b);
                }
            }
            out.add("facets", buckets);
        }

        int limit = req.has("limit") ? req.get("limit").getAsInt() : 10;
        int offset = req.has("offset") ? req.get("offset").getAsInt() : 0;

        TopDocs top;
        String sortField = req.has("sort_field") && !req.get("sort_field").isJsonNull()
                ? req.get("sort_field").getAsString() : null;
        if (sortField != null) {
            // Reads doc values and skips scoring, hence the NaN score below.
            Sort sort = new Sort(new SortField(sortField + "_dv", SortField.Type.LONG, true));
            top = searcher.search(query, offset + limit, sort);
        } else {
            // Deep paging in one line: page 500 collects 5,000 hits and throws
            // 4,990 away. searchAfter is the fix.
            top = searcher.search(query, offset + limit);
        }

        Highlighter highlighter = null;
        if ("highlight".equals(kind)) {
            // Re-analyzes stored text at query time; term vectors would avoid
            // that at the cost of a much larger index.
            highlighter = new Highlighter(new SimpleHTMLFormatter("<em>", "</em>"), new QueryScorer(query));
            highlighter.setTextFragmenter(new SimpleFragmenter(150));
        }

        StoredFields stored = searcher.storedFields();
        JsonArray hits = new JsonArray();
        for (int i = offset; i < top.scoreDocs.length; i++) {
            ScoreDoc sd = top.scoreDocs[i];
            Document d = stored.document(sd.doc);
            JsonObject h = new JsonObject();
            h.addProperty("id", d.get("id"));
            h.addProperty("score", Float.isNaN(sd.score) ? 0.0f : sd.score);
            h.addProperty("title", d.get("title"));
            if (highlighter != null) {
                String frag = highlighter.getBestFragment(ANALYZER, "abstract", d.get("abstract"));
                h.addProperty("highlight", frag == null ? "" : frag);
            }
            hits.add(h);
        }
        out.add("hits", hits);
        out.addProperty("total", top.totalHits.value);
        return out;
    }
}
