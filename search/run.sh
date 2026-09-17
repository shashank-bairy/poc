#!/usr/bin/env bash
# Services, jars, data, then both comparison passes.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> starting services"
docker compose up -d
until docker exec search-postgres pg_isready -U postgres -d search >/dev/null 2>&1; do sleep 1; done
until docker exec search-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done
until curl -fs http://localhost:9202/_cluster/health >/dev/null 2>&1; do sleep 2; done
until curl -fs http://localhost:9203/_cluster/health >/dev/null 2>&1; do sleep 2; done
until curl -fs http://localhost:8984/solr/admin/info/system >/dev/null 2>&1; do sleep 2; done

echo "==> python deps"
uv sync --quiet

echo "==> lucene jars"
./lucene_raw/fetch_jars.sh

if [ ! -f data/beir/scifact/docs.jsonl ]; then
  echo "==> relevance set (BEIR SciFact)"
  uv run python -m corpora.beir
fi

if [ ! -f data/arxiv/docs.jsonl ]; then
  echo
  echo "No arXiv corpus. It needs a Kaggle token at ~/.kaggle/kaggle.json:"
  echo "    uv run python -m corpora.arxiv --limit 200000"
  echo "Falling back to SciFact for the performance pass -- 5k docs, so those"
  echo "latencies are indicative only, and it carries no categories or dates."
  CORPUS=data/beir/scifact/docs.jsonl
else
  CORPUS=data/arxiv/docs.jsonl
fi

echo
echo "==> performance: same corpus, same query suite, every engine"
uv run python -m bench.compare --docs "$CORPUS"

echo
echo "==> relevance: BEIR judgements, nDCG@10 / recall@10 / MRR"
uv run python -m bench.evaluate

echo
echo "Phase 2 (embeddings + hybrid RRF), ~2 GB of torch:"
echo "    uv sync --extra embed"
echo "    uv run python -m bench.evaluate --engines elasticsearch,dense,hybrid"
