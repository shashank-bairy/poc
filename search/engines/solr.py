"""Solr: the same Lucene, schema-first.

Vocabulary vs Elasticsearch: core/collection not index, managed-schema not
mapping, string not keyword, fq not filter clause, edismax+qf not multi_match,
cursorMark not search_after, ZooKeeper instead of built-in coordination.
"""

from __future__ import annotations

import re
import time
from typing import Iterable

import pysolr
import requests

from core.common import SOLR_URL, Doc, Hit, IndexStats, Query

CORE = "papers"

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


class SolrEngine:
    name = "solr"

    def __init__(self, base: str = SOLR_URL, core: str = CORE):
        self.base = base.rstrip("/")
        self.core = core
        self.url = f"{self.base}/{core}"
        self.solr = pysolr.Solr(self.url, timeout=120, always_commit=False)
        self._stem_cache: dict[str, str] = {}

    def _ensure_schema(self) -> None:
        existing = {
            f["name"] for f in requests.get(f"{self.url}/schema/fields", timeout=30).json()["fields"]
        }
        new = [f for f in FIELDS if f["name"] not in existing]
        if new:
            requests.post(f"{self.url}/schema", json={"add-field": new}, timeout=60).raise_for_status()

    def index(self, docs: Iterable[Doc], with_vectors: bool = False) -> IndexStats:
        docs = list(docs)
        self._ensure_schema()
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
        size = int(info["status"][self.core]["index"].get("sizeInBytes", 0))
        return IndexStats(docs=len(docs), build_s=build_s, size_bytes=size, notes="after optimize")

    @staticmethod
    def _clean(text: str) -> str:
        """'(', ')', ':' and '-' are operators in the standard parser, so raw
        user text is a parse error. edismax tolerates it, which is why the
        boosted query needs no cleaning."""
        return " ".join(t for t in re.split(r"\W+", text) if len(t) > 1)

    def _stem(self, text: str) -> str:
        """One term through the field's analyzer, via Solr's analysis API. Cached
        so the extra round trip never lands inside a latency measurement."""
        if text in self._stem_cache:
            return self._stem_cache[text]
        resp = requests.get(
            f"{self.url}/analysis/field",
            params={"analysis.fieldname": "abstract", "analysis.fieldvalue": text, "wt": "json"},
            timeout=30,
        )
        stem = text.lower()
        try:
            # [stage_name, tokens, ...]; the last stage is the analyzed form.
            tokens = resp.json()["analysis"]["field_names"]["abstract"]["index"][-1]
            if tokens:
                stem = tokens[0]["text"]
        except (KeyError, IndexError, ValueError):
            pass
        self._stem_cache[text] = stem
        return stem

    def _params(self, q: Query) -> tuple[str, dict]:
        params: dict = {"rows": q.limit, "start": q.offset, "fl": "id,title,score"}
        text = self._clean(q.text)
        query = text

        if q.kind == "phrase":
            query = f'abstract:"{text}"'
        elif q.kind == "boolean":
            parts = [f"abstract:{t}" for t in q.must]
            if q.should:
                parts.append("(" + " OR ".join(f"abstract:{t}" for t in q.should) + ")")
            parts += [f"-abstract:{t}" for t in q.must_not]
            query = " AND ".join(parts)
        elif q.kind == "prefix":
            query = f"abstract:{text}*"
        elif q.kind == "fuzzy":
            # '~' is edit distance 2. The term must be stemmed first: Lucene's
            # parser runs only the multiterm chain (lowercasing) on fuzzy,
            # prefix and wildcard terms, and the index holds stems.
            query = f"abstract:{self._stem(text)}~"
        elif q.kind == "filtered":
            query = f"abstract:({text})"
            params["fq"] = f"update_date:[{q.date_from}T00:00:00Z TO *]"  # cached, unscored
        elif q.kind == "boosted":
            params["defType"] = "edismax"
            params["qf"] = " ".join(f"{f}^{w}" for f, w in q.boosts)
            query = q.text
        else:
            query = f"abstract:({text})"

        if q.kind == "facet":
            params.update(
                {"facet": "true", "facet.field": q.facet_field, "facet.limit": 10, "facet.mincount": 1}
            )
        if q.sort_field:
            params["sort"] = f"{q.sort_field} desc"
        if q.kind == "highlight":
            params.update({"hl": "true", "hl.fl": "abstract", "hl.snippets": 1, "hl.fragsize": 150})
        return query, params

    def search(self, q: Query) -> list[Hit]:
        query, params = self._params(q)
        res = self.solr.search(query, **params)
        hl = getattr(res, "highlighting", {}) or {}
        out = []
        for doc in res.docs:
            frags = hl.get(doc["id"], {}).get("abstract", [])
            out.append(
                Hit(
                    id=doc["id"],
                    score=float(doc.get("score", 0.0)),
                    title=doc.get("title", ""),
                    highlight=frags[0] if frags else "",
                )
            )
        return out

    def count(self, q: Query) -> int:
        query, params = self._params(q)
        params.update({"rows": 0, "start": 0})
        params.pop("sort", None)
        params.pop("hl", None)
        return int(self.solr.search(query, **params).hits)

    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]:
        query, params = self._params(q)
        params.update(
            {"rows": 0, "facet": "true", "facet.field": q.facet_field, "facet.limit": top, "facet.mincount": 1}
        )
        res = self.solr.search(query, **params)
        flat = res.facets["facet_fields"][q.facet_field]  # [value, count, value, count, ...]
        return [(flat[i], int(flat[i + 1])) for i in range(0, len(flat), 2)]

    def close(self) -> None:
        pass
