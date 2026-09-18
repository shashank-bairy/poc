"""Raw Lucene. One function per query; the query string is inside the function.

The Lucene itself is in lucene_raw/Search.java. This file starts that JVM and
sends it query strings. Each string is literal Lucene query-parser syntax -- the
same syntax Solr's standard parser takes, because it is the same parser.

Run this file on its own to see all eleven queries and their results:

    uv run python -m engines.lucene
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from typing import Iterable

import requests

from core.common import Doc, Hit, IndexStats, write_jsonl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "lucene_raw")
LIB_DIR = os.path.join(RAW_DIR, "lib")
INDEX_DIR = os.path.join(RAW_DIR, "index")
SRC = os.path.join(RAW_DIR, "Search.java")
JAVA_FLAGS = ["--enable-native-access=ALL-UNNAMED"]
PORT = int(os.environ["LUCENE_PORT"]) if os.environ.get("LUCENE_PORT") else 0


class LuceneEngine:
    name = "lucene"

    # ---------------------------------------------------------------- queries

    def q1_term(self):
        return self.search("abstract:retrieval")

    def q1_term_count(self) -> int:
        """The same match, counted only: no stored fields read, no scores kept."""
        return self.count("abstract:retrieval")

    def q2_phrase(self):
        return self.search('abstract:"attention mechanism"')

    def q3_boolean(self):
        # + is MUST, - is MUST_NOT, bare is SHOULD. AND/OR/NOT keywords work
        # too and mean the same thing.
        return self.search("+abstract:retrieval +(abstract:dense abstract:sparse) -abstract:image")

    def q4_prefix(self):
        # Walks the term dictionary from the prefix and rewrites into a
        # disjunction. Cheap for a rare prefix, a TooManyClauses risk for a
        # common one. Matched against stems: 'quant' finds the stem 'quantiz'.
        return self.search("abstract:quant*")

    def q5_fuzzy(self):
        # 'transfom~2' -- already stemmed, edit distance 2. Written
        # 'transfomer~2' it matches nothing: the index holds 'transform', which
        # is 3 edits away, and Lucene caps maxEdits at 2.
        return self.search("abstract:transfom~2")

    def q6_filter_text(self):
        # update_date is a LongPoint (BKD tree) of epoch days; 19358 is
        # 2023-01-01. In the Java API this would be a FILTER clause
        # contributing no score; in parser syntax it is a MUST that does.
        return self.search("abstract:(graph neural) AND update_date:[19358 TO 2147483647]")

    def q7_facet(self, top: int = 10) -> list[tuple[str, int]]:
        # Counts come from the SortedSetDocValues column, not the postings.
        data = self._post({"q": "abstract:retrieval", "limit": 10, "facet": "categories"})
        return [(value, int(n)) for value, n in data.get("facets", [])][:top]

    def q8_sort_date(self):
        # update_date_dv, not update_date: a LongPoint answers ranges and is
        # NOT sortable. Sorting needs a separate NumericDocValuesField over the
        # same number. The classic Lucene surprise.
        return self.search("abstract:retrieval", sort="update_date_dv")

    def q9_deep_page(self):
        # offset+limit collects 5,000 ScoreDocs to return 10. searchAfter takes
        # the previous page's last ScoreDoc and is the real fix.
        return self.search("abstract:learning", offset=4990)

    def q10_highlight(self):
        # The Highlighter re-analyzes the stored text at query time; term
        # vectors would avoid that at the cost of a much larger index.
        return self.search("abstract:(knowledge distillation)", highlight="abstract")

    def q11_boosted(self):
        # ^5.0 is a BoostQuery multiplying the subquery score. Field norms
        # already favour short fields like title, so a title boost compounds.
        return self.search("title:(language model)^5.0 abstract:(language model)^1.0")

    def all_queries(self):
        """Every search query, in order, for bench/compare.py."""
        return [
            ("1-term", self.q1_term),
            ("2-phrase", self.q2_phrase),
            ("3-boolean", self.q3_boolean),
            ("4-prefix", self.q4_prefix),
            ("5-fuzzy", self.q5_fuzzy),
            ("6-filter-text", self.q6_filter_text),
            ("8-sort-date", self.q8_sort_date),
            ("9-deep-page", self.q9_deep_page),
            ("10-highlight", self.q10_highlight),
            ("11-boosted", self.q11_boosted),
        ]

    # ------------------------------------------------------ what is on disk

    def stored(self, doc_id: str) -> str:
        """Stored fields and indexed terms, side by side.

        These are two different things and Lucene keeps them apart:
        stored fields are the verbatim text, written once and only read back
        to build a response; indexed terms are what the Analyzer produced, and
        are the only thing a query can ever match. Search.java prints both.
        """
        data = self._post({"stored": doc_id})
        if "error" in data:
            return data["error"]
        out = ["stored fields (verbatim, never searched):"]
        for k, v in data["fields"].items():
            out.append(f"  {k:<12} {str(v)[:96]}")
        out.append("\nindexed terms (what the postings are keyed on):")
        out.append(f"  title       {data['title_terms']}")
        out.append(f"  abstract    {data['abstract_terms'][:14]} ...")
        return "\n".join(out)

    # ------------------------------------------------------------- the runner

    def search(self, q: str, limit: int = 10, offset: int = 0, sort: str = None,
               highlight: str = None) -> list[Hit]:
        """Send one query string to the JVM and read the hits back."""
        request = {"q": q, "limit": limit, "offset": offset}
        if sort:
            request["sort"] = sort
        if highlight:
            request["highlight"] = highlight
        data = self._post(request)
        return [
            Hit(
                id=h["id"],
                score=float(h.get("score", 0.0)),
                title=h.get("title", ""),
                highlight=h.get("highlight", ""),
            )
            for h in data.get("hits", [])
        ]

    def count(self, q: str) -> int:
        return int(self._post({"q": q, "count_only": True})["total"])

    def __init__(self, port: int = 0):
        if not shutil.which("java"):
            raise RuntimeError("java not on PATH; Lucene 9.11 needs Java 17+")
        ensure_jars()
        self.port = port or PORT or free_port()
        self.url = f"http://localhost:{self.port}/search"
        self.proc: subprocess.Popen | None = None
        self.session = requests.Session()

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        tmp = os.path.join(RAW_DIR, "_index_input.jsonl")
        write_jsonl(tmp, (d.to_json() for d in docs))
        try:
            proc = subprocess.run(_java("index", tmp, INDEX_DIR), capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"lucene index failed:\n{proc.stderr[-2000:]}")
            # JVM logs go to stderr; stdout is the JSON.
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            os.remove(tmp)

        self.start()
        return IndexStats(
            docs=payload["docs"],
            build_s=payload["build_s"],
            size_bytes=payload["size_bytes"],
            notes=payload.get("notes", ""),
        )

    def start(self) -> None:
        """Restart so the server opens a reader over the new index: a
        DirectoryReader is a point-in-time snapshot of the segments."""
        self.stop()
        self.proc = subprocess.Popen(
            _java("serve", INDEX_DIR, str(self.port)),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        atexit.register(self.stop)
        health = f"http://localhost:{self.port}/health"
        for _ in range(100):
            try:
                if self.session.get(health, timeout=1).ok:
                    return
            except requests.RequestException:
                time.sleep(0.2)
        raise RuntimeError("lucene server did not come up; run the java command by hand")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _post(self, payload: dict) -> dict:
        if self.proc is None:  # --skip-index never called index(), so no JVM yet
            self.start()
        resp = self.session.post(self.url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data

    def close(self) -> None:
        self.stop()
        self.session.close()


def free_port() -> int:
    """A fixed port is a trap: a JVM left over from an earlier run still serves
    the old segments, so a stale index would be queried silently."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ensure_jars() -> None:
    if not os.path.isdir(LIB_DIR) or not os.listdir(LIB_DIR):
        subprocess.run([os.path.join(RAW_DIR, "fetch_jars.sh")], check=True)


def _java(*args: str) -> list[str]:
    return ["java", *JAVA_FLAGS, "-cp", f"{LIB_DIR}/*", SRC, *args]


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(LuceneEngine())
