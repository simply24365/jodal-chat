# 툴 레이어(`api/app/tools/`) 억척스러운 구현 분석 및 개선안

작성일: 2026-09-29 · 대상: `api/app/tools/` 전체 + `api/app/mcp_server.py` 연결부
경계: `retrieval/`, `loop.py`, `llm*.py`, `main.py` 코어는 제외 (mcp_client ↔ mcp_server 연결부만 포함)

---

## 0. 결론 요약

**전체 신뢰도: 높음 (1차 코드 전수 열람 + mcp SDK 공식 소스 교차 검증)**

툴 레이어는 대부분 Onyx 포트로서 합리적이지만, 두 가지 구조적 문제가 뚜렷하다.

1. **자기 자신의 MCP 서버를 HTTP 루프백으로 호출하는 구조** — 툴 호출 1회마다 새 스레드 + 새 이벤트 루프 + TCP 연결 + MCP initialize 핸드셰이크 + HTTP POST를 자기 자신에게 보낸다. 채팅 요청 1회마다 11개 툴 discover도 매번 반복된다. 이 구조의 취약성(포트 불일치, lifespan 데드락) 때문에 방어코드 3종(`_verify_self_mcp`, `sleep(1.0)`, config 에러 강화)이 이미 별도로 존재한다. **mcp SDK 1.28.1(설치 버전)에 인메모리 트랜스포트 `create_connected_server_and_client_session`이 실재하므로 표준 방법으로 교체 가능하다.**
2. **11개 툴의 inputSchema를 손으로 dict 리터럴로 작성** — 함수 시그니처(`tools/*.py`)와 평행 유지보수되며, 이미 드리프트가 발생했다(`item_products`의 `code10` 파라미터가 스키마에 없음). FastMCP `@tool` 데코레이터로 타입 시그니처에서 자동 생성하는 것이 SDK 1.28.1에 포함된 표준 기능이다.

그 외: 툴 실행 타임아웃 상수가 죽어 있고, web_search/open_url에 재시도가 없으며(tenacity는 이미 의존성에 있음), runner가 인용 슬롯을 `isinstance` 하드코딩으로 배분한다.

---

## 1. 항목별 발견

### 1-1. 자기 MCP HTTP 루프백 — 가장 큰 후보 (영향도: High)

**현재 구현 (증거 체인):**

| 위치 | 내용 |
|---|---|
| `config.py:73` | 기본 MCP 서버 URL = `http://127.0.0.1:{PORT}/mcp/` (자기 자신) |
| `main.py:37` | `app.mount("/mcp", mcp_asgi_app())` — FastAPI 안에 MCP 서버 탑재 |
| `main.py:86-90` | **채팅 요청마다** `build_mcp_tools()` → 11툴 HTTP discover 반복 |
| `tools/mcp_client.py:129-145` | 툴 호출 1회마다 `ThreadPoolExecutor(max_workers=1)` 신규 생성 → `asyncio.run` 신규 이벤트 루프 → `streamablehttp_client` 신규 TCP 연결 → `session.initialize()` 핸드셰이크 → 자기 서버로 HTTP POST |
| `mcp_server.py:15-18` | 설계 의도 스스로 문서화: "자기 자신을 HTTP 로 부르는 라운드트립이 있지만 (1) 계층 분리 유지 (2) curl/독립 검증 가능" |
| `mcp_server.py:351-` | `_verify_self_mcp()` — 루프백 실패를 감지하는 방어코드. "가장 흔한 사고: `--port 9000` 으로 띄웠는데 PORT env 를 안 줌 → 조용히 조달 툴 11개가 사라진다" |
| `mcp_server.py` warmup 주석 | "lifespan 안에서 동기 discover 를 부르면 이벤트 루프가 막혀 자기 요청이 처리되지 않는다(실측: 기동 직후 항상 실패)" → `await asyncio.sleep(1.0)` 으로 uvicorn 바인드를 **추측** |

