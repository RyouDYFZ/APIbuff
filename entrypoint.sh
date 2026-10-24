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

# 2. Fail closed unless the image carries a genuine private CLI build. The
#    check verifies the embedded PostHog opt-out patch plus its build receipt;
#    a receipt generated for an official binary is rejected here, so workers
#    can never run unverified telemetry-bearing code.
python3 - <<'PY_VERIFY'
import os
from app.cli_build import verify_private_cli

binary = os.environ.get("FREEBUFF_BIN", "/opt/freebuff/private-cli/freebuff")
verify_private_cli(binary)
print(f"Verified private Freebuff CLI build (PostHog opt-out embedded): {binary}")
PY_VERIFY

# 3. Workspace root; each account's worker initializes its own Git repository.
WORKSPACE="${FREEBUFF_WORKER_WORKDIR:-/workspace}"
mkdir -p "$WORKSPACE"

# 4. Bind Host and Port
APP_HOST="${FREEBUFF_API_HOST:-0.0.0.0}"
APP_PORT="${PORT:-${FREEBUFF_API_PORT:-8000}}"

echo "🚀 Starting Freebuff API Gateway on http://${APP_HOST}:${APP_PORT}..."
exec python3 -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"
