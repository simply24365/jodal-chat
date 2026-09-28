# jodal-chat — local dev
#
# api/ (FastAPI, port 8078) = agent loop + 조달데이터허브 MCP 서버(/mcp)
# ui/  (Next.js,  port 3002) = chat UI, api/ 를 MCP + /chat 으로 소비
#
# 기동:
#   make dev     두 프로세스 포그라운드
#   make api     api/ 만
#   make ui      ui/ 만
#   make health  상태 점검
#
# Secrets: api/.env (AGNES 등) + 셸 passthrough (XKIRO/GROQ/GEMINI/JINA).
API_DIR    ?= api
UI_DIR     ?= ui
API_PORT   ?= 8078
UI_PORT    ?= 3002
UI_HOST    ?= 0.0.0.0
LOG        ?= /tmp/jodal_api_$(API_PORT).log
UI_LOG     ?= /tmp/jodal_ui_$(UI_PORT).log
PIDFILE    ?= /tmp/jodal_api_$(API_PORT).pid
UI_PIDFILE ?= /tmp/jodal_ui_$(UI_PORT).pid

# ui 는 CUSTOM_CHAT_BACKEND_URL 로 FastAPI 를 소비한다. agent loop 은
# config.MCP_SERVERS 기본값(127.0.0.1:$(API_PORT)/mcp/)로 MCP 서버를 호출한다.
export CUSTOM_CHAT_BACKEND_URL ?= http://127.0.0.1:$(API_PORT)

.PHONY: help setup sync dev api ui start stop logs health tools smoke mcp-smoke parity

help:
	@echo "targets:"
	@echo "  make dev         - api + ui 함께 (foreground)"
	@echo "  make api         - FastAPI 만 (127.0.0.1:$(API_PORT), /chat /health /tools /mcp)"
	@echo "  make ui          - Next.js 만 (:$(UI_PORT))"
	@echo "  make start/stop  - 백그라운드 + PID 파일"
	@echo "  make logs        - api 로그 tail"
	@echo "  make health      - /health /tools 점검"
	@echo "  make mcp-smoke   - MCP 툴 11개 목록 + 대표 호출"
	@echo "  make smoke       - 툴 없는 채팅 왕복"
	@echo "  make parity      - mcp/ Worker JS ↔ Python 검색 결과 대조"
	@echo "  make setup       - api/.env 생성"
	@echo "  make sync        - api 의존성 설치 (uv)"

setup:
	@[ -f $(API_DIR)/.env ] || cp $(API_DIR)/.env.example $(API_DIR)/.env
	@chmod 600 $(API_DIR)/.env
	@echo "$(API_DIR)/.env 생성됨. 키를 채우고: set -a; . $(API_DIR)/.env; make dev"

sync:
	cd $(API_DIR) && uv sync

dev: sync
	@echo "api → http://127.0.0.1:$(API_PORT)  (chat UI 는 ui/ 가 서빙)"
	cd $(UI_DIR) && pnpm start --port $(UI_PORT) --hostname $(UI_HOST)

api: sync
	cd $(API_DIR) && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT)

ui:
	cd $(UI_DIR) && CUSTOM_CHAT_BACKEND_URL=$(CUSTOM_CHAT_BACKEND_URL) pnpm start --port $(UI_PORT) --hostname $(UI_HOST)

start:
	@if [ -f "$(PIDFILE)" ] && kill -0 $$(cat "$(PIDFILE)") 2>/dev/null; then \
		echo "api already running (pid $$(cat $(PIDFILE)))"; exit 1; \
	fi
	cd $(API_DIR) && nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT) > $(LOG) 2>&1 & echo $$! > $(PIDFILE)
	cd $(UI_DIR)  && CUSTOM_CHAT_BACKEND_URL=$(CUSTOM_CHAT_BACKEND_URL) nohup pnpm start --port $(UI_PORT) --hostname $(UI_HOST) > $(UI_LOG) 2>&1 & echo $$! > $(UI_PIDFILE)
	@sleep 3; echo "api pid $$(cat $(PIDFILE)) log $(LOG)"; echo "ui  pid $$(cat $(UI_PIDFILE)) log $(UI_LOG)"

stop:
	-[ -f "$(PIDFILE)"    ] && kill $$(cat "$(PIDFILE)")    2>/dev/null; rm -f "$(PIDFILE)"
	-[ -f "$(UI_PIDFILE)" ] && kill $$(cat "$(UI_PIDFILE)") 2>/dev/null; rm -f "$(UI_PIDFILE)"
	@echo "stopped"

logs:
	tail -f $(LOG)

health:
	@curl -s http://127.0.0.1:$(API_PORT)/health; echo
	@curl -s http://127.0.0.1:$(API_PORT)/tools | head -c 400; echo

tools: health

# MCP 툴 목록 + 대표 호출 3건 (검색 / 카탈로그 / live g2b)
mcp-smoke:
	@echo "== tools/list =="
	@curl -s -m 20 -X POST http://127.0.0.1:$(API_PORT)/mcp/ -H "Content-Type: application/json" \
	  -H "Accept: application/json, text/event-stream" \
	  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
	  | $(API_DIR)/.venv/bin/python -c "import sys,json;print('\n'.join('  - '+t['name'] for t in json.load(sys.stdin)['result']['tools']))"
	@echo "== catalog_health =="
	@curl -s -m 20 -X POST http://127.0.0.1:$(API_PORT)/mcp/ -H "Content-Type: application/json" \
	  -H "Accept: application/json, text/event-stream" \
	  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"catalog_health","arguments":{}}}' \
	  | head -c 200; echo
	@echo "== search_reports =="
	@curl -s -m 60 -X POST http://127.0.0.1:$(API_PORT)/mcp/ -H "Content-Type: application/json" \
	  -H "Accept: application/json, text/event-stream" \
	  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_reports","arguments":{"query":"조달업체 면허 업종 등록 현황","top_k":3}}}' \
	  | head -c 400; echo
	@echo "== resolve_items (live g2b) =="
	@curl -s -m 60 -X POST http://127.0.0.1:$(API_PORT)/mcp/ -H "Content-Type: application/json" \
	  -H "Accept: application/json, text/event-stream" \
	  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"resolve_items","arguments":{"query":"의자","top_k":3}}}' \
	  | head -c 300; echo

smoke:
	@curl -s -m 180 -X POST http://127.0.0.1:$(API_PORT)/chat -H "Content-Type: application/json" \
	  -d '{"message":"Say hi in one short sentence, no tools.","stream":false,"web_search":false,"max_cycles":2}'; echo

# mcp/ Worker JS 를 로컬 구동해 Python 검색 결과와 대조 (회귀 검증)
# 레퍼런스가 없으면 자동 skip. 복원: git show HEAD~N:mcp/worker.js > /tmp/opencode/mcp_ref/worker.js
parity:
	@cd $(API_DIR) && .venv/bin/python pipeline/parity_check.py