**왜 억척스러운가:**
- 호출 비용이 불필요하게 크다: 인프로세스 함수 호출(`search_reports(...)`) 한 번이 **스레드 생성 → 이벤트 루프 생성 → TCP → JSON-RPC initialize → HTTP POST → ASGI → 응답 역직렬화**의 7단 계층을 통과한다. 같은 프로세스 안의 함수다.
- 구조적 취약성의 자인: 포트 불일치 사고, lifespan 데드락, sleep 기반 대기 — 이 세 방어코드가 존재한다는 것 자체가 루프백이 만든 문제를 루프백 주변에서 수습하고 있다는 뜻이다.
- `MCP_SERVERS` JSON 파싱 실패 조용 무시 사고(config.py:80-89 주석의 실측 사례)도 같은 구조에서 비롯됐다.

**개선안 (표준 조건 부합):**
mcp SDK 1.28.1(프로젝트 설치 버전, `pyproject.toml`: `mcp==1.28.1`)의 공식 인메모리 트랜스포트 사용:

```python
from mcp.shared.memory import create_connected_server_and_client_session

async with create_connected_server_and_client_session(server) as session:
    tools = await session.list_tools()      # discover — HTTP 없음
    result = await session.call_tool(name, args)  # 호출 — HTTP 없음
```

