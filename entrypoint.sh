#!/usr/bin/env bash
# Freebuff Cloud Container Entrypoint
set -e

CONFIG_DIR="${HOME:-/root}/.config/manicode"
mkdir -p "$CONFIG_DIR"

echo "============================================================"
echo "    Freebuff OpenAI-Compatible Cloud API Gateway"
echo "============================================================"

# 1. Automatic Cloud Credential Seeding
AUTH_KEY="${CODEBUFF_API_KEY:-$FREEBUFF_AUTH_TOKEN}"
if [ -n "$AUTH_KEY" ]; then
    echo "✓ Found upstream auth key in environment. Seeding credentials.json..."
    cat <<EOF > "$CONFIG_DIR/credentials.json"
{
  "default": {
    "authToken": "$AUTH_KEY",
    "id": "${FREEBUFF_USER_ID:-cloud-user}",
    "email": "${FREEBUFF_USER_EMAIL:-cloud@freebuff.local}",
    "tokenStore": "file",
    "name": "Cloud Deployment"
  }
}
EOF
    cat <<EOF > "$CONFIG_DIR/settings.json"
{
  "mode": "DEFAULT",
  "adsEnabled": true,
  "freebuffModelKey": "m-7e20df6765",
  "hasSubmittedFirstPrompt": true
}
EOF
    chmod 600 "$CONFIG_DIR/credentials.json"
    echo "✓ Credentials configured headlessly."
else
    echo "ℹ️ No CODEBUFF_API_KEY provided. Gateway will operate in standby/fallback mode."
    echo "   Pass -e CODEBUFF_API_KEY=<your_token> or run ./login.sh inside container."
fi

# 2. Binary Verification
if [ ! -f "$CONFIG_DIR/freebuff" ]; then
    echo "Fetching Freebuff native binary..."
    freebuff --version || true
fi

# 3. Workspace Initialization
WORKSPACE="${FREEBUFF_WORKER_WORKDIR:-/workspace}"
mkdir -p "$WORKSPACE"
if [ ! -d "$WORKSPACE/.git" ]; then
    cd "$WORKSPACE"
    git config --global user.name "Cloud Developer" 2>/dev/null || true
    git config --global user.email "cloud@freebuff.local" 2>/dev/null || true
    git init -b main 2>/dev/null || git init
    echo "# Cloud Project Workspace" > README.md
    git add . 2>/dev/null || true
    git commit -m "Initial commit" 2>/dev/null || true
fi

# 4. Bind Host and Port
APP_HOST="${FREEBUFF_API_HOST:-0.0.0.0}"
APP_PORT="${PORT:-${FREEBUFF_API_PORT:-8000}}"

echo "🚀 Starting Freebuff API Gateway on http://${APP_HOST}:${APP_PORT}..."
exec python3 -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"
