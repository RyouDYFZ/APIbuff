#!/usr/bin/env bash
# Freebuff OAuth Login Helper (Supports both native shell & Docker container)
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Prefer the verified private CLI: the official binary ships active PostHog
# telemetry even for `login`. Fall back to the launcher only when no private
# build exists yet (run scripts/build_private_cli.py first).
PRIVATE_BIN="${FREEBUFF_BIN:-$DIR/.freebuff/private-cli/freebuff}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^freebuff-api-gateway$"; then
    echo "============================================================"
    echo "    Freebuff Container OAuth Login"
    echo "============================================================"
    docker exec -it freebuff-api-gateway freebuff login
    echo "Restarting container worker..."
    docker restart freebuff-api-gateway >/dev/null 2>&1
else
    echo "============================================================"
    echo "    Freebuff Native CLI OAuth Login"
    echo "============================================================"
    if [ -x "$PRIVATE_BIN" ]; then
        "$PRIVATE_BIN" login
    elif command -v freebuff >/dev/null 2>&1; then
        echo "⚠️  Using the official CLI: build a private one with"
        echo "    python3 scripts/build_private_cli.py --fetch"
        freebuff login
    else
        echo "❌ freebuff CLI not found. Build the private CLI with:"
        echo "   python3 scripts/build_private_cli.py --fetch"
        exit 1
    fi
fi

echo ""
echo "Verifying service status..."
sleep 2
curl -s http://127.0.0.1:8000/health || true
echo ""
