"""MCP tool wrapper. Port of Onyx MCPTool, minus DB/OAuth/metrics."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import config
from ..models import MCPServerConfig, Packet, SearchDoc, ToolResponse
from ..utils import setup_logger
from . import mcp_client
from .interface import Tool

logger = setup_logger("custom_chat.mcp_tool")

_INVALID = re.compile(r"[^a-zA-Z0-9_-]")

# citation 계약을 지원하는 jodal 툴 — 응답에 report_id + 이름 목록이 있는 것들.
# web_search/open_url 과 동일한 SearchDoc+citation_mapping 계약으로 통일해,
# 루프의 인용 수집·근거 검증(citation_docs)이 jodal 툴에도 작동하게 한다.
_CITATION_TOOLS = {"search_reports", "get_report_detail", "value_lookup", "concept_reports"}


def _extract_citation_docs(result_text: str) -> list[SearchDoc]:
    """툴 결과 JSON에서 (report_id, 보고서명)을 SearchDoc으로 뽑는다.

    실패해도 툴 호출 자체는 성공한 것이므로 조용히 빈 목록을 돌려준다.
    """
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    items: list[tuple[str, str]] = []
    for cand in data.get("candidates") or []:
        rid, nm = cand.get("report_id"), cand.get("name")
        if rid and nm:
            items.append((str(rid), str(nm)))
    for rep in data.get("reports") or []:
        rid, nm = rep.get("report_id"), rep.get("name")
        if rid and nm:
            items.append((str(rid), str(nm)))
    if data.get("report_id") and data.get("name"):
        rid, nm = str(data["report_id"]), str(data["name"])
        if (rid, nm) not in items:
            items.append((rid, nm))
    seen: set[str] = set()
    docs: list[SearchDoc] = []
    for rid, nm in items:
        if rid in seen:
            continue
        seen.add(rid)
        docs.append(SearchDoc(
            document_id=f"JODAL_REPORT_{rid}",
            title=nm,
            link=f"https://data.g2b.go.kr/link/AISC001_01/?g2bOpen={rid}",
            snippet="",
        ))
    return docs

# httpx/anyio는 예외를 ExceptionGroup으로 감싼다 ("unhandled errors in a
# TaskGroup (1 sub-exception)") — str()만 쓰면 진짜 원인이 사라진다.
_MAX_UNWRAP_DEPTH = 5


def unwrap_exception(exc: BaseException) -> BaseException:
    """ innermost leaf of an ExceptionGroup chain. """
    seen: set[int] = set()
    cur = exc
    for _ in range(_MAX_UNWRAP_DEPTH):
        nested = getattr(cur, "exceptions", None)
        if not nested or id(cur) in seen:
            break
        seen.add(id(cur))
        cur = nested[0]
    return cur


def describe_error(exc: BaseException) -> str:
    """Tool-facing one-liner: leaf message, HTTP status when available."""
    leaf = unwrap_exception(exc)
    msg = str(leaf).strip() or leaf.__class__.__name__
    status = getattr(getattr(leaf, "response", None), "status_code", None)
    if status is not None and str(status) not in msg:
        msg = f"HTTP {status}: {msg}"
    return msg[:200]


def sanitize_tool_name(name: str) -> str:
    return _INVALID.sub("_", name)


class MCPTool(Tool[None]):
    """One MCP server tool, exposed as an OpenAI function tool."""

    def __init__(
        self,
        tool_id: int,
        server: MCPServerConfig,
        mcp_tool_name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> None:
        self._id = tool_id
        self._server = server
        self._mcp_tool_name = mcp_tool_name
        self._name = sanitize_tool_name(f"mcp_{server.name}_{mcp_tool_name}")
        self._description = description or f"MCP tool {mcp_tool_name} on {server.name}"
        params = dict(parameters or {})
        if params.get("type") == "object" and "properties" not in params:
            params["properties"] = {}
        self._parameters = params
        # 도메인 호출 정책은 설정 계층(config.MCP_TOOL_MAX_CALLS)이 소유하고
        # 이름(원본 MCP 툴명)으로 매핑한다. 루프는 이 속성만 읽는다.
        max_calls = config.MCP_TOOL_MAX_CALLS.get(mcp_tool_name)
        if max_calls is not None:
            self.max_calls_per_turn = max_calls
            self.exhausted_reminder = (
                f"{mcp_tool_name}는 {max_calls}회로 충분하다. "
                "더 호출하지 말고 가진 후보로 좁히거나 되물어라."
            )

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def display_name(self) -> str:
        return self._mcp_tool_name

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": self._description,
                "parameters": self._parameters,
            },
        }

    def emit_start(self, turn_index: int, tab_index: int) -> Packet:
        return Packet(
            turn_index=turn_index,
            tab_index=tab_index,
            type="custom_tool_start",
            data={"tool_name": self._name},
        )

    def run(
        self,
        turn_index: int,
        tab_index: int,
        override_kwargs: None = None,
        **llm_kwargs: Any,
    ) -> tuple[ToolResponse, list[Packet]]:
        packets = [
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="custom_tool_args",
                data={"tool_name": self._name, "tool_args": llm_kwargs},
            )
        ]
        try:
            result_text = mcp_client.call_mcp_tool(
                self._server.url,
                self._mcp_tool_name,
                llm_kwargs,
                headers=self._server.headers,
                transport=self._server.transport,
                server_name=self._server.name,
                local=self._server.local,
            )
            payload = {"tool_result": result_text}
            llm_str = json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            reason = describe_error(e)
            logger.error(
                "MCP tool %s failed: %s: %s",
                self._name, type(unwrap_exception(e)).__name__, reason,
                exc_info=True,
            )
            payload = {"error": f"Tool execution failed: {reason}"}
            llm_str = json.dumps(payload, ensure_ascii=False)
        docs: list[SearchDoc] = []
        if self._mcp_tool_name in _CITATION_TOOLS and "error" not in payload:
            docs = _extract_citation_docs(result_text)
        rich: dict[str, Any] = {"tool_name": self._name, **payload}
        if docs:
            rich["search_docs"] = [d.model_dump() for d in docs]
            rich["citation_mapping"] = {
                str(i + 1): d.document_id for i, d in enumerate(docs)
            }
            # web_search 와 동일한 UI 계약: citation_info 로 사용자 화면에 근거를 띄우고
            # LLM facing 페이로드에도 [N] 인용 대상을 명시해 답변의 보고서명이
            # 근거와 1:1 대응되게 한다.
            for i, d in enumerate(docs):
                packets.append(
                    Packet(
                        turn_index=turn_index,
                        tab_index=tab_index,
                        type="citation_info",
                        data={
                            "citation_number": i + 1,
                            "document_id": d.document_id,
                            "title": d.title,
                            "link": d.link,
                        },
                    )
                )
            cited = json.dumps(
                {"citable_reports": [
                    {"num": i + 1, "report_id": d.document_id.removeprefix("JODAL_REPORT_"),
                     "name": d.title}
                    for i, d in enumerate(docs)
                ]},
                ensure_ascii=False,
            )
            llm_str = (
                llm_str[:-1] + ", " + cited[1:-1] + "}"
                if llm_str.endswith("}")
                else llm_str
            )
        packets.append(
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="custom_tool_delta",
                data={"tool_name": self._name, **payload},
            )
        )
        response = ToolResponse(
            rich_response=rich,
            llm_facing_response=llm_str,
        )
        return response, packets


def build_mcp_tools(
    servers: list[MCPServerConfig], start_id: int = 100
) -> list[MCPTool]:
    """Discover tools on each server and wrap them. Failures -> no tools."""
    tools: list[MCPTool] = []
    tool_id = start_id
    seen: set[str] = set()
    for server in servers:
        if server.local:
            # 같은 프로세스의 MCP 서버를 SDK 인메모리 트랜스포트로 직결.
            # HTTP 없이 discover/call — 포트·lifespan 부류의 장애가 없다.
            try:
                discovered = mcp_client.discover_mcp_tools(
                    "", server_name=server.name, local=True
                )
            except Exception as e:
                logger.warning(
                    "MCP local discover failed for %s: %s", server.name, e
                )
                continue
        else:
            try:
                discovered = mcp_client.discover_mcp_tools(
                    server.url, headers=server.headers, transport=server.transport
                )
            except Exception as e:
                logger.warning("MCP discover failed for %s (%s): %s", server.name, server.url, e)
                continue
        for t in discovered:
            schema = t.inputSchema or {}
            tool = MCPTool(
                tool_id=tool_id,
                server=server,
                mcp_tool_name=t.name,
                description=t.description or "",
                parameters=schema,
            )
            if tool.name in seen:
                tool._name = sanitize_tool_name(f"mcp_{server.name}_{tool_id}_{t.name}")
            seen.add(tool.name)
            tools.append(tool)
            tool_id += 1
    return tools
