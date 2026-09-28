# custom_chat_webservice vs Onyx — 빠진 것, 문제, 이유

`custom_chat_webservice`는 Onyx agent loop의 골격만 포트한 경량판이다.
Loop 구조(`loop.py`), tool-call delta 조립(`llm_step.py`), 병렬 runner(`tools/runner.py`)는 가져왔다.
아래 항목은 가져오지 않았다. 각 항목마다 현재 상태, 문제, 이유를 적는다.

원본 위치는 `backend/onyx/` 기준이다. 경량판 위치는 `custom_chat_webservice/backend/app/` 기준이다.

---

## 1. 시스템 프롬프트 — ✅ 적용됨 (2026-09-20)

`backend/app/prompts.py`에 Onyx 텍스트 포트 + per-request 치환.
`main.py:_build_history`가 `build_system_prompt(base, should_cite_documents=req.web_search)` 호출.
`SYSTEM_PROMPT` env 비어있으면 Onyx default, 있으면 override + placeholder 치환.

**원래 상태 (적용 전):**

**Onyx 원본:**
`prompts/chat_prompts.py:13-28` `DEFAULT_SYSTEM_PROMPT`는 다음을 포함한다:

- 역할 정의: truthful, nuanced, insightful, efficient expert assistant.
- 모호하면 도구로 맥락 확보.
- 현재 날짜 주입 (`{{CURRENT_DATETIME}}`).
- 인용 지침 주입 (`{{CITATION_GUIDANCE}}`).
- Response Style: Markdown, LaTeX (`$$...$$`, `\(...\)`), 코드 언어 태그, 표, 구분선, 강조.

**없으면 문제:**

- 모델이 오늘 날짜를 모른다. "최신", "이번 주" 질문에 freshness 판단을 못 한다.
- 출력 형식이 매번 달라진다. 수식 깨짐, 코드 블록 언어 누락, 장문 가독성 저하가 생긴다.
- 모델 성향이 고정되지 않는다. 같은 질문에 답 깊이와 톤이 흔들린다.

**왜 해야 하는지:**

- 시스템 프롬프트는 모든 cycle에 들어가는 유일한 상수 지침이다.
- 날짜 한 줄이 도구 호출 판단(검색할지 말지)을 바꾼다.
- 스타일 지침은 UI 렌더링(마크다운, 수식)과 직결된다. 없으면 프론트 표시 품질이 떨어진다.

---

## 2. 동적 프롬프트 조립 — `build_system_prompt` 없음

**현재 상태:**
`main.py:51-70` `_build_history`는 고정 문자열 하나를 system 메시지로 넣는다.
요청별 조립이 없다.

> ✅ 적용됨 (2026-09-20): `TOOL_DESCRIPTION_SEARCH_GUIDANCE`, `WEB_SEARCH_GUIDANCE`를
> `prompts.py:build_tool_guidance`로 포트. `main.py:_run`이 실제 구성된 도구 기준
> `has_web_search`를 계산해 `_build_history`에 전달 — 키 없이 web_search가 떨어져도
> 거짓 지침이 안 들어간다. `TOOL_CALL_FAILURE_PROMPT`는 `tools/runner.py`에서
> unknown-tool 호출에 per-call failure response로 반환 (Onyx `create_tool_call_failure_messages` 패턴).

`chat/prompt_utils.py:250-342` `build_system_prompt`는 매 턴 다음을 조립한다:

- `apply_prompt_placeholders`: 날짜, 인용 지침 placeholder 치환. ✅
- company context (`get_company_context`): 조직명, 조직 설명. ⬜ 미적용 (남은 copy-paste 1개).
- user info 섹션: memory, 조직 프로필. ❌ scope-out (memory 도구 없음).
- `REQUIRE_CITATION_GUIDANCE`: 검색 참조 시 인라인 인용 강제. ✅
- `TOOL_SECTION_HEADER` + per-tool guidance: 있는 도구(web_search, open_url)만 선택 첨부. ✅
  scope-out 4개 guidance는 붙이지 않음 (없는 도구 지침은 환각 호출 유발).

**없으면 문제 (현 scope):**
- 회사·사용자 맥락이 매번 빠진다. "우리 팀", "우리 상품" 질문에 답이 일반론이 된다.
- 인용 강제 문구가 조건부로 안 붙는다. 검색을 해도 인용 없는 답이 나온다.
- 도구별 사용법 지침이 없다. 모델이 도구를 언제·어떻게 쓸지 추측한다. 아래 2.1–2.4가 그 결과다.

**왜 해야 하는지:**

- 고정 프롬프트는 도구 조합이 바뀔 때 거짓말을 한다. 없는 도구 지침은 환각 호출을 유발하고, 있는 도구 지침 누락은 호출 실패를 유발한다.
- 조립 함수는 "이번 요청에 필요한 지침만" 넣는다. 토큰 낭비 없이 정확도를 올리는 구조다.

### 2.1 `TOOL_DESCRIPTION_SEARCH_GUIDANCE` 없음

