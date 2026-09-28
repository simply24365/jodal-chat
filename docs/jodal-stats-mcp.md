# Jodal Stats MCP (`POST /mcp`)

조달데이터허브 통계테이블(131건) 조회용 MCP 서버. `worker.js` 내장, 판단 없음 — 사실만 반환하고 선택·되묻기·컷오프는 호출 측(LLM)이 결정한다.

## Tools

### `search_reports`

원하는 통계 1문장 → 후보 `object[]` (score 내림차순).

```jsonc
// input
{ "query": "여성기업 계약금액 지역별", "top_k": 10, "vector_weight": 0.7 }
// output (content[0].text JSON)
{ "query_slots": { "family": {"value": null, "confidence": "none"},
    "dims": ["지역"], "dims_explicit": ["지역"], "excluded_dims": [],
    "channel": null, "item_scope": null, "metric": ["계약금액"],
    "visual": false, "concepts": ["contract_amount", "region", "female_biz"] },
  "candidates": [
    { "report_id": "00289", "name": "지역별 사회적약자기업…",
      "official_id": "UI-ADOXFA-132R", "stage_score": 0.3624,
      "signals": { "family": "none", "dims_coverage": 1.0,
        "metric_hit": ["사회적약자기업건수"], "concept_hits": ["region"],
        "name_mention": false, "bm25_rank": 7, "vec_rank": 3 },
      "dropped": false, "drop_reason": null, "views": 1234 } ],
  "scoring": { "method": "weighted_rrf+stage",
    "vector_weight": 0.7, "rrf_k": 60, "pool": 50 },
  "catalog_ver": "w2d-20260919|jina-embeddings-v3-1024" }
```

- 파이프라인: `parseSlots` → BM25(개념확장 쿼리) + Jina vector(원문 쿼리) → 가중 RRF → `stageSolve`. clarify·confidence·No Match 제외, S0 탈락도 `dropped:true`로 포함.
- 가중 RRF: `score = (1-w)/(K+rb) + w/(K+rv)`, `K=60`(`meta.rrf_k`), 미포함 rank는 `pool+1`. 기본 `w=0.7`.
- 토큰: Top10+신호 ≈ 2~3K. 131건 통째 반환 금지.

### `get_report_detail`

```jsonc
// input
{ "report_id": "00289" }
// output
{ "report_id": "00289", "name": "…", "official_id": "UI-ADOXFA-132R",
  "reptId": "00289", "desc": "…", "conds": ["납품요구일자(From)_전일(date)", …],
  "metrics": […], "dims": […], "concepts": ["vendor", …],
  "family": "쇼핑몰",
  "links": { "move": "https://data.g2b.go.kr/link/…", "direct": "https://…?g2bOpen=00289" },
  "catalog_ver": "w2d-20260919|jina-embeddings-v3-1024" }
```

## Protocol

- `POST /mcp` 단일 URL, 수동 JSON-RPC (SDK 불필요, stateless):
  - `initialize` → `protocolVersion` 에코 + `capabilities.tools` + `serverInfo`. 응답에 `MCP-Protocol-Version` 헤더 에코.
  - `notifications/initialized` → `202` 수락.
  - `tools/list` → 2툴 정의.
  - `tools/call` → `{ content: [{ type: "text", text: "<위 JSON>" }] }`.
- 에러: 빈 query·미존재 ID·미지 툴 → `-32602`; Jina/내부 실패 → `-32000` + 사용자용 문구. 스택 노출 없음.
- 인증 없음(public). `GET /mcp` → 405. trailing slash 무관 (`/mcp`·`/mcp/` 동일).

## catalog_ver 정책

`"w2d-20260919|jina-embeddings-v3-1024"` — 카탈로그+임베딩 모델 식별자. 바뀌면 클라이언트는 캐시를 버리고 재조회할 것(description 명시).

## 클라이언트 등록

```jsonc
// custom_chat: MCP_SERVERS='[{"name":"jodal","url":"http://127.0.0.1:8321/mcp/"}]'
// transport: STREAMABLE_HTTP. 툴명은 mcp_jodal_search_reports 형태로 prefix됨.
```

Inspector: `npx @modelcontextprotocol/inspector`, URL에 dev 주소 + `/mcp` 입력 후 `tools/list` → `search_reports` 호출.
