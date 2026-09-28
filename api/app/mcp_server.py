"""MCP 서버 — 조달데이터허브 툴 11개를 /mcp 로 직접 서빙.

mcp/ Cloudflare Worker(worker.js 1,521줄 + hubpick.js 349줄)의 대체품.
같은 11개 툴을 같은 이름·같은 inputSchema 로 노출하므로, agent 쪽은
MCP 클라이언트 설정만 바꿀면 되고 툴 사용 프롬프트는 그대로다.

    search_reports / get_report_detail / resolve_items / item_children
    item_detail / item_products            ← app/tools/reports.py + search.py
    form_guide / concept_reports / value_lookup / family_map / catalog_health
                                           ← app/tools/hubpick.py

전통 MCP 방식으로 서빙한다(streamable HTTP). Agent 는 이 앱을 MCP **클라이언트**로
호출하므로(mcp_client.py → MCP_SERVERS), 서버/클라이언트 경계가 실제로 성립한다.
자기 자신을 HTTP 로 부르는 라운드트립이 있지만 (1) 계층 분리가 유지되고
(2) MCP 서버가 curl/다른 클라이언트로 독립 검증 가능하다는 값이 있다.

에러 규격은 mcp/ 와 동일하게 유지한다:
    -32602 입력오류(ValueError 계열) / -32000 내부(GoodsError 등)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool as MCPTool

from .tools import hubpick as H
from .tools import reports as R
from .tools import search as S
from .utils import setup_logger

logger = setup_logger("custom_chat.mcp_server")

SERVER_NAME = "jodal-stats"
SERVER_VERSION = "0.1.0"

INPUT_ERROR = -32602
INTERNAL_ERROR = -32000

_MAX_TOP_K = 30

# (name, description, inputSchema, callable)
_SPECS: tuple[tuple[str, str, dict[str, Any], Any], ...] = (
    (
        "search_reports",
        S.SEARCH_REPORTS_DESC,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "원하는 통계 1문장"},
                "top_k": {
                    "type": "integer",
                    "description": f"후보 수 (default 10, max {_MAX_TOP_K})",
                    "default": 10,
                },
                "vector_weight": {
                    "type": "number",
                    "description": (
                        "vector 가중 w 0~1 (default 0.7). "
                        "score=(1-w)/(K+rb)+w/(K+rv). 0 이면 BM25 단독."
                    ),
                    "default": 0.7,
                },
            },
            "required": ["query"],
        },
        S.search_reports,
    ),
    (
        "get_report_detail",
        S.GET_REPORT_DETAIL_DESC,
        {
            "type": "object",
            "properties": {
                "report_id": {"type": "string", "description": "후보 report_id (예: 00010)"},
                "report_name": {
                    "type": "string",
                    "description": "보고서명 전체 또는 일부 (예: 수요기관별 계약납품요구 실적통계)",
                },
            },
        },
        S.get_report_detail,
    ),
    (
        "resolve_items",
        R.RESOLVE_ITEMS_DESC,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "물품 표현 그대로 (예: 의자, 레미콘)"},
                "level": {
                    "type": "string",
                    "description": "품명 | 세부품명 | all (default all)",
                    "default": "all",
                },
                "top_k": {
                    "type": "integer",
                    "description": f"후보 수 (default 10, max {_MAX_TOP_K})",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        R.resolve_items,
    ),
    (
        "item_children",
        R.ITEM_CHILDREN_DESC,
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "2·4·6·8자리 prefix (예: 43, 4321, 432115, 43211501)",
                },
            },
            "required": ["code"],
        },
        R.item_children,
    ),
    (
        "item_detail",
        R.ITEM_DETAIL_DESC,
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "8자리 품명 또는 10자리 세부품명 번호"},
            },
            "required": ["code"],
        },
        R.item_detail,
    ),
    (
        "item_products",
        R.ITEM_PRODUCTS_DESC,
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "10자리 세부품명 번호 (예: 3011150501)",
                },
            },
            "required": ["code"],
        },
        R.item_products,
    ),
    (
        "form_guide",
        H.FORM_GUIDE_DESC,
        {
            "type": "object",
            "properties": {
                "report_id": {"type": "string", "description": "보고서 ID (예: 00262)"},
                "report_name": {"type": "string", "description": "보고서명 전체 또는 일부"},
                "include_options": {
                    "type": "boolean",
                    "description": (
                        "선택지 값까지 받을지 (기본 false). 사용자가 고를 수 있는 값을 물었을 때만 true."
                    ),
                },
            },
        },
        H.form_guide,
    ),
    (
        "concept_reports",
        H.CONCEPT_REPORTS_DESC,
        {
            "type": "object",
            "properties": {
                "concept_id": {"type": "string", "description": "개념 ID (예: female_biz)"},
            },
            "required": ["concept_id"],
        },
        H.concept_reports,
    ),
    (
        "value_lookup",
        H.VALUE_LOOKUP_DESC,
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "찾을 값 (기관명·업체명 등). 공백 제거 후 비교.",
                },
                "report_id": {
                    "type": "string",
                    "description": "지정 시 (2) 모드. 생략하면 (1) 131건 전수 스캔.",
                },
                "cond": {
                    "type": "string",
                    "description": "조건명 (report_id 지정 시 필수). form_guide 의 name 을 쓴다.",
                },
                "intent": {
                    "type": "string",
                    "description": (
                        "질문의 나머지 부분. 기관 1개가 60~70건에 걸릴 때 정답을 가르는 신호."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": f"후보 수 (default 10, max {_MAX_TOP_K})",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        H.value_lookup,
    ),
    (
        "family_map",
        H.FAMILY_MAP_DESC,
        {
            "type": "object",
            "properties": {
                "family": {"type": "string", "description": "family 이름 (예: 입찰공고)"},
            },
            "required": ["family"],
        },
        H.family_map,
    ),
    (
        "catalog_health",
        H.CATALOG_HEALTH_DESC,
        {"type": "object", "properties": {}},
        H.catalog_health,
    ),
)

_BY_NAME = {name: (desc, schema, fn) for name, desc, schema, fn in _SPECS}


def tool_names() -> list[str]:
    return [n for n, *_ in _SPECS]


# ---------- 서버 ----------

def build_server() -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[MCPTool]:
        return [
            MCPTool(name=name, description=desc, inputSchema=schema)
            for name, desc, schema, _ in _SPECS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        spec = _BY_NAME.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        _, _, fn = spec
        try:
            result = fn(**(arguments or {}))
        except (H.HubInputError, R.GoodsInputError) as e:
            # 입력오류. 사용자에게EDEACTABLE 한 줄 가이드가 낫다.
            raise ValueError(str(e)) from e
        except R.GoodsError as e:
            raise RuntimeError(str(e)) from e
        return [
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, default=str),
            )
        ]

    return server


_manager: StreamableHTTPSessionManager | None = None


def get_manager() -> StreamableHTTPSessionManager:
    """ASGI 앱(manager.handle_request)을 반환한다. 진입은 lifespan(startup) 에서.

    StreamableHTTPSessionManager.run() 은 진입해야 세션 매니저가 살아 있으므로,
    앱 마운트와 수명 주기를 분리할 수 없다. mount 는 handle_request 를 받고
    실제 진입은 lifespan 이 담당한다.
    """
    global _manager
    if _manager is None:
        _manager = StreamableHTTPSessionManager(
            app=build_server(),
            event_store=None,
            json_response=True,
            stateless=True,
        )
    return _manager


def mcp_asgi_app() -> Any:
    """FastAPI app.mount() 에 넘길 ASGI callable."""
    return get_manager().handle_request


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """MCP 세션 매니저 진입 + 인덱스 선로드."""
    async with get_manager().run():
        await warmup()
        yield


async def warmup() -> None:
    """BM25 인덱스(1.4s) + 카탈로그 + hubpick 에셋 선로드 + 자기 MCP 연결 확인.

    첫 사용자에게 1.4초를 지불시키지 않기 위해 기동 시 미리 올린다.
    실패해도 치명적이지 않다 — 툴 호출 시 lazy 로 다시 시도한다.

    ★ 자기 자신을 MCP 클라이언트로 부르므로, config.MCP_SERVERS 의 URL 이 실제
    바인딩 포트와 어긋나면 조용히 조달 툴 11개가 사라진다(가장 흔은 사고:
    `--port 9000` 으로 띄웠는데 PORT env 를 안 줌). 원래 코드는 discover 실패를
    WARNING 으로만 남기고 넘어가서 "에이전트가 조달을 못 아는" 형태로 드러난다.
    그래서 기동 후 한 번 물어보고, 안 맞으면 이유를 Prints 한다.

    주의: 이 검사는 **반드시 서버가 요청을 받을 수 있는 뒤에** 실행해야 한다.
    lifespan 안에서 동기 discover 를 부르면 이벤트 루프가 막혀 자기 요청이
    처리되지 않는다(실측: 기동 직후 항상 실패로 떴다). 그래서 백그라운드
    태스크로 미룬다.
    """
    from .retrieval import catalog, search_hybrid

    for label, fn in (
        ("bm25", search_hybrid._lazy),  # noqa: SLF001
        ("catalog", catalog.catalog),
        ("hubpick", H.load_hub),
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup %s failed (lazy retry on first use): %s", label, e)

    _tasks.add(asyncio.create_task(_verify_self_mcp()))


# lifespan 이 닫힐 때까지 살아 있는 백그라운드 작업 참조 (GC 방지)
_tasks: set[asyncio.Task] = set()


async def _verify_self_mcp() -> None:
    """자기 /mcp 로 실제 discover 하여 포트 불일치 등을 조기에 드러낸다."""
    from . import config
    from .tools import mcp_client

    # uvicorn 이 소켓을 bind 하고 요청을 받기 시작할 때까지 양보.
    await asyncio.sleep(1.0)

    for s in config.MCP_SERVERS:
        url = s.get("url", "")
        if not url:
            continue
        try:
            found = await asyncio.to_thread(
                mcp_client.discover_mcp_tools,
                url,
                s.get("headers"),
                s.get("transport", "STREAMABLE_HTTP"),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "MCP 자기연결 실패 %s (%s): %s — 조달 툴이 사라진 채 챗이 도둑질된다. "
                "서버를 띄운 포트와 config.MCP_SERVERS 의 url 이 같은지 확인 "
                "(--port 로 바꾸었으면 PORT env 도 같이).",
                url,
                s.get("name"),
                e,
            )
            continue
        names = sorted(t.name for t in found)
        if len(names) != len(_SPECS):
            logger.error(
                "MCP 자기연결 경고 %s: 툴 %d/%d 개만 노출됨. 누락=%s",
                url,
                len(names),
                len(_SPECS),
                sorted(set(n for n, *_ in _SPECS) - set(names)),
            )
        else:
            logger.info("MCP self-check OK %s (%d tools)", url, len(names))