검색 판단 기준(아는 내용은 직접 답변, 오래된 지식·빠른 변화 주제는 검색, 모호하면 검색 우선, 같은 쿼리 반복 금지)이 없다.
결과: 불필요한 검색(비용·지연 증가) 또는 검색 생략(오답)이 랜덤하게 생긴다.

### 2.2 `WEB_SEARCH_GUIDANCE` 없음

Freshness/Accuracy/Niche 판단 기준과 `site:` 제한 안내가 없다.
결과: 최신 질문에 검색을 안 하거나, 쿼리 품질이 낮아 스니펫만으로 답한다.

### 2.3 `OPEN_URL_GUIDANCE` 없음 (도구 자체도 없음, 3절 참조)

"web_search 후 open_url로 원문 확인" 패턴이 없다.
결과: 스니펫 기반 답에 머문다. 스니펫에는 없는 세부 수치·조건 질문에 오답이 나온다.

### 2.4 `TOOL_CALL_FAILURE_PROMPT` 없음

도구명·인자 오타 시 LLM-facing 교정 메시지가 없다.
`loop.py:159-166`은 "Answer directly"로 넘어간다.
결과: 고칠 수 있는 호출 오타가 턴 전체 실패가 된다.

---

## 3. 도구 — ✅ 현 scope 완성 (2026-09-20)

필요 도구: web_search + open_url + MCP. 아래 4개는 scope-out (사용자 결정):
internal search, run_python, memory(add_memory), image gen. 관련 guidance도 붙이지 않음.
`backend/app/tools/open_url.py` 신설: crawl-only port. web_search와 같은
`{"results": [{document, title, url, content}]}` + `citation_info` 규약, runner
citation slot 공유, `GET /tools` 노출, 프론트 `open_url_*` 패킷 렌더.
`OPEN_URLS_GUIDANCE` + `OPEN_URL_REMINDER`(web_search 직후 nudge, `llm_loop.py:804` 패턴) 포함.
**원래 상태 (적용 전):**
`main.py` `_build_tools`는 `WebSearchTool` + MCP만 만들었다.

`tools/tool_implementations/` 아래 internal search, open_url, python(`run_python`), image generation, memory(`add_memory`)가 있다.
open_url만 포트. 나머지 4개 scope-out.

**해결됨:** open_url 추가로 스니펫 한계 보완. web_search → open_url → 인용 체인 완성.

**Scope-out (불필요):** internal_search, run_python, add_memory, generate_image.
관련 guidance(`INTERNAL_SEARCH_GUIDANCE`, `PYTHON_TOOL_GUIDANCE`,
`GENERATE_IMAGE_GUIDANCE`, `MEMORY_GUIDANCE`)도 붙이지 않음. 없는 도구 지침은 환각 호출 유발.

---

## 4. 인프라 없음

### 4.1 Citation processor 없음 → ⚠️ 부분 적용 (2026-09-20)

Onyx `DynamicCitationProcessor`의 unknown-skip만 `llm_step.py:strip_unknown_citations`로
포트. loop 종료 시 `citation_docs` 키와 대조해 근거 없는 `[N]` 제거. 코드펜스 안·연도
(`[2024]`)는 유지. 스트림 델타는 손대지 않고 `LoopResult.answer` 확정 전에만 적용 —
스트리밍 텍스트와 final이 다를 수 있어 프론트가 final로 re-render.
남은 것: `[[N]](url)` 하이퍼링크 변환, 코드블록 내 오표시 제외(스트림 측), 병렬 매핑 검증.
**현재 상태:**
`tools/web_search.py:157-185`가 `citation_info` 패킷을 순서대로 직접 emit한다.
LLM 출력 속 `[N]`을 검증·가공하는 단계가 없다.

**Onyx 원본:**
`chat/citation_processor.py` `DynamicCitationProcessor`는 스트림 토큰에서 인용을 검출하고, 모드별(`REMOVE` / `KEEP_MARKERS` / `HYPERLINK`)로 처리하고, 첫 인용 순으로 문서를 추적하고, `CitationInfo`를 emit한다. 코드 블록 안 `[1]`은 인용이 아니라고 판단한다.

**없으면 문제:**

- 존재하지 않는 `[99]` 같은 환각 인용이 그대로 UI에 나간다.
- 코드 예제 속 `[1]`이 인용으로 오표시된다.
- 클릭 링크(`[[1]](url)`) 변환이 없다. 번호-문서 연결을 프론트가 수동으로 맞춰야 한다.
- 병렬 검색 시 번호-문서 매핑 충돌 검증이 없다. `runner.py:18`의 `CITATION_SLOT=100` 예약만 있고 후단 검증이 없다.

**왜 해야 하는지:**

- 인용은 신뢰 기능이다. 검증 없는 인용 표시는 오답보다 해롭다. 사용자는 거짓 근거를 진짜로 믿는다.

### 4.2 Token budget + Compression 없음

**현재 상태:**
입력 토큰 상한 검사, history 압축이 없다.
`config.py`의 `MAX_INPUT_TOKENS`, `INPUT_SAFETY_MARGIN`은 읽기만 하고 강제하지 않는다.

