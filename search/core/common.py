"""Document model, corpus loading, and the per-engine demo runner.

There is no query abstraction here on purpose. Each engine module writes its own
queries out as one function per query, in that engine's own syntax. To see what
Redis is actually asked, open engines/redis.py and read q5_fuzzy -- the command
in it is the one you would type into redis-cli.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Iterator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ARXIV_DOCS = os.path.join(DATA_DIR, "arxiv", "docs.jsonl")

PG_DSN = os.environ.get("PG_DSN", "postgresql://postgres:postgres@localhost:55433/search")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6381"))
ES_URL = os.environ.get("ES_URL", "http://localhost:9202")
OS_URL = os.environ.get("OS_URL", "http://localhost:9203")
SOLR_URL = os.environ.get("SOLR_URL", "http://localhost:8984/solr")

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMS = 384

@dataclass(frozen=True)
class Doc:
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



# --- dataset loading ---


def read_jsonl(path: str) -> Iterator[dict]:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_docs(path: str = ARXIV_DOCS, limit: int | None = None) -> list[Doc]:
    if not os.path.exists(path):
        raise SystemExit(
            f"missing {path}\nRun:  uv run python -m corpora.arxiv"
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



def run_engine_demo(engine, docs_limit: int = 5000, example: str | None = None) -> None:
    """Index a small corpus, show what the engine stored, then run every query.

    This is what `python -m engines.postgres` (or .redis, .lucene, ...) does.
    Each engine file is self-contained: no harness, no comparison, no scoring.
    """
    import inspect

    print(f"== {engine.name}: indexing {docs_limit:,} docs")
    docs = load_docs(limit=docs_limit)
    print("  ", engine.index(docs))

    example = example or docs[0].id
    print(f"\n{'=' * 72}\nWHAT {engine.name.upper()} STORED for {example}\n{'=' * 72}")
    print(engine.stored(example))

    print(f"\n{'=' * 72}\nQUERY PATTERNS\n{'=' * 72}")
    for name, fn in engine.all_queries():
        print(f"\n---- {name} " + "-" * (66 - len(name)))
        print(inspect.getsource(fn).strip())
        hits = fn()
        print(f"   {len(hits)} rows:" if hits else "   (no rows)")
        for hit in hits[:5]:
            print(f"     {hit.id:<14} {hit.title[:56]}")

    print(f"\n---- 7-facet " + "-" * 60)
    print(inspect.getsource(engine.q7_facet).strip())
    for label, count in engine.q7_facet(top=5):
        print(f"     {label:<14} {count}")

    print(f"\n---- match count (no rows fetched) " + "-" * 38)
    print(f"     {engine.q1_term_count():,} documents match")
    engine.close()
