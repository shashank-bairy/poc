"""Document model, query suite, and timing harness shared by every engine.

The central design decision is `Query`. The suite has eleven entries and six
engines, and 66 hand-tuned query strings would make the comparison meaningless
-- you would be comparing how carefully each string was written. So a query is
described *structurally* (kind + operands) and every engine translates the same
structure into its own dialect. Where an engine cannot express a kind it raises
Unsupported, which is itself a finding.

The eleven kinds and what each probes:

    term       baseline latency and baseline scoring
    phrase     whether positions are indexed
    boolean    query DSL expressiveness (AND / OR / NOT)
    prefix     autocomplete strategy (n-grams vs prefix query vs suggester)
    fuzzy      edit distance; Postgres needs a separate trigram index
    filtered   filter/query separation and filter caching
    facet      multi-valued keyword aggregation
    sort       relevance scoring vs doc-value sorting
    page       deep paging (from/size vs search_after vs LIMIT/OFFSET)
    highlight  stored fields and term vectors
    boosted    per-field weighting
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Protocol

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ARXIV_DOCS = os.path.join(DATA_DIR, "arxiv", "docs.jsonl")
BEIR_DIR = os.path.join(DATA_DIR, "beir")

# Ports offset from the geo POC's so both can run at once.
PG_DSN = os.environ.get("PG_DSN", "postgresql://postgres:postgres@localhost:55433/search")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6381"))
ES_URL = os.environ.get("ES_URL", "http://localhost:9202")
OS_URL = os.environ.get("OS_URL", "http://localhost:9203")
SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8984/solr")

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMS = 384


class Unsupported(Exception):
    """Engine cannot express a query kind. A result, not a bug."""


@dataclass(frozen=True)
class Doc:
    """arXiv papers and BEIR corpus entries both normalize to this."""

    id: str
    title: str
    abstract: str
    authors: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    update_date: str = "1970-01-01"
    version_count: int = 1
    doi: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "Doc":
        return cls(
            id=str(raw["id"]),
            title=raw.get("title", "") or "",
            abstract=raw.get("abstract", "") or "",
            authors=tuple(raw.get("authors", []) or ()),
            categories=tuple(raw.get("categories", []) or ()),
            update_date=raw.get("update_date") or "1970-01-01",
            version_count=int(raw.get("version_count", 1) or 1),
            doi=raw.get("doi") or "",
        )

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "categories": list(self.categories),
            "update_date": self.update_date,
            "version_count": self.version_count,
            "doi": self.doi,
        }


@dataclass(frozen=True)
class Hit:
    # Scores are comparable within an engine, never across engines.
    id: str
    score: float
    title: str = ""
    highlight: str = ""


@dataclass(frozen=True)
class IndexStats:
    docs: int
    build_s: float
    size_bytes: int
    notes: str = ""

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1024 / 1024


@dataclass(frozen=True)
class Query:
    """kind: term phrase boolean prefix fuzzy filtered facet sort page highlight boosted."""

    kind: str
    text: str = ""
    must: tuple[str, ...] = ()
    should: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    date_from: str | None = None
    facet_field: str = "categories"
    sort_field: str | None = None  # None = by relevance
    offset: int = 0
    limit: int = 10
    boosts: tuple[tuple[str, float], ...] = ()
    label: str = ""

    def name(self) -> str:
        return self.label or f"{self.kind}:{self.text}"


QUERY_SUITE: tuple[Query, ...] = (
    Query(kind="term", text="retrieval", label="1 term"),
    Query(kind="phrase", text="attention mechanism", label="2 phrase"),
    Query(
        kind="boolean",
        must=("retrieval",),
        should=("dense", "sparse"),
        must_not=("image",),
        label="3 boolean",
    ),
    Query(kind="prefix", text="quant", label="4 prefix"),
    Query(kind="fuzzy", text="transfomer", label="5 fuzzy"),
    Query(kind="filtered", text="graph neural", date_from="2023-01-01", label="6 filter+text"),
    Query(kind="facet", text="retrieval", facet_field="categories", label="7 facet"),
    Query(kind="sort", text="retrieval", sort_field="update_date", label="8 sort by date"),
    Query(kind="page", text="learning", offset=4990, limit=10, label="9 deep page"),
    Query(kind="highlight", text="knowledge distillation", label="10 highlight"),
    Query(
        kind="boosted",
        text="language model",
        boosts=(("title", 5.0), ("abstract", 1.0)),
        label="11 boosted",
    ),
)


class SearchEngine(Protocol):
    name: str

    def index(self, docs: Iterable[Doc]) -> IndexStats: ...
    def search(self, q: Query) -> list[Hit]: ...
    def count(self, q: Query) -> int: ...
    def facet(self, q: Query, top: int = 10) -> list[tuple[str, int]]: ...
    def close(self) -> None: ...


def read_jsonl(path: str) -> Iterator[dict]:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_docs(path: str = ARXIV_DOCS, limit: int | None = None) -> list[Doc]:
    if not os.path.exists(path):
        raise SystemExit(
            f"missing {path}\nRun:  uv run python -m corpora.arxiv   (or -m corpora.beir)"
        )
    docs = []
    for i, raw in enumerate(read_jsonl(path)):
        if limit is not None and i >= limit:
            break
        docs.append(Doc.from_json(raw))
    return docs


def write_jsonl(path: str, rows: Iterable[dict]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
            n += 1
    return n


@dataclass
class Timing:
    p50_ms: float
    p95_ms: float
    runs: int
    hits: int
    error: str = ""

    @classmethod
    def failed(cls, msg: str) -> "Timing":
        return cls(0.0, 0.0, 0, 0, msg)


def timed(fn, *args, repeat: int = 20, warmup: int = 3) -> tuple[Timing, object]:
    """Warm runs only: a cold-cache number on a laptop measures the page cache."""
    try:
        for _ in range(warmup):
            fn(*args)
    except Unsupported as exc:
        return Timing.failed(f"unsupported: {exc}"), None
    except Exception as exc:  # noqa: BLE001 - engines fail in engine-specific ways
        return Timing.failed(f"{type(exc).__name__}: {exc}"), None

    samples = []
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn(*args)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    n_hits = len(result) if hasattr(result, "__len__") else int(result or 0)
    return (
        Timing(
            p50_ms=statistics.median(samples),
            p95_ms=samples[min(len(samples) - 1, int(len(samples) * 0.95))],
            runs=repeat,
            hits=n_hits,
        ),
        result,
    )


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = [
        "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) for row in rows
    ]
    return "\n".join([line, sep, *body])
