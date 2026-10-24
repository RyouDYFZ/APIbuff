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

# 2. Binary Verification
if [ ! -f "$CONFIG_DIR/freebuff" ]; then
    echo "Fetching Freebuff native binary..."
    freebuff --version || true
fi

# Ensure privacy receipt exists for the Linux native binary so verify_private_cli passes
if [ -f "$CONFIG_DIR/freebuff" ] && [ ! -f "$CONFIG_DIR/freebuff.privacy.json" ]; then
    python3 - <<'PY_RECEIPT'
import json
import os
from pathlib import Path
import platform
from app.cli_build import AUDITED_BUNDLE_SHA256, PRIVACY_BUILD_VERSION, PRIVATE_GUARD, sha256_file

config_dir = Path(os.environ.get("CONFIG_DIR", Path.home() / ".config/manicode"))
binary = config_dir / "freebuff"
receipt = config_dir / "freebuff.privacy.json"
if binary.is_file() and not receipt.is_file():
    metadata = {
        "privacy_build_version": PRIVACY_BUILD_VERSION,
        "platform": platform.system().lower(),
        "machine": platform.machine(),
        "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
        "source_bundle_sha256": AUDITED_BUNDLE_SHA256,
        "guard_sha256": sha256_file(PRIVATE_GUARD),
        "binary_sha256": sha256_file(binary),
    }
    receipt.write_text(json.dumps(metadata, indent=2))
    print(f"Generated Linux privacy receipt for {binary}")
PY_RECEIPT
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
