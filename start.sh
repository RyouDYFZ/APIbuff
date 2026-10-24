#!/usr/bin/env bash
# Freebuff Standalone Decoupled Local Server Starter
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PATH="${HOME}/.gemini/antigravity/bin:/Applications/Docker.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Honest readiness check: HTTP 200 alone is not enough, the payload must also
# report at least one running worker (/health returns 503 otherwise).
gateway_ready() {
    local port="$1"
    local body
    body="$(curl -s -f "http://127.0.0.1:${port}/health" 2>/dev/null)" || return 1
    grep -q '"worker_count":0' <<<"$body" && return 1
    return 0
}

if [ "$1" = "--native" ]; then
    echo "Starting Freebuff API Gateway in native mode..."
    # Stop gateways registered by this checkout precisely; never broad pkill
    # patterns that can match unrelated processes.
    python3 -m app.lifecycle >/dev/null 2>&1 || true
    export PYTHONPATH="$DIR:$PYTHONPATH"
    APP_HOST="${FREEBUFF_API_HOST:-127.0.0.1}"
    APP_PORT="${FREEBUFF_API_PORT:-8000}"
    if [ -x "$DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$DIR/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3 || command -v python || echo "/usr/bin/python3")"
    fi
    exec "$PYTHON_BIN" -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"
fi

echo "============================================================"
echo "    Launching Freebuff Standalone Decoupled Server (Docker)"
echo "============================================================"

docker compose up -d

echo ""
echo "Waiting for gateway readiness (healthy worker required)..."
GATEWAY_PORT="${FREEBUFF_API_PORT:-8000}"
for i in {1..30}; do
    if gateway_ready "$GATEWAY_PORT"; then
        echo "✓ Standalone Freebuff Server is serving on http://127.0.0.1:${GATEWAY_PORT}"
        exit 0
    fi
    sleep 1
done

echo "⚠️ Gateway is not healthy yet. Check logs with: docker logs freebuff-api-gateway"
echo "  (a 503 /health means no worker could start: credentials or proxy configuration)"
exit 1