- 검증: `modelcontextprotocol/python-sdk` v1.28.1 태그의 `src/mcp/shared/memory.py`에 `create_connected_server_and_client_session(server: Server | FastMCP, ...)` 이 실재함을 확인 (2026-09-29 열람, https://github.com/modelcontextprotocol/python-sdk/blob/v1.28.1/src/mcp/shared/memory.py). 서버 인스턴스를 직접 받아 anyio 메모리 스트림으로 연결한다 — 정확히 "server를 직접 임포트해 in-process로 호출"하는 표준 경로.
- 적용: `mcp_server.build_server()`가 이미 `Server` 인스턴스를 반환하므로(`mcp_server.py:248`) 이 객체를 `mcp_client`에 넘기면 된다. `MCPTool.run`은 `call_mcp_tool()` 대신 인메모리 세션 호출로 교체.
- **계층 분리·독립 검증이라는 원래 가치는 보존된다**: `/mcp` HTTP 마운트는 그대로 유지(curl/외부 클라이언트 검증 가능). 바뀌는 것은 agent 내부 호출 경로만. 원격 Worker(`config.py` 주석의 `jodal.simply24365.workers.dev`)로 되돌릴 때도 `MCPServerConfig.url`이 있으면 기존 HTTP 경로를 쓰는 하이브리드가 가능하다.
- 파생 제거 효과: `_verify_self_mcp`(포트 불일치 검사), `sleep(1.0)` 대기, `MCP_SERVERS` 루프백 URL 기본값 전부 불필요해진다. `asyncio.run`-in-thread 패턴(`mcp_client.py:44-52`, `129-145`)도 자연 축소.
- 성능 견적(참고): 11툴 discover × 채팅 1회 + 툴 호출당 왕복이 프로세스 내 함수 호출로 대체되어, 요청당 수십 ms~수백 ms와 스레드/루프 생성 비용 절감. (정확한 수치는 미계산 — 계층 수로부터의 추론)

**주의(추가 확인 필요):** `create_connected_server_and_client_session`은 세션을 async context manager로만 제공하므로, 동기 `Tool.run` 인터페이스(`interface.py:30-37`)에서 쓰려면 장수명 세션 1개를 백그라운드 이벤트 루프에 올려두고 `anyio.from_thread.run`으로 호출하는 브리지가 필요하다. 이는 현재의 "호출마다 `asyncio.run`"보다도 단순해진다(루프 1회 생성, 세션 1회 initialize). `Tool.run` 전체를 async로 바꾸는 안은 `loop.py`/`main.py` 코어 침범이므로 경계 밖으로 표기만 둔다.

---

### 1-2. inputSchema 손작성 + 함수 시그니처 평행 유지보수 (영향도: High)

**현재 구현:**
- `mcp_server.py:50-238`: `_SPECS` 튜플에 11개 툴의 (name, description, inputSchema, callable)을 **손으로 작성한 dict 리터럴**로 정의. 스키마가 두 곳에 복사돼 있는 것은 아니고 한 곳(`_SPECS`)에만 있으나, **실행 함수의 실제 시그니처(`tools/search.py`, `reports.py`, `hubpick.py`)와 스키마가 서로 다른 파일에서 평행하게 유지**되어야 한다.

**드리프트 실례 (1차 증거):**
- `reports.py:301`: `def item_products(code: str = None, code10: str = None)` — `code10` 파라미터 수용
- `mcp_server.py` `item_products` 스키마: `properties`에 `code`만 있고 `code10`은 노출 안 됨 → 스키마가 함수를 따라가지 못한 상태. 클라이언트가 `code10`으로 호출할 방법이 MCP 표면에 없다.
- `_MAX_TOP_K = 30`이 `mcp_server.py:47`, `reports.py:30`, `search.py:27`, `hubpick.py`(`_clamp_top_k` cap=30)에 각각 하드코딩 — 설명 문자열(`"max 30"`)까지 합치면 4곳 평행.

**왜 억척스러운가:** 11개 툴 × 평균 3~4 파라미터의 이름·타입·설명·기본값·required를 두 체계(파이썬 시그니처 / JSON Schema dict)에 일관되게 유지하는 인적 작업. 이미 어긋난 곳이 있다.

**개선안 (표준 조건 부합):** SDK 1.28.1에 포함된 **FastMCP 데코레이터**(`mcp.server.fastmcp`)로 스키마 자동 생성:

```python
from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jodal-stats")

@mcp.tool()
def resolve_items(
    query: Annotated[str, Field(description="물품 표현 그대로 (예: 의자, 레미콘)")],
    level: str = "all",
    top_k: int = 10,
) -> dict:
    """물품 표현을 조달 물품분류 번호로 변환..."""
```

- 검증: SDK v1.x README "Tools" 섹션 — `@mcp.tool()` + 타입 힌트로 JSON Schema가 자동 생성됨 ("Notice what you did **not** write: no JSON Schema"). 2026-09-29 열람 (https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
- 기존 한국어 description(프롬프트 엔지니어링 산물)은 `Annotated[..., Field(description=...)]`로 이전하면 내용 손실 없이 유지된다.
- `get_report_detail`류의 "required 없음"도 `report_id: str | None = None` 시그니처로 동일 재현.
- 단, `Any` 타입 파라미터(`top_k: Any`)는 스키마가 빈약해지므로 `int`로 바꾸는 것이 오히려 정합성 향상 — 현재는 함수 내 `_clamp_top_k`가 방어하고 있다.
- 에러 코드 매핑(`-32602`/`-32000`, `mcp_server.py:44-45`)은 표준 예외 `mcp.shared.exceptions.McpError(ErrorData(code=...))` 사용이 SDK 정식 경로다. 현재의 `raise ValueError`가 lowlevel `Server.call_tool` 데코레이터에서 어떤 JSON-RPC 코드로 변환되는지는 미확인(추정: 내부 오류 취급) — **전환 시 검증 필요 항목**.

---

### 1-3. 툴 실행 타임아웃 부재 — 죽은 상수 (영향도: Medium-High)

**현재 구현:**
- `runner.py:18`: `TOOL_EXECUTION_TIMEOUT_SECONDS = 600` 정의 — **어디에서도 사용 안 함**(전수 grep 확인: 정의 1건만 hit).
- `runner.py:140-142`: `ThreadPoolExecutor.map`은 타임아웃 없음. 개별 툴의 내부 타임아웃(MCP `read_timeout=60s`, requests 30s, urlopen 20s)에만 의존. `hubpick`/`search` 등 순수 계산 툴과 향후 추가될 툴에는 상한이 없다.

**왜 억척스러운가:** 상수가 "타임아웃이 있다"는 착각을 주면서 실제로는 죽어 있다. Onyx 원본의 async `wait_for`가 포트되며 누락된 것으로 보인다(파일 헤더 "Port of Onyx run_tool_calls").

**개선안:** 동기 실행이므로 `concurrent.futures` 표준 관용구로:
```python
with ThreadPoolExecutor(...) as pool:
    futures = [pool.submit(_run_one, *p) for p in params]
    for f in futures:
        response, pkts = f.result(timeout=TOOL_EXECUTION_TIMEOUT_SECONDS)
```
(전체 상한을 두려면 deadline 계산 후 `timeout=deadline - now`.) 주의: 타임아웃 시 스레드는 취소 불가(Python 스레드의 한계)하므로, 완전한 해법은 1-1의 인메모리 전환 후 `asyncio.timeout` — 이 항목은 1-1에 종속된다.

---

### 1-4. runner의 `isinstance` 하드코딩 인용 슬롯 배분 (영향도: Low-Medium)

**현재 구현:** `runner.py:100-102`
```python
if isinstance(tool, (WebSearchTool, OpenURLTool)):
    override = {"starting_citation_num": start}
    start += CITATION_SLOT
```
runner가 `WebSearchTool`, `OpenURLTool` 구체 클래스를 import해서 알고 있다(`runner.py:14-16`).

**왜 억척스러운가:** Tool ABC(`interface.py`)의 존재 이유(구체 툴 모름)를 runner 스스로 깨고 있다. 인용을 쓰는 툴이 추가되면 runner를 고쳐야 한다.

**개선안:** `Tool` ABC에 `citation_capacity: int | None = None` 속성(또는 `emit_start`에 슬롯 위임)을 두고 runner는 속성만 읽게 변경. 병합 로직(`merge_tool_calls`, `runner.py:23-50`)은 Onyx 포트의 도메인 로직으로서 문제 없음 — 표준 라이브러리로 대체할 성질이 아니다.

---

### 1-5. mcp_client: 호출마다 새 스레드 + 새 이벤트 루프 (영향도: Medium — 1-1 해소 시 자동 축소)

**현재 구현:**
- `mcp_client.py:44-52`(`_run_sync`)와 `129-145`(`call_mcp_tool`): 호출마다 `ThreadPoolExecutor(1)` 생성 → `asyncio.run`(이벤트 루프 생성/파괴) → 연결 → 세션 initialize. 세션 재사용이 전혀 없다.
- `MCP_TOOL_CALL_TIMEOUT_SECONDS = 60`(27행)은 `read_timeout_seconds`로만 적용 — connect 타임아웃은 httpx 기본값에 맡김.

**왜 억척스러운가:** "sync 세계에서 async 호출"이라는 본질적 어려움을, 호출 비용을 매번 지불하는 방식으로 해결했다. Onyx 포트 출신이지만 원본은 프로세스가 아니라 이벤트 루프 상에서 동작한다.

**개선안:** 1-1의 인메모리 전환 후에는 백그라운드 루프 1개 + 장수명 세션 1개를 앱 수명(lifespan)에 두고 `anyio.from_thread.run(session.call_tool, ...)` 브리지. 원격 MCP 서버 지원을 유지해야 한다면 HTTP 경로도 같은 브리지로 통일(루프 재사용)만으로 이득이 있다. SSE/STREAMABLE_HTTP 전환 분기(`_transport`, 36-41행)는 SDK 표준 사용법이라 문제 없음.

---

### 1-6. web_search/open_url: 재시도 없음 + tenacity 미사용 (영향도: Medium)

**현재 구현:**
- `providers.py:49-64`(Jina), `78-92`(Tavily): `requests.post(..., timeout=30)` 1회, `raise_for_status` 후 즉시 실패. 재시도(backoff) 없음.
- `web_search.py:204-210`(`_safe_search`), `open_url.py:266-`(`_safe_fetch`): 모든 예외를 `str(e)`로 변환만. 429(레이트리밋)/일시적 5xx/ConnectionError가 구분 없이 "실패"로 처리된다.
- **`pyproject.toml`에 `tenacity==9.1.2`가 선언되어 있는데 `import tenacity`가 코드베이스 전체에 없다**(grep 확인) — 재시도 라이브러리를 설치만 해둔 상태.

**왜 억척스러운가:** 재시도는 이미 의존성에 있는 라이브러리의 존재 이유이고, 타임아웃·에러 매핑도 표준 패턴이 확립된 영역이다. 타임아웃 상수(30s)와 URL cap(`OPEN_URL_MAX_URLS=5`) 같은 나머지 처리는 Onyx 포트로서 정상 범위.

**개선안:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(reraise=True, stop=stop_after_attempt(2),
       wait=wait_exponential(multiplier=0.5),
       retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError, requests.HTTPError)))
