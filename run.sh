#!/usr/bin/env bash
set -euo pipefail
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
PORT="${PORT:-8001}"
echo "Ops console:   http://127.0.0.1:${PORT}/"
echo "Customer app:  http://127.0.0.1:${PORT}/customer"
python -m uvicorn resolve.api.app:app --host 127.0.0.1 --port "$PORT"
