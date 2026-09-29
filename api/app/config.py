"""Env-driven config. No DB: everything comes from process env."""

import json
import os
import re
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """api/.env 자체 로딩 (표준 라이브러리만). 크로스플랫폼 필수 동작.

    Linux(make dev — bash sourcing)든 Windows(uvicorn 직접 실행)든 동일하게
    키가 주입되도록 한다. 셸 passthrough가 원칙이지만, sourcing 없이 서버를
    띄우는 경로에서 키가 조용히 빠지는 사고를 막는 안전장치.
    우선순위: 이미 설정된 환경변수(셸) > .env. BOM·CRLF·export 접두어·따옴표 허용.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if val:
            os.environ.setdefault(key, val)


_load_env_file(Path(__file__).resolve().parent.parent / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# LLM (xkiro/agnes=OpenAI SDK 직결, groq/gemini=litellm). 키는 env only, 파일 기록 금지.
# AGNES_API_KEY는 apihub 키(51자, sk-...) — shim EDGE_SECRET(43자)과 다름. 혼동 금지.
# XKIRO_API_KEY는 sk-xt-... (54자) — 셸 passthrough(.env 아님), 또 다른 키.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY")
LLM_API_BASE = os.environ.get("LLM_API_BASE")
# 체인 순서 override: LLM_CHAIN="xkiro,agnes" (부분집합·순서 변경 가능)
# 기본 체인은 xkiro 메인 + agnes 폴백. groq·gemini 는 폐기.
LLM_CHAIN = os.environ.get("LLM_CHAIN", "xkiro,agnes")
# 1순위: xkiro (OpenAI 호환, https://api.xkiro.com/v1). Cloudflare 1010에 걸리므로
# SDK 기본 UA는 통과하지만 raw urllib은 브라우저 UA가 필요 — SDK 경유는 문제없음.
XKIRO_API_KEY = os.environ.get("XKIRO_API_KEY")
XKIRO_MODEL = os.environ.get("XKIRO_MODEL", "meta/muse-spark-1.3-contributor:free")
# 폴백: agnes (OpenAI 호환, apihub)
AGNES_API_KEY = os.environ.get("AGNES_API_KEY")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
# 폐기됨: groq·gemini 는 체인에서 제외. 키가 남아 있어도 기본 체인은
# xkiro,agnes 만 사용한다 (LLM_CHAIN 으로 되돌릴 수 있다).
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE") or 0.7)

# Loop
MAX_LLM_CYCLES = _int("MAX_LLM_CYCLES", 6)
MAX_CONCURRENT_TOOLS: int | None = (
    int(v) if (v := os.environ.get("MAX_CONCURRENT_TOOLS")) else None
)

# Empty = use prompts.DEFAULT_SYSTEM_PROMPT (Onyx port). Set SYSTEM_PROMPT
# to override. Placeholders {{CURRENT_DATETIME}}, {{CITATION_GUIDANCE}},
# {{REMINDER_TAG_DESCRIPTION}} are substituted per request.
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "")

# Web search: "jina" (JINA_API_KEY) or "tavily" (TAVILY_API_KEY).
WEB_SEARCH_PROVIDER = os.environ.get("WEB_SEARCH_PROVIDER", "jina")
JINA_API_KEY = os.environ.get("JINA_API_KEY")
JINA_SEARCH_URL = os.environ.get("JINA_SEARCH_URL", "https://s.jina.ai/")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

# MCP servers: JSON list of {"name": ..., "url": ..., "transport":
# "STREAMABLE_HTTP"|"SSE", "headers": {...}}. Empty = the in-process jodal
# server via the SDK in-memory transport. A server with "local": true is
# served by this process (mcp_server.build_server) without HTTP.
def _mcp_servers() -> list[dict]:
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if not raw:
        # 기본값: 이 앱이 직접 서빙하는 조달데이터허브 MCP 서버.
        # HTTP 루프백(127.0.0.1:$PORT/mcp/) 대신 SDK 표준 인메모리 트랜스포트로
        # 같은 프로세스의 서버 객체를 직결한다 — 포트 불일치·lifespan 데드락·
        # 스레드/이벤트루프/TCP 비용이 모두 사라진다. /mcp 마운트는 curl 등
        # 외부 클라이언트의 독립 검증용으로 그대로 유지된다.
        return [{"name": "jodal", "local": True}]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # 조용히 [] 로 떨어뜨리지 않는다. 실제로 이 실패가 챗을 조용히 망가뜨렸다:
        # run.sh 의 `set -a; . ./.env` sourcing 은 bash quote 제거 때문에
        # [{"name":"jodal",...}] 를 [{name:jodal,...}] 로 만들었고, 그 결과
        # 조달 툴 11개가 하나도 등록되지 않은 채 로그에도 아무것도 안 남았다.
        # 작은따옴표로 감싼 값을 .env 에 써야 한다.
        raise RuntimeError(
            f"MCP_SERVERS JSON 파싱 실패 ({e}). "
            f"값: {raw[:160]!r} — .env 에서 작은따옴표로 감싸야 한다: "
            """MCP_SERVERS='[{"name":"jodal","url":"http://127.0.0.1:8078/mcp/","transport":"STREAMABLE_HTTP"}]'"""
        ) from e
    if not isinstance(parsed, list):
        raise RuntimeError(f"MCP_SERVERS 는 JSON 배열이어야 한다. Got {type(parsed).__name__}")
    return parsed


MCP_SERVERS: list[dict] = _mcp_servers()

# open_url: live-fetch pages (stdlib HTML parser, no extra deps).
# Mirrors Onyx MAX_CHARS_ACROSS_URLS = 10 * MAX_CHARS_PER_URL (15000).
OPEN_URL_TIMEOUT_SECONDS = _int("OPEN_URL_TIMEOUT_SECONDS", 30)
OPEN_URL_MAX_URLS = _int("OPEN_URL_MAX_URLS", 5)
OPEN_URL_MAX_CHARS_PER_PAGE = _int("OPEN_URL_MAX_CHARS_PER_PAGE", 15000)
OPEN_URL_MAX_CHARS_TOTAL = _int("OPEN_URL_MAX_CHARS_TOTAL", 150000)

# MCP 툴별 호출 정책 (tool_name -> max_calls_per_turn). 툴의 도메인 정책은
# 루프 코어가 아니라 설정 계층이 소유한다. 없는 툴은 무제한.
# eval 근거(runs/head.ndjson 20턴 집계): search_reports 가 턴당 최대 3회까지
# 반복 호출됐고 3회째부터는 후보가 거의 변하지 않았다 — 2회 상한으로 절약하고
# 초과분은 exhausted_reminder 로 "가진 후보로 답하라"고 유도한다.
# value_lookup/get_report_detail 은 서로 다른 후보를 파는 정당한 재호출이라 무제한 유지.
MCP_TOOL_MAX_CALLS = {"resolve_items": 2, "search_reports": 2}

# In-memory session history cap (session_id -> messages). v0 only.
MAX_SESSIONS = _int("MAX_SESSIONS", 200)
