"""Minimal MCP client. Port of Onyx server/features/mcp/client.py core.

Drops SSRF factory, OAuth, metrics, resources. Keeps sync
discover + call over streamable-http / SSE via the `mcp` SDK.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from datetime import timedelta
from enum import Enum
from typing import Any, TypeVar

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextResourceContents
from mcp.types import Tool as MCPLibTool
from pydantic import BaseModel

from ..utils import setup_logger

logger = setup_logger("custom_chat.mcp")

T = TypeVar("T", covariant=True)

MCP_TOOL_CALL_TIMEOUT_SECONDS = 60


class MCPTransport(str, Enum):
    SSE = "SSE"
    STREAMABLE_HTTP = "STREAMABLE_HTTP"


def _transport(value: str) -> MCPTransport:
    try:
        return MCPTransport(str(value).upper())
    except ValueError:
        return MCPTransport.STREAMABLE_HTTP


def _run_sync(coro: Any) -> Any:
    """asyncio.run in a worker thread (port of run_async_sync_no_cancel)."""
    ctx = contextvars.copy_context()

    def _run() -> Any:
        return ctx.run(asyncio.run, coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def _runner(
    server_url: str,
    headers: dict[str, str] | None,
    transport: MCPTransport,
    fn_name: str,
) -> Any:
    client_func = (
        streamablehttp_client if transport == MCPTransport.STREAMABLE_HTTP else sse_client
    )

    async def _run() -> Any:
        async with client_func(server_url, headers=headers or {}) as streams:
            read, write = (streams[0], streams[1])
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=MCP_TOOL_CALL_TIMEOUT_SECONDS),
            ) as session:
                if fn_name == "discover":
                    await session.initialize()
                    listed = await session.list_tools()
                    return listed.tools
                raise ValueError(f"unknown fn {fn_name}")

    return _run_sync(_run())


async def _call_async(
    server_url: str,
    headers: dict[str, str] | None,
    transport: MCPTransport,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    client_func = (
        streamablehttp_client if transport == MCPTransport.STREAMABLE_HTTP else sse_client
    )
    async with client_func(server_url, headers=headers or {}) as streams:
        read, write = (streams[0], streams[1])
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=MCP_TOOL_CALL_TIMEOUT_SECONDS),
        ) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return flatten_result(result)


def flatten_result(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content or []:
        btype = getattr(block, "type", "")
        if btype == "text" and getattr(block, "text", None):
            parts.append(block.text)
        elif btype == "resource":
            resource = getattr(block, "resource", None)
            if isinstance(resource, TextResourceContents) and resource.text:
                parts.append(resource.text)
    text = "\n\n".join(p for p in parts if p)
    if text:
        return text
    structured = getattr(result, "structuredContent", None)
    return str(structured) if structured is not None else ""


def discover_mcp_tools(
    server_url: str,
    headers: dict[str, str] | None = None,
    transport: str = "STREAMABLE_HTTP",
) -> list[MCPLibTool]:
    return _runner(server_url, headers, _transport(transport), "discover")


def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
    transport: str = "STREAMABLE_HTTP",
) -> str:
    ctx = contextvars.copy_context()

    def _run() -> str:
        return ctx.run(
            asyncio.run,
            _call_async(server_url, headers, _transport(transport), tool_name, arguments),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


class DiscoveredTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = {}
