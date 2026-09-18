#!/usr/bin/env bash
# Services, jars, then every engine in turn: what it stored, and every query.
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

if [ ! -f data/arxiv/docs.jsonl ]; then
  echo
  echo "No corpus yet. It needs a Kaggle token at ~/.kaggle/kaggle.json:"
  echo "    uv run python -m corpora.arxiv --limit 200000"
  exit 1
fi

for engine in postgres redis lucene elasticsearch opensearch solr; do
  echo
  echo "############################################################ $engine"
  uv run python -m "engines.$engine"
done
