#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
export DATABASE_URL="${DATABASE_URL:-sqlite:///data/dev_fastapi.sqlite3}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8090}"
