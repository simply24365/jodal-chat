"""MCP 서버 — 조달데이터허브 툴 11개를 /mcp 로 직접 서빙.

mcp/ Cloudflare Worker(worker.js 1,521줄 + hubpick.js 349줄)의 대체품.
같은 11개 툴을 같은 이름으로 노출한다.

    search_reports / get_report_detail / resolve_items / item_children
    item_detail / item_products            ← app/tools/reports.py + search.py
    form_guide / concept_reports / value_lookup / family_map / catalog_health
                                           ← app/tools/hubpick.py

inputSchema 는 **타입 힌트에서 자동 생성**한다 (SDK FastMCP `Tool.from_function`,
mcp==1.28.1 표준 기능). 예전에는 11개 스키마를 손으로 dict 리터럴로 적어 함수
시그니처와 평행 유지했고, 실제로 `item_products` 의 `code10` 이 스키마에서
누락되는 드리프트가 있었다. 한국어 파라미터 설명은 `Annotated[..., Field(...)]`
으로 옮겨 프롬프트 엔지니어링 산출물을 그대로 보존한다.

전통 MCP 방식으로 서빙한다(streamable HTTP). Agent 는 같은 프로세스의 서버를
SDK 인메모리 트랜스포트(mcp_client.local=True)로 호출하고, /mcp 는 curl 등
외부 클라이언트의 독립 검증용으로 유지한다.

에러 규격(SDK 1.28.1 표준): 툴 예외는 lowlevel 핸들러가 `isError=True`
CallToolResult 로 바꾸고 메시지를 text 로 전달한다. 입력오류(ValueError 계열)와
내부오류(RuntimeError 계열) 모두 메시지가 모델에게 그대로 가서 재시도할 수 있다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import FastAPI
from mcp.server.fastmcp.tools import Tool as FastTool
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool as MCPTool
from pydantic import Field

from .tools import hubpick as H
from .tools import reports as R
from .tools import search as S
from .utils import setup_logger

logger = setup_logger("custom_chat.mcp_server")

SERVER_NAME = "jodal-stats"
SERVER_VERSION = "0.1.0"

_MAX_TOP_K = 30

# ---- 툴 함수: 시그니처가 곧 inputSchema 다 ----


def _search_reports(
    query: Annotated[str, Field(description="원하는 통계 1문장")],
    top_k: Annotated[
        int, Field(description=f"후보 수 (default 10, max {_MAX_TOP_K})")
    ] = 10,
    vector_weight: Annotated[
        float | None,
        Field(
            description=(
                "vector 가중 w 0~1 (default 0.7). "
                "score=(1-w)/(K+rb)+w/(K+rv). 0 이면 BM25 단독."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    return S.search_reports(query=query, top_k=top_k, vector_weight=vector_weight)


def _get_report_detail(
    report_id: Annotated[
        str | None, Field(description="후보 report_id (예: 00010)")
    ] = None,
    report_name: Annotated[
        str | None,
        Field(description="보고서명 전체 또는 일부 (예: 수요기관별 계약납품요구 실적통계)"),
    ] = None,
) -> dict[str, Any]:
    return S.get_report_detail(report_id=report_id, report_name=report_name)


def _resolve_items(
    query: Annotated[str, Field(description="물품 표현 그대로 (예: 의자, 레미콘)")],
    level: Annotated[str, Field(description="품명 | 세부품명 | all (default all)")] = "all",
    top_k: Annotated[
        int, Field(description=f"후보 수 (default 10, max {_MAX_TOP_K})")
    ] = 10,
) -> dict[str, Any]:
    return R.resolve_items(query=query, level=level, top_k=top_k)


def _item_children(
    code: Annotated[
        str, Field(description="2·4·6·8자리 prefix (예: 43, 4321, 432115, 43211501)")
    ],
) -> dict[str, Any]:
    return R.item_children(code=code)


def _item_detail(
    code: Annotated[
        str, Field(description="8자리 품명 또는 10자리 세부품명 번호")
    ],
) -> dict[str, Any]:
    return R.item_detail(code=code)


def _item_products(
    code: Annotated[
        str | None, Field(description="10자리 세부품명 번호 (예: 3011150501)")
    ] = None,
    code10: Annotated[
        str | None, Field(description="10자리 세부품명 번호 (code 와 동일, 별칭)")
    ] = None,
) -> dict[str, Any]:
    return R.item_products(code=code, code10=code10)


def _form_guide(
    report_id: Annotated[str | None, Field(description="보고서 ID (예: 00262)")] = None,
    report_name: Annotated[
        str | None, Field(description="보고서명 전체 또는 일부")
    ] = None,
    include_options: Annotated[
        bool,
        Field(
            description=(
                "선택지 값까지 받을지 (기본 false). "
                "사용자가 고를 수 있는 값을 물었을 때만 true."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    return H.form_guide(
        report_id=report_id, report_name=report_name, include_options=include_options
    )


def _concept_reports(
    concept_id: Annotated[str, Field(description="개념 ID (예: female_biz)")],
) -> dict[str, Any]:
    return H.concept_reports(concept_id=concept_id)


def _value_lookup(
    query: Annotated[
        str, Field(description="찾을 값 (기관명·업체명 등). 공백 제거 후 비교.")
    ],
    report_id: Annotated[
        str | None, Field(description="지정 시 (2) 모드. 생략하면 (1) 131건 전수 스캔.")
    ] = None,
    cond: Annotated[
        str | None,
        Field(description="조건명 (report_id 지정 시 필수). form_guide 의 name 을 쓴다."),
    ] = None,
    intent: Annotated[
        str | None,
        Field(description="질문의 나머지 부분. 기관 1개가 60~70건에 걸릴 때 정답을 가르는 신호."),
    ] = None,
    top_k: Annotated[
        int, Field(description=f"후보 수 (default 10, max {_MAX_TOP_K})")
    ] = 10,
) -> dict[str, Any]:
    return H.value_lookup(
        query=query, report_id=report_id, cond=cond, intent=intent, top_k=top_k
    )


def _family_map(
    family: Annotated[str, Field(description="family 이름 (예: 입찰공고)")],
) -> dict[str, Any]:
    return H.family_map(family=family)


def _catalog_health() -> dict[str, Any]:
    return H.catalog_health()


# (name, description, FastTool — schema 는 시그니처에서 자동 생성)
_SPECS: tuple[tuple[str, str, FastTool], ...] = (
    ("search_reports", S.SEARCH_REPORTS_DESC, _search_reports),
    ("get_report_detail", S.GET_REPORT_DETAIL_DESC, _get_report_detail),
    ("resolve_items", R.RESOLVE_ITEMS_DESC, _resolve_items),
    ("item_children", R.ITEM_CHILDREN_DESC, _item_children),
    ("item_detail", R.ITEM_DETAIL_DESC, _item_detail),
    ("item_products", R.ITEM_PRODUCTS_DESC, _item_products),
    ("form_guide", H.FORM_GUIDE_DESC, _form_guide),
    ("concept_reports", H.CONCEPT_REPORTS_DESC, _concept_reports),
    ("value_lookup", H.VALUE_LOOKUP_DESC, _value_lookup),
    ("family_map", H.FAMILY_MAP_DESC, _family_map),
    ("catalog_health", H.CATALOG_HEALTH_DESC, _catalog_health),
)

_SPECS = tuple(
    (name, desc, FastTool.from_function(fn, name=name, description=desc))
    for name, desc, fn in _SPECS
)

_BY_NAME: dict[str, tuple[str, FastTool]] = {name: (desc, tool) for name, desc, tool in _SPECS}


def tool_names() -> list[str]:
    return [name for name, *_ in _SPECS]


# ---------- 서버 ----------

def build_server() -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[MCPTool]:
        return [
            MCPTool(name=name, description=tool.description, inputSchema=tool.parameters)
            for name, _, tool in _SPECS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        spec = _BY_NAME.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        _, tool = spec
        try:
            result = tool.fn(**(arguments or {}))
        except (H.HubInputError, R.GoodsInputError) as e:
            # 입력오류. 사용자에게 디버깅 가능한 한 줄 가이드가 낫다.
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

    기본 구성은 인메모리 연결이라 포트 불일치 부류의 사고가 없고, 검사는
    툴 수가 스펙과 일치하는지만 확인한다.
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
    """기본 구성(local=True)은 HTTP 루프백이 아니라 SDK 인메모리 세션이므로
    포트 불일치 부류의 사고가 원천적으로 없다. 검사는 discover 결과 툴 수가
    스펙과 일치하는지만 확인한다. 원격 서버(url 있는)는 기존대로 HTTP 검증."""
    from . import config
    from .tools import mcp_client

    for s in config.MCP_SERVERS:
        url = s.get("url", "")
        is_local = bool(s.get("local")) or (not url)
        if not url and not is_local:
            continue
        try:
            if is_local:
                found = await asyncio.to_thread(
                    mcp_client.local_server(s.get("name", "jodal")).list_tools
                )
            else:
                found = await asyncio.to_thread(
                    mcp_client.discover_mcp_tools,
                    url,
                    s.get("headers"),
                    s.get("transport", "STREAMABLE_HTTP"),
                )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "MCP 자기연결 실패 %s (%s): %s — 조달 툴이 사라진 채 챗이 도둑질된다.",
                url or "(in-memory)",
                s.get("name"),
                e,
            )
            continue
        names = sorted(t.name for t in found)
        if len(names) != len(_SPECS):
            logger.error(
                "MCP 자기연결 경고 %s: 툴 %d/%d 개만 노출됨. 누락=%s",
                url or "(in-memory)",
                len(names),
                len(_SPECS),
                sorted(set(n for n, *_ in _SPECS) - set(names)),
            )
        else:
            logger.info(
                "MCP self-check OK %s (%d tools)", url or "(in-memory)", len(names)
            )
