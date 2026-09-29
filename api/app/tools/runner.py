"""Parallel tool runner. Port of Onyx run_tool_calls, web_search+MCP only."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from ..models import Packet, ToolCallKickoff, ToolResponse
from ..prompts import TOOL_CALL_FAILURE_PROMPT
from ..utils import setup_logger
from .interface import Tool, ToolCallException
from .mcp_tool import MCPTool
from .open_url import OpenURLTool
from .web_search import QUERIES_FIELD, WebSearchTool

logger = setup_logger("custom_chat.tool_runner")

# 개별 툴 호출의 상한. 개별 툴 내부 타임아웃(MCP 60s, requests 30s)보다 큰
# 최후 방어선이다 — 툴 내부가 타임아웃 없는 연산에 갇혀 루프가 영원히 안 끝나는
# 것을 막는다. 실패한 호출은 표준 ToolCallException 계약으로 모델에게 돌아간다.
TOOL_EXECUTION_TIMEOUT_SECONDS = int(
    os.environ.get("TOOL_EXECUTION_TIMEOUT_SECONDS", "600")
)
# Citation slots reserved per search tool so parallel calls never collide.
CITATION_SLOT = 100


def merge_tool_calls(calls: list[ToolCallKickoff]) -> list[ToolCallKickoff]:
    """Collapse repeated web_search calls into one (port of Onyx _merge)."""
    grouped: dict[str, list[ToolCallKickoff]] = {}
    for call in calls:
        grouped.setdefault(call.tool_name, []).append(call)
    merged: list[ToolCallKickoff] = []
    for name, group in grouped.items():
        if name == WebSearchTool.NAME and len(group) > 1:
            queries: list[str] = []
            for call in group:
                values = call.tool_args.get(QUERIES_FIELD, [])
                if isinstance(values, list):
                    queries.extend(values)
                elif values:
                    queries.append(str(values))
            first = group[0]
            merged.append(
                ToolCallKickoff(
                    tool_call_id=first.tool_call_id,
                    tool_name=name,
                    tool_args={QUERIES_FIELD: queries},
                    turn_index=first.turn_index,
                    tab_index=first.tab_index,
                )
            )
        else:
            merged.extend(group)
    return merged


def run_tool_calls(
    tool_calls: list[ToolCallKickoff],
    tools: list[Tool],
    citation_start: int = 1,
    max_concurrent_tools: int | None = None,
) -> tuple[list[ToolResponse], list[Packet], int]:
    """Run merged tool calls in parallel.

    Returns (responses, packets, next_citation_start).
    """
    packets: list[Packet] = []
    if not tool_calls:
        return [], packets, citation_start

    merged = merge_tool_calls(tool_calls)
    by_name = {t.name: t for t in tools}
    filtered = [c for c in merged if c.tool_name in by_name]
    missed = [c for c in merged if c.tool_name not in by_name]
    missed_responses: list[ToolResponse] = []
    start = citation_start
    for c in missed:
        logger.warning("Tool %s not found in tools list", c.tool_name)
        # Onyx-style: unknown tool name becomes a per-call failure response
        # so the model sees TOOL_CALL_FAILURE_PROMPT and can self-correct.
        missed_responses.append(
            ToolResponse(
                rich_response=None,
                llm_facing_response=(
                    f"{TOOL_CALL_FAILURE_PROMPT} (unknown tool: {c.tool_name})"
                ),
                tool_call=c,
            )
        )
        packets.append(
            Packet(
                turn_index=c.turn_index, tab_index=c.tab_index, type="section_end"
            )
        )
    params: list[tuple[Tool, ToolCallKickoff, Any]] = []
    for call in filtered:
        if call.args_unparsed:
            # 표준 에이전트 패턴(OpenAI Agents SDK 관용): 인자를 파싱할 수 없으면
            # 툴을 실행하지 않고 즉시 모델에게 재생성을 요구한다. 조용한 빈 인자
            # 실행은 스키마 오류(-32602)를 유발하고 원인이 모델에게 숨겨진다.
            logger.warning(
                "Tool %s had unparsable arguments; asking model to regenerate", call.tool_name
            )
            missed_responses.append(
                ToolResponse(
                    rich_response=None,
                    llm_facing_response=(
                        f"Tool call to {call.tool_name} had invalid arguments — not valid JSON. "
                        f"Raw: {call.args_unparsed[:200]}. Regenerate the arguments as a "
                        "valid JSON object matching the tool schema and call again."
                    ),
                    tool_call=call,
                )
            )
            packets.append(
                Packet(
                    turn_index=call.turn_index, tab_index=call.tab_index, type="section_end"
                )
            )
            continue
        tool = by_name[call.tool_name]
        start_packet = tool.emit_start(call.turn_index, call.tab_index)
        if start_packet is not None:
            packets.append(start_packet)
        override: Any = None
        # Citeable tools share the starting_citation_num contract:
        # each reserves a slot so parallel calls never collide.
        # MCPTool 은 jodal 검색/값조회 툴도 SearchDoc 인용을 내놓으므로 같은 계약에 넣는다.
        if isinstance(tool, (WebSearchTool, OpenURLTool, MCPTool)):
            override = {"starting_citation_num": start}
            start += CITATION_SLOT
        params.append((tool, call, override))

    def _run_one(
        tool: Tool, call: ToolCallKickoff, override: Any
    ) -> tuple[ToolResponse | None, list[Packet]]:
        try:
            response, tool_packets = tool.run(
                call.turn_index, call.tab_index, override, **call.tool_args
            )
            response.tool_call = call
            end = Packet(
                turn_index=call.turn_index, tab_index=call.tab_index, type="section_end"
            )
            return response, [*tool_packets, end]
        except ToolCallException as e:
            logger.error("Tool call error for %s: %s", tool.name, e)
            response = ToolResponse(
                rich_response=None,
                llm_facing_response=f"Tool failed with error: {e.llm_facing_message}",
                tool_call=call,
            )
            end = Packet(
                turn_index=call.turn_index, tab_index=call.tab_index, type="section_end"
            )
            return response, [end]
        except Exception as e:
            logger.exception("Unexpected error running tool %s", tool.name)
            response = ToolResponse(
                rich_response=None,
                llm_facing_response=f"Tool failed with error: {e}",
                tool_call=call,
            )
            end = Packet(
                turn_index=call.turn_index, tab_index=call.tab_index, type="section_end"
            )
            return response, [end]

    workers = min(len(params), max_concurrent_tools or len(params))
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futures = [pool.submit(_run_one, *p) for p in params]
        results: list[tuple[ToolResponse | None, list[Packet]]] = []
        for call, fut in zip((p[1] for p in params), futures):
            try:
                results.append(fut.result(timeout=TOOL_EXECUTION_TIMEOUT_SECONDS))
            except FuturesTimeoutError:
                logger.error(
                    "Tool %s exceeded %ss timeout", call.tool_name,
                    TOOL_EXECUTION_TIMEOUT_SECONDS,
                )
                results.append(
                    (
                        ToolResponse(
                            rich_response=None,
                            llm_facing_response=(
                                f"Tool failed with error: {call.tool_name} "
                                f"timed out after {TOOL_EXECUTION_TIMEOUT_SECONDS}s"
                            ),
                            tool_call=call,
                        ),
                        [
                            Packet(
                                turn_index=call.turn_index,
                                tab_index=call.tab_index,
                                type="section_end",
                            )
                        ],
                    )
                )

    responses: list[ToolResponse] = list(missed_responses)
    for response, tool_packets in results:
        packets.extend(tool_packets)
        if response is not None:
            responses.append(response)
    return responses, packets, start
