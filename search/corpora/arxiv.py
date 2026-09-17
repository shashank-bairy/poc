"""arXiv metadata snapshot -> data/arxiv/docs.jsonl.

A snapshot, not the API: pagination, rate limits and a shifting corpus would
make runs incomparable. Needs a Kaggle API token -- either ~/.kaggle/kaggle.json
(username + key) or ~/.kaggle/access_token (the newer KGAT_ form), chmod 600.

    uv run python -m corpora.arxiv --limit 200000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from core.common import ARXIV_DOCS, DATA_DIR, write_jsonl

KAGGLE_SLUG = "Cornell-University/arxiv"  # Kaggle slugs move; verify before blaming this
RAW_NAME = "arxiv-metadata-oai-snapshot.json"
RAW_DIR = os.path.join(DATA_DIR, "arxiv", "raw")


def download() -> str:
    raw_path = os.path.join(RAW_DIR, RAW_NAME)
    if os.path.exists(raw_path):
        print(f"raw snapshot already present: {raw_path}")
        return raw_path

    os.makedirs(RAW_DIR, exist_ok=True)
    print(f"downloading {KAGGLE_SLUG} (~4 GB, one time)")
    try:
        subprocess.run(
            ["kaggle", "datasets", "download", "-d", KAGGLE_SLUG, "-p", RAW_DIR, "--unzip"],
            check=True,
        )
    except FileNotFoundError:
        sys.exit("kaggle CLI not found. Run:  uv sync")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"kaggle download failed ({exc.returncode}); check the token and the slug")
    if not os.path.exists(raw_path):
        sys.exit(f"downloaded, but {RAW_NAME} is not in {RAW_DIR}")
    return raw_path


def split_authors(raw: dict) -> list[str]:
    parsed = raw.get("authors_parsed")  # [[last, first, suffix], ...]
    if parsed:
        out = []
        for parts in parsed:
            name = " ".join(p.strip() for p in reversed(parts) if p and p.strip())
            if name:
                out.append(name)
        return out
    return [a.strip() for a in (raw.get("authors") or "").split(",") if a.strip()]


def normalize(raw: dict) -> dict:
    return {
        "id": raw["id"],
        # The snapshot wraps titles and abstracts over several lines.
        "title": " ".join((raw.get("title") or "").split()),
        "abstract": " ".join((raw.get("abstract") or "").split()),
        "authors": split_authors(raw),
        "categories": (raw.get("categories") or "").split(),
        "update_date": raw.get("update_date") or "1970-01-01",
        "version_count": len(raw.get("versions") or []) or 1,
        "doi": raw.get("doi") or "",
    }


def cs_papers(raw_path: str, limit: int, prefix: str):
    """Streamed: the snapshot is ~4 GB."""
    kept = 0
    with open(raw_path) as fh:
        for line in fh:
            if kept >= limit:
                return
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            cats = (raw.get("categories") or "").split()
            if prefix and not any(c.startswith(prefix) for c in cats):
                continue
            doc = normalize(raw)
            if not doc["abstract"]:
                continue
            kept += 1
            yield doc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200_000)
    ap.add_argument("--category", default="cs.", help="category prefix filter ('' = all)")
    ap.add_argument("--raw", help="path to an already-downloaded snapshot")
    ap.add_argument("--out", default=ARXIV_DOCS)
    args = ap.parse_args()

    n = write_jsonl(args.out, cs_papers(args.raw or download(), args.limit, args.category))
    print(f"wrote {n:,} docs -> {args.out}  ({os.path.getsize(args.out) / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
