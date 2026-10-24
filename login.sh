#!/usr/bin/env bash
# Freebuff CLI Login Helper & Token Synchronizer
# Launches the official browser OAuth flow, verifies Keychain storage,
# and automatically refreshes the background Freebuff API Gateway.

set -e

FREEBUFF_BIN="${FREEBUFF_BIN:-$HOME/.config/manicode/freebuff}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

echo "============================================================"
echo "   Freebuff Authentication & OAuth Token Refresh"
echo "============================================================"
echo "This will open your browser to log in to Freebuff."
echo ""

if [ ! -f "$FREEBUFF_BIN" ]; then
    echo "❌ Freebuff binary not found at $FREEBUFF_BIN"
    exit 1
fi

"$FREEBUFF_BIN" login

echo ""
echo "Verifying new credentials..."
TOKEN=""
if command -v security >/dev/null 2>&1; then
    TOKEN=$(security find-generic-password -s "freebuff-cli" -w 2>/dev/null || true)
fi
if [ -z "$TOKEN" ] && [ -f "$HOME/.config/manicode/credentials.json" ]; then
    TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.config/manicode/credentials.json')).get('default', {}).get('authToken', ''))" 2>/dev/null || true)
fi

if [ -n "$TOKEN" ]; then
    echo "✓ Found active auth token: ${TOKEN:0:8}..."
    VERIFY_RESP=$(curl -s "https://www.codebuff.com/api/v1/me?fields=id,email" -H "Authorization: Bearer $TOKEN" || true)
    if [[ "$VERIFY_RESP" == *"email"* ]]; then
        echo "✓ Authentication verified successfully with Freebuff servers!"
        echo "  Account details: $VERIFY_RESP"
        
        # Check if gateway service is running on port 8000
        if curl -s http://127.0.0.1:8000/health >/dev/null 2>&1; then
            echo ""
            echo "Restarting Freebuff API worker to reload fresh credentials..."
            cd "$SCRIPT_DIR" && ./stop.sh >/dev/null 2>&1 || true
            sleep 1
            nohup ./start.sh >/dev/null 2>&1 &
            sleep 2
            echo "✓ Freebuff API Gateway restarted with new credentials."
        fi
        echo "🎉 Login complete! You can now resume using your API Gateway."
    else
        echo "⚠️ Token found in Keychain, but verification returned: $VERIFY_RESP"
    fi
else
    echo "❌ No token found in Keychain. Please try running login again."
fi
