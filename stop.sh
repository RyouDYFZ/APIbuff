#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
mode="${1:---all}"
case "$mode" in
    --all|--native|--docker) ;;
    *) echo "Usage: $0 [--all|--native|--docker]" >&2; exit 2 ;;
esac

echo "Stopping Freebuff API Gateway and workers ($mode)..."
status=0
if [ "$mode" != "--native" ]; then
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        docker compose stop --timeout 15 || status=1
    elif [ "$mode" = "--docker" ]; then
        echo "Docker Compose is not available." >&2
        status=1
    fi
fi
if [ "$mode" != "--docker" ]; then
    python3 -m app.lifecycle || status=1
fi
exit "$status"
