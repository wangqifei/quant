#!/usr/bin/env bash
# Start the dashboard on http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
exec python -m uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8000}"
