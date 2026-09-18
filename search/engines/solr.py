"""Solr. One function per query, the parameters inside the function.

Every query here runs as-is in curl:

    curl -s 'localhost:8984/solr/papers/select?q=abstract:(retrieval)&rows=10' | jq

Vocabulary against Elasticsearch:
    core / collection   not index
    managed-schema      not mapping
    string              not keyword
    fq                  not a filter clause
    edismax + qf        not multi_match
    cursorMark          not search_after
    ZooKeeper           not built-in coordination

    uv run python -m engines.solr
"""

from __future__ import annotations

import time
from typing import Iterable

import pysolr
import requests

from core.common import SOLR_URL, Doc, Hit, IndexStats


class SolrEngine:
    name = "solr"

    # ---------------------------------------------------------------- queries

    def q1_term(self):
        return self.search(q="abstract:(retrieval)", rows=10, fl="id,title,score")

    def q1_term_count(self) -> int:
        return self.count(q="abstract:(retrieval)", rows=0)

    def q2_phrase(self):
        return self.search(q='abstract:"attention mechanism"', rows=10, fl="id,title,score")

    def q3_boolean(self):
        return self.search(
            q="abstract:retrieval AND (abstract:dense OR abstract:sparse) AND -abstract:image",
            rows=10,
            fl="id,title,score",
        )

    def q4_prefix(self):
        return self.search(q="abstract:quant*", rows=10, fl="id,title,score")

    def q5_fuzzy(self):
        # Note the term: 'transfom', already stemmed, not 'transfomer'.
        # Lucene's parser runs only the multiterm chain (lowercasing) on fuzzy,
        # prefix and wildcard terms, so the raw misspelling sits 3 edits from
        # the indexed stem 'transform' and matches nothing. ES's `match` +
        # fuzziness hides this by analyzing the query first.
        return self.search(q="abstract:transfom~", rows=10, fl="id,title,score")

    def q6_filter_text(self):
        # fq is the filter: no score contribution, cached in the filterCache.
        return self.search(
            q="abstract:(graph neural)",
            fq="update_date:[2023-01-01T00:00:00Z TO *]",
            rows=10,
            fl="id,title,score",
        )

    def q7_facet(self, top: int = 10) -> list[tuple[str, int]]:
        res = self._run(
            q="abstract:(retrieval)",
            rows=0,
            facet="true",
            **{"facet.field": "categories", "facet.limit": 10, "facet.mincount": 1},
        )
        flat = res.facets["facet_fields"]["categories"]  # [value, count, value, count, ...]
        return [(flat[i], int(flat[i + 1])) for i in range(0, len(flat), 2)][:top]

    def q8_sort_date(self):
        return self.search(
            q="abstract:(retrieval)", sort="update_date desc", rows=10, fl="id,title,score"
        )

    def q9_deep_page(self):
        # start/rows is from/size. cursorMark is the real answer for deep
        # paging and needs a sort that includes a unique field.
        return self.search(q="abstract:(learning)", start=4990, rows=10, fl="id,title,score")

    def q10_highlight(self):
        return self.search(
            q="abstract:(knowledge distillation)",
            rows=10,
            fl="id,title,score",
            hl="true",
            **{"hl.fl": "abstract", "hl.snippets": 1, "hl.fragsize": 150},
        )

    def q11_boosted(self):
        # edismax takes the raw user words and spreads them across weighted
        # fields, tolerating punctuation a real user would type. The standard
        # parser would throw a parse error on it -- which is why every other
        # query here is written against pre-cleaned text.
        return self.search(
            q="language model",
            defType="edismax",
            qf="title^5 abstract^1",
            rows=10,
            fl="id,title,score",
        )

    def all_queries(self):
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
        """The stored document, then the analyzer chain stage by stage.

        Solr's analysis endpoint is the most explicit of any engine here: it
        names every tokenizer and filter in the chain and shows the tokens
        after each one. That is the whole index-time pipeline, printed.
        """
        docs = self._run(q=f'id:"{doc_id}"', rows=1, fl="*").docs
        if not docs:
            return f"no such doc: {doc_id}"
        doc = docs[0]
        out = ["stored document:"]
        for k, v in doc.items():
            if k == "vec":
                v = f"<{len(v)} floats>"
            out.append(f"  {k:<14} {str(v)[:92]}")
        out.append("\nindex-time analyzer chain for `abstract`:")
        for stage, tokens in self._analysis_chain(doc.get("title", "")):
            out.append(f"  {stage:<26} {tokens}")
        return "\n".join(out)

    # ------------------------------------------------------------- the runner

    def search(self, q: str, **params) -> list[Hit]:
        res = self._run(q=q, **params)
        hl = getattr(res, "highlighting", {}) or {}
        return [
            Hit(
                id=doc["id"],
                score=float(doc.get("score", 0.0)),
                title=doc.get("title", ""),
                highlight=(hl.get(doc["id"], {}).get("abstract") or [""])[0],
            )
            for doc in res.docs
        ]

    def count(self, q: str, **params) -> int:
        return int(self._run(q=q, **params).hits)

    def _analysis_chain(self, text: str) -> list[tuple[str, list[str]]]:
        """Solr's analysis endpoint: every stage of the chain, named."""
        res = requests.get(
            f"{self.url}/analysis/field",
            params={"analysis.fieldname": "abstract", "analysis.fieldvalue": text, "wt": "json"},
            timeout=30,
        ).json()
        stages = res["analysis"]["field_names"]["abstract"]["index"]
        out = []
        for i in range(0, len(stages), 2):
            name = stages[i].rsplit(".", 1)[-1]
            out.append((name, [t["text"] for t in stages[i + 1]]))
        return out

    def _run(self, q: str, **params):
        return self.solr.search(q, **params)

    def __init__(self, base: str = SOLR_URL, core: str = "papers"):
        self.base = base.rstrip("/")
        self.core = core
        self.url = f"{self.base}/{core}"
        self.solr = pysolr.Solr(self.url, timeout=120, always_commit=False)

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        existing = {
            f["name"] for f in requests.get(f"{self.url}/schema/fields", timeout=30).json()["fields"]
        }
        new = [f for f in FIELDS if f["name"] not in existing]
        if new:
            requests.post(f"{self.url}/schema", json={"add-field": new}, timeout=60).raise_for_status()
        self.solr.delete(q="*:*", commit=True)

        t0 = time.perf_counter()
        batch = []
        for d in docs:
            batch.append(
                {
                    "id": d.id,
                    "title": d.title,
                    "abstract": d.abstract,
                    "authors": list(d.authors),
                    "categories": list(d.categories),
                    "update_date": f"{d.update_date}T00:00:00Z",  # Solr wants an instant
                    "version_count": d.version_count,
                    "doi": d.doi,
                }
            )
            if len(batch) >= 1000:
                self.solr.add(batch)
                batch = []
        if batch:
            self.solr.add(batch)
        self.solr.commit()  # Solr's refresh: nothing is visible before it
        self.solr.optimize()
        build_s = time.perf_counter() - t0

        info = requests.get(
            f"{self.base}/admin/cores", params={"action": "STATUS", "core": self.core}, timeout=30
        ).json()
        return IndexStats(
            docs=len(docs),
            build_s=build_s,
            size_bytes=int(info["status"][self.core]["index"].get("sizeInBytes", 0)),
            notes="after optimize",
        )

    def close(self) -> None:
        pass


# text_en ships with the default configset: tokenize, lowercase, stopwords,
# porter stemmer -- the job `analyzer: english` does in Elasticsearch.
FIELDS = [
    {"name": "title", "type": "text_en", "stored": True, "indexed": True},
    {"name": "abstract", "type": "text_en", "stored": True, "indexed": True},
    {"name": "authors", "type": "string", "stored": True, "indexed": True, "multiValued": True},
    {"name": "categories", "type": "string", "stored": True, "indexed": True, "multiValued": True},
    {"name": "update_date", "type": "pdate", "stored": True, "indexed": True, "docValues": True},
    {"name": "version_count", "type": "pint", "stored": True, "indexed": True, "docValues": True},
    {"name": "doi", "type": "string", "stored": True, "indexed": False},
]


if __name__ == "__main__":
    from core.common import run_engine_demo

    run_engine_demo(SolrEngine())
