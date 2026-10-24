#!/usr/bin/env bash
echo "Stopping Freebuff API Gateway and workers..."
PIDS=$(lsof -ti :8000 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    kill -9 $PIDS 2>/dev/null || true
    echo "Service stopped."
else
    echo "No service running on port 8000."
fi
