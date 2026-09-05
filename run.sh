#!/usr/bin/env bash
# Start the web front end. Open http://127.0.0.1:8077
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8077}" "$@"
