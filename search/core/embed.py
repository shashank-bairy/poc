"""Local embeddings with all-MiniLM-L6-v2 (384 dims, CPU-friendly).

    uv sync --extra embed
    uv run python -m core.embed --limit 20000
"""

from __future__ import annotations

import argparse
import os

import numpy as np

from core.common import ARXIV_DOCS, EMBED_DIMS, EMBED_MODEL, Doc, load_docs

_model = None


def model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # pulls in torch

        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def encode(texts: list[str], batch_size: int = 128) -> np.ndarray:
    """Normalized, so cosine similarity is a plain dot product."""
    return model().encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=len(texts) > 1000,
        convert_to_numpy=True,
    )


def doc_text(d: Doc) -> str:
    """MiniLM's window is 256 word pieces; the rest of a long abstract is
    silently dropped, which is why production RAG systems chunk."""
    return f"{d.title}. {d.abstract}"[:2000]


def vectors_path(docs_path: str) -> str:
    return os.path.join(os.path.dirname(docs_path), "vectors.npz")


def build(docs_path: str, limit: int | None) -> str:
    docs = load_docs(docs_path, limit=limit)
    mat = encode([doc_text(d) for d in docs])
    out = vectors_path(docs_path)
    np.savez_compressed(out, ids=np.array([d.id for d in docs]), vectors=mat.astype(np.float32))
    print(f"{len(docs):,} vectors x {mat.shape[1]} dims -> {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out


def load(docs_path: str = ARXIV_DOCS) -> dict[str, np.ndarray]:
    path = vectors_path(docs_path)
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}\nRun:  uv run python -m core.embed")
    data = np.load(path, allow_pickle=False)
    return dict(zip((str(i) for i in data["ids"]), data["vectors"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", default=ARXIV_DOCS)
    ap.add_argument("--limit", type=int, default=20_000)
    args = ap.parse_args()
    print(f"model {EMBED_MODEL} ({EMBED_DIMS} dims)")
    build(args.docs, args.limit)


if __name__ == "__main__":
    main()
