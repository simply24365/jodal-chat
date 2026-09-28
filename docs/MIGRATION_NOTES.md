# jodal-chat 이관 노트 (mcp/ Worker → api/ 통합)

2026-09-28. `mcp/`(Cloudflare Worker 1,870줄)를 폐기하고 `api/`(FastAPI)로 통합하면서
반드시 이식해야 했던 미커밋 수정 2건을 기록한다.

## 1. `hubpick.js` value_lookup — "값 없음"을 에러로 던지지 말 것 [★반드시 이식]

`mcp/`가 git rm 되기 직전의 미커밋 변경. `api/app/tools/hubpick.py`의
`hubValueLookup`에 그대로 옮겨야 한다.

변경 전: 스캔 결과 0건이면 `e.code = -32602` 를 raise
변경 후: 200 + 빈 결과 + 다음 3단계 대체 경로 가이드

```javascript
// "값 없음"은 정상 업무 결과다. -32602로 throw 하면 MCP 클라이언트가
// Tool execution failed 로 닫아버려 모델이 아래 guide 를 읽고 다음 툴로
// 진행하지 못하고 포기한다. report_id 지정 모드와 동일하게 200 + 빈 결과로 돌려준다.
return { mode: "scan_forms", query: args.query,
  matched_reports: 0, returned: 0, reports: [],
  note: `131건 보고서의 입력폼 정적 옵션 어디에도 "${q}" 가 없다. `
    + `기관명·업체명은 보고서 이름에도 없고, 조건 선택값(정적 옵션)에 있을 때만 여기서 잡힌다. `
    + `팝업이 자유입력인 조건이라 애초에 목록에 안 뜨는 경우다. `
    + `대체 경로: (1) 더 짧은 형태로 재검색 (예: "한국전력기술" → "한국전력"). `
    + `(2) search_reports 로 보고서명부터 찾은 뒤 form_guide(include_options=true)로 그 조건을 고를 수 있는지 확인. `
    + `(3) 그래도 안 되면 이 기관/업체를 집계하는 보고서가 허브에 없는 것.`,
  hubpick_ver: HUBPICK_VERSION };
```

commits: `be0c7f4 feat(mcp): value_lookup 에 131건 전수 스캔 추가` 가 이 로직의 선행분.

## 2. `worker.js` LLM 체인에 xkiro 우선 추가 — [이식 불필요]

Worker의 독립 `/api/chat` 경로(`handleChatC`, C_PROMPT)에 `callXkiro()` 를 추가하고
폴백 체인을 `agnes → groq → gemini` 에서 `xkiro → agnes → groq → gemini` 로 넓힌 것.

이건 **Python에 이미 반영되어 있다**:

- `api/app/config.py` → `LLM_CHAIN = os.environ.get("LLM_CHAIN", "xkiro,agnes,groq,gemini")`
- `api/.env.example` / `api/run.sh` 에 `XKIRO_API_KEY` 셸 passthrough 주석 존재
- README LLM 체인 설명도 `xkiro → agnes → groq → gemini`

Worker에만 남아 있던 미반영분이므로 이식 대상이 아니다. Worker를 지우면서 함께 사라진다.

## 3. 함께 사라지는 것 (이식 불필요)

- `PROXY = "https://g2bgo.simply24365.workers.dev/"` — `worker.js:496` 딥링크 1줄
  (`?g2bOpen=<rid>`). 데이터가 아니라 링크 생성기라 `api/`에서 f-string 1줄로 대체.
- `jodal.simply24365.workers.dev/mcp/` 공개 URL — 사용자가 폐기 승인.
- `knowledge/api-index/*.json` (2.3M) — Worker 전용 익스포트 아티팩트
  (`tools/export_worker_index.py` 산출). Python은 `data/index/bm25`(bm25s)를
  직접 로드하므로 애초에 필요 없고, **이중 언어 parity 검증하던 export 단계 자체가 사라진다.**
  토크나이저도 Worker의 "kiwi 근사" 정규식에서 진짜 `kiwipiepy`로 교체.
- `mcp/public/api-index/` 상위 디렉터리 구조 (hubpick 6파일만 `api/data/hubpick/`로 이관)
