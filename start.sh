#!/usr/bin/env bash
# Freebuff Standalone Decoupled Local Server Starter
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PATH="${HOME}/.gemini/antigravity/bin:/Applications/Docker.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ "$1" = "--native" ]; then
    echo "Starting Freebuff API Gateway in native mode..."
    pkill -9 -f "freebuff.*--cwd" 2>/dev/null || true
    pkill -9 -f "zsh -l -c.*freebuff" 2>/dev/null || true
    export PYTHONPATH="$DIR:$PYTHONPATH"
    APP_HOST="${FREEBUFF_API_HOST:-127.0.0.1}"
    APP_PORT="${FREEBUFF_API_PORT:-8000}"
    PYTHON_BIN="$(command -v python3 || command -v python || echo "/usr/bin/python3")"
    exec "$PYTHON_BIN" -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"
fi

echo "============================================================"
echo "    Launching Freebuff Standalone Decoupled Server (Docker)"
echo "============================================================"

docker compose up -d

echo ""
echo "Waiting for gateway readiness..."
GATEWAY_PORT="${FREEBUFF_API_PORT:-8000}"
for i in {1..15}; do
    if curl -s "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
        echo "✓ Standalone Freebuff Server is active on http://127.0.0.1:${GATEWAY_PORT}"
        exit 0
    fi
    sleep 1
done

echo "⚠️ Container started, check logs with: docker logs freebuff-api-gateway"
