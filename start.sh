#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$DIR:$PYTHONPATH"

HOST="${FREEBUFF_API_HOST:-0.0.0.0}"
PORT="${FREEBUFF_API_PORT:-8000}"

echo "Starting Freebuff API Gateway on http://${HOST}:${PORT}..."
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT"
