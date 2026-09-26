#!/usr/bin/env bash
# Start the Jev Alarm Triage demo at http://localhost:8000
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app:app --app-dir backend --port "${PORT:-8000}"