def search(self, query): ...
```
에러 매핑은 `requests.HTTPError`에서 status code별(429 → "rate limited, retry later" 등) 한 줄 변환을 `_safe_search`에 추가. round-robin 병합(`web_search.py:129-149`)은 Onyx 포트 도메인 로직 — 유지.

---

### 1-7. providers.py Jina/Tavily 중복 (영향도: Low)

**현재 구현:** 두 클라이언트의 `search()` 본체가 구조적으로 ~80% 동일(POST → `raise_for_status` → `results` 파싱 → link/title/snippet 검증 → `[:num]` 캡). `web_search.build_provider()`의 `if/else` 분기(2개) 자체는 이 규모에선 억척이 아니다.

**개선안:** 공통 `_post_search(url, headers, payload) -> list[dict]` 헬퍼 하나로 파싱·검증 통합, 각 클라이언트는 요청 조립만 담당. 프로바이더 확장은 `WebSearchProvider` ABC(`providers.py:29-33`)가 이미 올바른 확장점이다.

---

### 1-8. open_url 인코딩 추론 취약 (영향도: Low)

**현재 구현:** `open_url.py:283-284`
```python
encoding = resp.encoding or "utf-8"
raw = body.decode(encoding, errors="replace")
```
HTTP 헤더에 charset이 없는 `text/*` 응답에서 requests는 `ISO-8859-1`을 기본 반환한다(RFC 관례) — 한국어 페이지에서 euc-kr/utf-8 문서가 깨질 수 있다.

**개선안:** `resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"` (charset_normalizer 기반, requests 내장). 수제 `_TextExtractor`(stdlib HTMLParser)는 "no extra deps" 의도가 docstring에 명시돼 있어 억척으로 보기 어렵다. SOTA 품질을 원한다면 trafilatura가 정석이지만, 의존성 추가와의 트레이드오프 — 우선순위는 낮게.

---

### 1-9. 정당화된 수제 구현 (억척이 아닌 것 — 열거하여 오해 방지)

| 위치 | 내용 | 왜 유지 OK |
|---|---|---|
| `mcp_tool.py:20-44` | `unwrap_exception`/`describe_error` — ExceptionGroup 언래핑 | httpx/anyio가 TaskGroup 예외를 감싸는 실제 문제에 대한 대응. 단, Python 3.13(`pyproject` `requires-python>=3.13`)이므로 호출부에서 `except*`로 잡는 3.11+ 표준 구문 병행 검토 가치는 있음 |
| `reports.py` 정규식 HTML 파싱 | `goods.g2b.go.kr` 비공식 HTML 테이블 파싱 | 업스트림이 문서화되지 않은 HTML이고 셀 인덱스 계약이 주석으로 명시돼 있음. BeautifulSoup(lxml) 이전은 로버스트성 개선책이지만 독립적 우선순위는 낮음 |
| `runner.py`의 `ThreadPoolExecutor` 병렬 | 도구가 전부 동기 `Tool.run` | 인터페이스가 동기인 한 표준적 선택. asyncio `TaskGroup` 대체는 `Tool.run` async화(= `loop.py`/`main.py` 전면 개편, 경계 밖)와 세트 |
| `mcp_server.py`의 `_SPECS` 단일 소스 | 스키마가 "두 곳에 복제"는 아님 | 문제는 복제가 아니라 함수 시그니처와의 평행 유지보수(1-2 참조) |

---

## 2. 개선 후보 Top 5 (우선순위)

| # | 개선안 | 대상 | 영향도 | 근거 |
|---|---|---|---|---|
| 1 | **자기 MCP 루프백 → 인메모리 세션**: `create_connected_server_and_client_session`(mcp 1.28.1 공식)으로 agent 내부 호출 교체. `/mcp` HTTP 마운트는 독립 검증용 유지, 원격 Worker fallback은 `MCPServerConfig` 유지 | `mcp_client.py`, `mcp_tool.py`, `mcp_server.py`, `config.py` | **High** | 방어코드 3종(`_verify_self_mcp`, `sleep(1.0)`, 루프백 기본 URL)이 증명하는 구조적 취약성 + 호출마다 7단 계층 비용. 설치된 SDK 버전에 표준 해법이 실재함을 공식 소스로 확인 |
| 2 | **inputSchema 손작성 → FastMCP `@tool` 자동 생성**: `Annotated[..., Field(description=...)]`로 한국어 설명 이전, `McpError(ErrorData)`로 에러 코드 표준화 | `mcp_server.py` `_SPECS`, `tools/*.py` | **High** | 이미 드리프트 발생(`item_products`의 `code10`), `_MAX_TOP_K=30` 4곳 중복. SDK 내장 기능으로 즉시 해결 |
| 3 | **툴 실행 타임아웃 부활**: 죽은 `TOOL_EXECUTION_TIMEOUT_SECONDS`(600s)를 `future.result(timeout=...)`로 실제 적용. 궁극적으로는 #1 완료 후 `asyncio.timeout` | `runner.py` | **Medium-High** | 상수가 정의만 되고 사용처 없음(전수 grep). 무응답 툴이 스레드를 영구 점유하는 유일한 방어선이 개별 HTTP 클라이언트 타임아웃뿐 |
| 4 | **web_search/open_url 재시도 + 에러 매핑**: 이미 설치된 tenacity(`pyproject.toml` 선언, 미사용)로 Timeout/ConnectionError/429에 1회 지수 backoff. `apparent_encoding`으로 open_url 한글 인코딩 방어 | `providers.py`, `web_search.py`, `open_url.py` | **Medium** | 의존성은 있고 구현은 없음. 무료 API의 일시적 429가 현재는 즉시 전체 쿼리 실패로 직결 |
| 5 | **runner 인용 슬롯 `isinstance` → Tool ABC 속성 위임**: `Tool`에 `citation_capacity` 속성 추가, runner는 구체 클래스 import 제거. (#1과 함께 `mcp_tool.py:179`의 `tool._name` private 직접 할당도 생성자 경유로 정리) | `interface.py`, `runner.py`, `mcp_tool.py` | **Low-Medium** | ABC 설계의존 방향 역전. 변경 비용이 낮고 이후 툴 추가 시 runner 불변 |

**종속 관계:** #3, #5는 #1과 독립 적용 가능. #1이 완료되면 #3은 `asyncio.timeout`으로, #5는 세션 브리지 설계와 함께 자연 정리된다.

---

## 3. 미확인 사항 (전환 시 검증 필요)

- `raise ValueError`가 lowlevel `Server.call_tool`(`mcp_server.py:259-260`)을 통과할 때 실제 JSON-RPC 오류 코드: `-32602`로 변환된다는 보장을 소스에서 확인 못 함. `MIGRATION_NOTES.md` §1이 언급하는 "MCP 클라이언트가 Tool execution failed 로 닫아버린다"는 현상과 연관 가능. FastMCP 전환 시 `McpError` 경로로 재검증 권장.
- 인메모리 세션의 장수명 운영 시 `Server` 인스턴스의 동시성 특성(여러 툴 호출이 동시 세션 사용) — SDK의 `ClientSession`은 순차 요청 가정이므로, 병렬 runner와 조합하려면 세션 락 또는 세션 풀 필요. 구현 시 확인.
- mcp SDK v2(2026-07-28 스펙 지원, `pip install mcp` 현재 기본)가 출시된 상태 — 상위 버전 이전 시 `Client("url")` 등 v2 API로 추가 단순화 여지 있음. 단 v1.x도 계속 유지보수됨(공식 README 확인). 본 보고서의 권고는 설치 버전인 1.28.1 기준.
