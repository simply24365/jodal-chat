#!/bin/bash
# jodal-api (port 8078) — reboot-safe launcher.
# Secrets: api/.env (file 600, AGNES apihub key + models).
# GROQ/GEMINI/JINA keys are shell passthrough (not in .env).
# AGNES_API_KEY == apihub key (NOT shim EDGE_SECRET — different keys, do not mix).
set -u
# 호출 셸에 stale 키가 있으면 .env가 덮어써지지 않으므로, AGNES_API_KEY만 미리 비운다.
# (다른 키는 셸 passthrough 유지: GROQ/GEMINI/JINA는 셸에만 존재)
unset AGNES_API_KEY
API_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$API_DIR" || exit 1
if [ ! -f .env ]; then echo "missing api/.env" >&2; exit 1; fi
set -a; . ./.env; set +a
: "${AGNES_API_KEY:?AGNES_API_KEY missing in api/.env}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8078}"
