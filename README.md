# jodal-chat

조달데이터허브 지식 이관본 + agent loop. Live: https://jodal.orla.cc

`onyx/custom_chat_webservice`(agent loop 포트) + 조달데이터허브 131건 보고서 검색을
**2단 구조**로 묶었다. `api/` 가 에이전트 루프와 MCP 검색 서버를 모두 갖고,
`ui/` 가 그걸 소비한다.

## Layout

```
api/      FastAPI — agent loop + 조달데이터허브 MCP 서버   (127.0.0.1:8078)
  app/
    main.py        POST /chat (SSE|JSON) · GET /health · GET /tools · MOUNT /mcp
    loop.py        agent 루프 (Onyx run_loop 포트) + 답변 위생 15종
    llm_step.py    LLM 1스텝 (tool-call delta 조립, JSON 스캔)
    llm.py         LiteLLM 래퍼  ·  prompts.py  시스템 프롬프트  ·  models.py  패킷
    mcp_server.py  ★ MCP 서버 — 툴 11개를 /mcp 로 직접 서빙
    retrieval/     ★ 조달데이터허브 검색 엔진 (런타임)
      search_hybrid.py  BM25(bm25s+kiwipiepy) + Vector(Jina) → 가중 RRF
      stage_solve.py    S0~S5 결정적 스테이지 솔버
      slot_parser.py    쿼리 슬롯 파싱  ·  concept_tagger.py  개념 확장
      catalog.py        131건 카탈로그  ·  embed.py  Jina 임베딩
    tools/
      interface.py  Tool ABC  ·  runner.py  병렬 실행
      mcp_client.py / mcp_tool.py   MCP **클라이언트** (자기 /mcp 호출)
      search.py     search_reports · get_report_detail
      reports.py    resolve_items · item_children · item_detail · item_products
      hubpick.py    form_guide · concept_reports · value_lookup · family_map · catalog_health
      web_search.py · open_url.py · providers.py
  data/            런타임 데이터 2.6M
    catalog/  report_catalog_w2d.json (131건 원본 SSOT) + concepts + search_docs
    index/    bm25/ (bm25s) + vectors.json (Jina 1024d) + meta.json
    hubpick/  에셋 6파일 (form_guide/names/family/concept/value_topk/health)
    columns/ summaries/ eval/ details131.json + dim_vocab/stage_weights/family_overrides
  pipeline/        오프라인 배치 (인덱스 빌드·추출·평가) — 앱 미임포트
docs/              ONYX_GAP_ANALYSIS.md · MIGRATION_NOTES.md
ui/                Next.js 16 chat UI (:3002) — vercel/ai-chatbot fork
Makefile           dev/api/ui/start/stop/health/mcp-smoke/smoke/parity
```

## Run

```bash
# api: FastAPI. LLM 체인 xkiro → agnes → groq → gemini (키 있는 것만)
export XKIRO_API_KEY=... AGNES_API_KEY=... GROQ_API_KEY=... GEMINI_API_KEY=... JINA_API_KEY=...
cd api && uv sync && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8078

# ui: Next.js. api/ 를 소비
cd ui && CUSTOM_CHAT_BACKEND_URL=http://127.0.0.1:8078 pnpm start --port 3002

# 터널: jodal.orla.cc → 127.0.0.1:3002 (/etc/cloudflared/config.yml)
```

`make dev` / `make health` / `make mcp-smoke` 로 점검.

## 구조: 왜 api/ 하나에 MCP까지

`mcp/` Cloudflare Worker(1,870줄)를 2026-09-28 통합했다. 삭제 전 근거와 이식 대상은
`docs/MIGRATION_NOTES.md` 에 있다. 요지:

- 검색 알고리즘(BM25·RRF·stage solver·슬롯 파서)은 **원래 Python 에 있었다**.
  Worker 는 그걸 JS 로 옮겨 적었고, 두 구현을 `export_worker_index.py` 의
  parity assert 로만 맞췄다. 통합으로 이중 언어가 사라진다.
- 토크나이저가 진짜 `kiwipiepy` 로 돌아왔다. Worker 의 정규식 근사는
  **"시각화" 검색이 불가능**했다(인덱스 토큰은 `시각`, 쿼리 토큰은 `시각화` →
  idf 에 없어 버려짐). `make parity` 로 확인 가능.
- 툴 11개 이름·inputSchema 를 그대로 유지했다. agent 쪽은 `MCP_SERVERS` 대상만
  바뀐다.
- MCP 프로토콜은 유지한다. api 가 `/mcp` 로 서빙하고, agent 는 MCP **클라이언트**로
  자기 자신을 호출한다. 계층 분리가 살고 MCP 서버를 curl 로 독립 검증할 수 있다.

## 배포 형태

Vercel 이 아니다. Oracle Linux(`myalwaysfreearm`, OCI Always Free ARM) 에서
systemd 프로세스 2개(127.0.0.1) + cloudflared 터널로만 외부 노출.
Worker 공개 URL(`jodal.simply24365.workers.dev`)은 폐기했다.

## Jina 크레딧 (중요)

`JINA_API_KEY` 는 hybrid 검색의 **vector 절반**과 `web_search` 양쪽에 쓴다.
크레딧 소진(403) 시 `search_reports` 는 BM25 단독으로 강등하되
`scoring.degraded` 에 근거를 남긴다 — 조용히 품질이 떨어지지 않는다.
영구 실패는 circuit breaker 로 5분간 즉시 강등한다(retry 로 요청이 30초씩
밀리지 않게).

단, **Worker 와 달리 vector 가 빠지면 랭킹 품질이 실제로 떨어진다.**
`시각화 보고서` 처럼 BM25 만으로 정답이 안 잡히는 쿼리가 있다.

## 파이프라인 (오프라인)

```bash
cd api && uv run python pipeline/<script>.py
```

- `build_hybrid_index.py` — BM25 + Jina 임베딩 인덱스 빌드
- `export_worker_index.py` — lucene 재계산 + bm25s parity assert (Worker 산출물은
  더 이상 서빙하지 않는다. 회귀 검증용으로 `var/api-index/` 에만 쓴다)
- `parity_check.py` — `git show HEAD~N:mcp/worker.js` 로 복원한 Worker JS 를
  node 로 돌려 Python 결과와 대조(슬롯·탈락결정·점수 불변식)
- `export_hubpick_index.py` — `data/hubpick/` 6파일 생성
- `eval_*.py` — 검색 품질 평가. **vector 평가에는 Jina 크레딧이 필요**하다.

## Provenance

- 원본 `~/proj/jodal`(Worker + Python). 온전한 self-contained — 외부 경로 참조 0.
- agent loop 는 `onyx/custom_chat_webservice` 포트(구 Onyx `backend/onyx/`).
  무엇을 가져왔고 뺐는지는 `docs/ONYX_GAP_ANALYSIS.md` 가 항목별로 추적한다.
- 제외: `data/raw_excel/`, `data/raw_mstr_json/`, `data/raw/` (37M 원본, jodal 에만 존재).
- `ui/` 는 upstream `vercel/ai-chatbot` clone 이고 로컬 커밋 2개가 얹혀 있다
  (FastAPI 어댑터 + citation 수정). 이 repo 는 `ui/` 를 **추적하지 않는다**
  (`.gitignore`) — 옮기려면 upstream base 커밋을 기록한 채 통합이 필요하다.