**Onyx 원본:**
`chat/compression.py`가 예산 초과 시 오래된 메시지를 요약하고 최근 메시지는 verbatim으로 유지한다 (`COMPRESSION_TRIGGER_RATIO`, `RECENT_MESSAGES_RATIO`).

**없으면 문제:**

- 긴 대화 + 도구 결과 누적 시 context 초과로 요청 자체가 400으로 실패한다.
- 잘리더라도 중간이 잘린다. tool-call JSON이 반토막 나면 loop가 깨진다.
- 매 턴 전체 history를 보내므로 토큰 비용이 턴 수에 비례해 증가한다.

**왜 해야 하는지:**

- 세션이 길어질수록 반드시 터지는 장애다. 출시 전이 아니라 출시 후에 터진다.
- 압축은 장애 대응이 아니라 비용 대응이다. 요약 한 번이 매 턴 전체 재전송보다 싸다.

### 4.3 Search receipts 없음 → ❌ scope-out

internal search 전용 (`maybe_append_search_receipt`는 internal lane 메타데이터).
internal search scope-out이므로 함께 제외.

### 4.4 Tracing 없음

**Onyx 원본:**
`tracing/` 아래 `ChatTraceMetadata`, `LLMFlow`, `llm_generation_span`으로 매 step의 모델·프롬프트·도구 호출을 기록한다.

**없으면 문제:**

- "왜 이 답이 나왔나"를 재현할 수 없다. 로그는 텍스트만 남고, 어떤 프롬프트·어떤 도구 결과로 답했는지 모른다.
- 프롬프트 변경 전후 비교가 감으로 된다.

**왜 해야 하는지:**

- Agent 디버깅은 프롬프트 diff가 전부다. trace 없이 프롬프트를 고치는 것은 눈 감고 조정하는 것과 같다.

### 4.5 Persona 없음

**Onyx 원본:**
`server/features/persona/`, `db/persona.py`의 persona가 system prompt, 도구 세트, datetime aware 여부를 턴마다 바꾼다.

**없으면 문제:**

- `SYSTEM_PROMPT` 환경변수 하나로 모든 용도를 처리한다. 용도 추가마다 배포가 필요하다.
- "인용 엄격 모드", "간결 모드" 같은 변형을 런타임에 못 바꾼다.

**왜 해야 하는지:**

- 지금은 단일 용도라 없어도 된다. 두 번째 용도가 생기는 순간 하드코딩 분기로 망가진다. 그때 붙인다.

### 4.6 File / image handling 없음

**Onyx 원본:**
`FILE_REMINDER`, `IMAGE_DROP_REMINDER`, `NON_VISION_IMAGE_MARKER`, python sandbox의 파일 입출력(`tool_prompts.py:54-67`)이 있다.

**없으면 문제:**

- 파일 업로드·이미지 입력이 오면 무시하거나 깨진다.
- 모델별 이미지 수 제한 초과 시 요청이 실패한다. Onyx는 초과분을 떼고 reminder를 붙이는데, 경량판은 그 로직이 없다.

**왜 해야 하는지:**

- 텍스트 전용이면 없어도 된다. 파일·이미지를 받는 시점에 loop보다 먼저 이 레이어가 필요하다. 없으면 멀티모달 요청 전체가 실패한다.

---

## 5. 세션 — tool-call history 미보존

**현재 상태:**
`main.py:112-117` 세션 저장소는 user/assistant 텍스트만 저장한다 (`_sessions`).
`main.py:58-66` `_build_history`는 최근 20턴 텍스트만 복원한다. tool_calls, tool 응답, citation 매핑은 버린다.

**없으면 문제:**

- 후속 질문이 깨진다. "2번 문서 자세히", "그 표 다시 계산해줘" 같은 지시가 이전 도구 결과를 못 찾는다.
- 매 요청마다 같은 검색을 반복한다. 비용·지연이 턴 수만큼 증가한다.
- 턴마다 citation 번호가 1부터 다시 매겨진다. 이전 턴 `[3]`과 이번 턴 `[3]`이 다른 문서가 된다.

**왜 해야 하는지:**

- 텍스트만 이어붙인 세션은 대화처럼 보이지만 기억이 없다.
- 최소 fix는 세션에 assistant tool_calls + tool 응답을 함께 저장하고, citation 번호를 세션 단위로 이어가는 것이다. DB 없이 인메모리 구조 확장으로 해결된다.

---

## 남은 작업 (scope-in only)

| 순위 | 항목 | 상태 |
|------|------|------|
| 1 | 2. company context (`COMPANY_NAME/DESCRIPTION`) | copy-paste 1개. env 2개 + 조립 10줄 |
| 2 | 5. 세션 tool history 보존 | 설계 필요. citation 번호 정책 + pair-safe 절단 |
| 3 | 4.2 token budget + compression | 토크나이저 + 요약 LLM 필요. 출시 후 장애 대비 |
| 4 | 4.4 tracing | 프롬프트 손대기 전 기반 |

완료: 1, 2.1–2.4, 3(open_url), 4.1(부분). Scope-out: internal/python/memory/image + receipts.
