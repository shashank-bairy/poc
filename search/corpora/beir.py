"""BEIR set -> docs.jsonl + queries.jsonl + qrels.tsv.

qrels are human relevance judgements, which is the whole point: self-labeled
query sets are biased towards whichever engine was built first. Small corpus,
so this is where relevance is measured and never latency.

    uv run python -m corpora.beir --dataset scifact
"""

from __future__ import annotations

import argparse
import io
import json
import os
import zipfile

import requests

from core.common import BEIR_DIR, write_jsonl

BASE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"
DATASETS = ("scifact", "nfcorpus", "trec-covid", "fiqa")


def download(dataset: str, dest: str) -> str:
    unpacked = os.path.join(dest, dataset)
    if os.path.exists(os.path.join(unpacked, "corpus.jsonl")):
        print(f"already downloaded: {unpacked}")
        return unpacked
    url = f"{BASE}/{dataset}.zip"
    print(f"downloading {url}")
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(dest)
    return unpacked


def to_docs(corpus_path: str):
    """BEIR has no authors, categories or dates; those stay empty."""
    with open(corpus_path) as fh:
        for line in fh:
            raw = json.loads(line)
            yield {
                "id": raw["_id"],
                "title": raw.get("title", ""),
                "abstract": raw.get("text", ""),
                "authors": [],
                "categories": [],
                "update_date": "1970-01-01",
                "version_count": 1,
                "doi": "",
            }


def to_queries(queries_path: str, wanted: set[str]):
    with open(queries_path) as fh:
        for line in fh:
            raw = json.loads(line)
            if raw["_id"] in wanted:
                yield {"id": raw["_id"], "text": raw["text"]}


def load_qrels(path: str) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with open(path) as fh:
        if "query" not in fh.readline():
            fh.seek(0)  # no header after all
        for line in fh:
            parts = line.strip().split("\t")
            if len(parts) != 3:
                continue
            qid, did, score = parts
            if int(score) > 0:
                qrels.setdefault(qid, {})[did] = int(score)
    return qrels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="scifact", choices=DATASETS)
    ap.add_argument("--split", default="test")
    args = ap.parse_args()

    src = download(args.dataset, BEIR_DIR)
    out_dir = os.path.join(BEIR_DIR, args.dataset)
    qrels = load_qrels(os.path.join(src, "qrels", f"{args.split}.tsv"))

    n_docs = write_jsonl(os.path.join(out_dir, "docs.jsonl"), to_docs(os.path.join(src, "corpus.jsonl")))
    # Read fully before writing: the archive unpacks into the output directory,
    # so streaming would truncate queries.jsonl mid-read.
    queries = list(to_queries(os.path.join(src, "queries.jsonl"), set(qrels)))
    n_q = write_jsonl(os.path.join(out_dir, "queries.jsonl"), queries)

    with open(os.path.join(out_dir, "qrels.tsv"), "w") as fh:
        fh.write("query-id\tcorpus-id\tscore\n")
        for qid, docs in qrels.items():
            for did, score in docs.items():
                fh.write(f"{qid}\t{did}\t{score}\n")

    judged = sum(len(v) for v in qrels.values())
    print(f"{args.dataset}: {n_docs:,} docs, {n_q:,} queries, {judged:,} judgements -> {out_dir}")


if __name__ == "__main__":
    main()
