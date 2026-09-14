#!/usr/bin/env bash
# Bring up the whole POC: databases, data, API, UI.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> starting databases"
docker compose up -d
until docker exec geo-postgis pg_isready -U postgres -d geo >/dev/null 2>&1; do sleep 1; done
until docker exec geo-redis redis-cli ping >/dev/null 2>&1; do sleep 1; done
until docker exec geo-aerospike asinfo -v build >/dev/null 2>&1; do sleep 1; done

echo "==> python deps"
uv sync --quiet

if [ ! -f points.csv ]; then
  echo "==> generating dataset"
  uv run generate_data.py
fi

echo "==> loading every store (also prints the comparison tables)"
uv run compare.py

echo "==> node deps"
(cd ui && npm install --silent)

echo
echo "Now run these in two terminals:"
echo "  uv run uvicorn api:app --reload --port 8000"
echo "  cd ui && npm run dev"
echo
echo "Then open http://localhost:5173"
