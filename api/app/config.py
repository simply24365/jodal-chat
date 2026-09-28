"""Env-driven config. No DB: everything comes from process env."""

import json
import os


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
# 체인 순서 override: LLM_CHAIN="xkiro,agnes,groq,gemini" (부분집합·순서 변경 가능)
LLM_CHAIN = os.environ.get("LLM_CHAIN", "xkiro,agnes,groq,gemini")
# 1순위: xkiro (OpenAI 호환, https://api.xkiro.com/v1). Cloudflare 1010에 걸리므로
# SDK 기본 UA는 통과하지만 raw urllib은 브라우저 UA가 필요 — SDK 경유는 문제없음.
XKIRO_API_KEY = os.environ.get("XKIRO_API_KEY")
XKIRO_MODEL = os.environ.get("XKIRO_MODEL", "qwen/qwen3.8-max:free")
AGNES_API_KEY = os.environ.get("AGNES_API_KEY")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or (
    os.environ.get("LLM_API_KEY") if os.environ.get("LLM_PROVIDER") == "groq" else None
)
# raw env 기준: 미설정 시 동작하는 모델로 폴백 (폐기된 llama-3.3-70b-versatile 회피)
GROQ_MODEL = os.environ.get("GROQ_MODEL") or (
    os.environ.get("LLM_MODEL") if os.environ.get("LLM_PROVIDER") == "groq" else None
) or "openai/gpt-oss-120b"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE") or 0.7)
MAX_INPUT_TOKENS = _int("MAX_INPUT_TOKENS", 128_000)
# Fraction of the context window reserved as safety margin (mirrors Onyx).
INPUT_SAFETY_MARGIN = float(os.environ.get("INPUT_SAFETY_MARGIN") or 0.1)

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
# "STREAMABLE_HTTP"|"SSE", "headers": {...}}. Empty = no MCP tools.
def _mcp_servers() -> list[dict]:
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if not raw:
        # 기본값: 이 앱이 직접 서빙하는 조달데이터허브 MCP 서버(/mcp).
        # mcp/ Cloudflare Worker(jodal.simply24365.workers.dev)를 통합하면서
        # 자기 자신을 클라이언트로 부른다 — 계층 분리를 유지하고 MCP 서버를
        # curl/다른 클라이언트로 독립 검증할 수 있게 하려는 선택.
        # 원격 Worker 를 쓰려면 MCP_SERVERS 로 url 을 덮어쓴다.
        return [
            {
                "name": "jodal",
                "url": f"http://127.0.0.1:{os.environ.get('PORT', '8078')}/mcp/",
                "transport": "STREAMABLE_HTTP",
            }
        ]
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

# In-memory session history cap (session_id -> messages). v0 only.
MAX_SESSIONS = _int("MAX_SESSIONS", 200)
