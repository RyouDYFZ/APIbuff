#!/usr/bin/env bash
# Freebuff Cloud Container Entrypoint
set -e

CONFIG_DIR="${HOME:-/root}/.config/manicode"
mkdir -p "$CONFIG_DIR"

echo "============================================================"
echo "    Freebuff OpenAI-Compatible Cloud API Gateway"
echo "============================================================"

# 1. Seed the CLI's supported credential schema without shell JSON interpolation.
python3 - <<'PY_CREDENTIALS'
import os
from app.credentials import default_config_dir, write_cli_credentials

token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
if token:
    write_cli_credentials(default_config_dir(), token, os.environ.get("FREEBUFF_USER_EMAIL"))
    print("Upstream credentials configured.")
else:
    print("No upstream token supplied; existing login files are preserved.")
PY_CREDENTIALS

# 2. Fail closed unless the image carries the pinned official CLI. Workers
#    never run unverified code: the binary's SHA-256 must match the committed
#    registry, and telemetry is refused by the gateway proxy rather than by
#    modifying the executable.
python3 - <<'PY_VERIFY'
import os
from app.cli_build import verify_official_cli

binary = os.environ.get("FREEBUFF_BIN", "/opt/freebuff/official-cli/freebuff")
verify_official_cli(binary)
print(f"Verified pinned official Freebuff CLI: {binary}")
PY_VERIFY

# 3. Workspace root; each account's worker initializes its own Git repository.
WORKSPACE="${FREEBUFF_WORKER_WORKDIR:-/workspace}"
mkdir -p "$WORKSPACE"

# 4. Bind Host and Port
APP_HOST="${FREEBUFF_API_HOST:-0.0.0.0}"
APP_PORT="${PORT:-${FREEBUFF_API_PORT:-8000}}"

echo "🚀 Starting Freebuff API Gateway on http://${APP_HOST}:${APP_PORT}..."
exec python3 -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"
