"""Minimal MCP client. Port of Onyx server/features/mcp/client.py core.

Drops SSRF factory, OAuth, metrics, resources. Keeps sync
discover + call over streamable-http / SSE via the `mcp` SDK.

`local=True` 서버는 HTTP 루프백 대신 SDK 공식 인메모리 트랜스포트
(`mcp.shared.memory.create_connected_server_and_client_session`)으로 같은
프로세스의 `Server` 객체를 직결한다. 스레드/이벤트루프/TCP/핸드셰이크 비용이
호출마다 아니라 세션당 한 번만 들고, 포트 불일치라는 부류의 사고 자체가 사라진다.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
from datetime import timedelta
from enum import Enum
from typing import Any, TypeVar

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextResourceContents
from mcp.types import Tool as MCPLibTool
from pydantic import BaseModel

from ..utils import setup_logger

logger = setup_logger("custom_chat.mcp")

T = TypeVar("T", covariant=True)

MCP_TOOL_CALL_TIMEOUT_SECONDS = 60

# ---- 인메모리 로컬 서버 (SDK 표준 트랜스포트) ----
#
# anyio 의 취소 스코프는 "연 태스크에서 닫아야" 한다. 그래서 세션을 여는
# `create_connected_server_and_client_session` 을 호출자 스레드에서 asyncio.run
# 으로 열고 다른 스레드에서 쓰는 방식은 SDK 가 거부한다(실측:
# "Attempted to exit cancel scope in a different task"). 표준 해법은 **세션을
# 소유한 단일 소유자 태스크**를 전용 스레드/루프에 두고, 요청은 큐로 넘겨
# 소유자 태스크 안에서 실행하게 하는 것이다. 요청-응답 브리지는
# concurrent.futures.Future 를 그대로 쓴다.


class _LocalMCP:
    """같은 프로세스의 MCP 서버에 인메모리로 연결된 소유자 태스크."""

    def __init__(self, server_name: str) -> None:
        self.name = server_name
        self._ready = threading.Event()
        self._queue: asyncio.Queue | None = None
        self._loop = asyncio.new_event_loop()
        self._tools: list[MCPLibTool] = []
        self._error: BaseException | None = None
        t = threading.Thread(target=self._serve, name="mcp-inmemory", daemon=True)
        t.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._owner())
        self._loop.run_forever()

    async def _owner(self) -> None:
        from .. import mcp_server

        self._queue = asyncio.Queue()
        try:
            cm = create_connected_server_and_client_session(
                mcp_server.build_server(),
                read_timeout_seconds=timedelta(
                    seconds=MCP_TOOL_CALL_TIMEOUT_SECONDS
                ),
            )
            async with cm as session:
                await session.initialize()
                self._tools = (await session.list_tools()).tools
                self._ready.set()
                while True:
                    fn, fut = await self._queue.get()
                    if fut.set_running_or_notify_cancel() is False:
                        continue
                    try:
                        fut.set_result(await fn(session))
                    except BaseException as e:  # noqa: BLE001
                        fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
            logger.error("in-memory MCP owner died: %s", e)

    def _submit(self, fn: Any) -> Any:
        self._ready.wait(timeout=MCP_TOOL_CALL_TIMEOUT_SECONDS)
        if self._error is not None:
            raise self._error
        assert self._queue is not None
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (fn, fut))
        return fut.result(timeout=MCP_TOOL_CALL_TIMEOUT_SECONDS)

    def list_tools(self) -> list[MCPLibTool]:
        self._ready.wait(timeout=MCP_TOOL_CALL_TIMEOUT_SECONDS)
        if self._error is not None:
            raise self._error
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        async def _call(session: ClientSession) -> str:
            result = await session.call_tool(tool_name, arguments)
            return flatten_result(result)

        return self._submit(_call)


_local_servers: dict[str, _LocalMCP] = {}
_local_guard = threading.Lock()


def local_server(server_name: str) -> _LocalMCP:
    """이름별로 하나의 인메모리 연결을 만들고 재사용한다."""
    with _local_guard:
        existing = _local_servers.get(server_name)
        if existing is None:
            existing = _LocalMCP(server_name)
            _local_servers[server_name] = existing
        return existing

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
    *,
    server_name: str = "",
    local: bool = False,
) -> list[MCPLibTool]:
    if local:
        return local_server(server_name or "jodal").list_tools()
    return _runner(server_url, headers, _transport(transport), "discover")


def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
    transport: str = "STREAMABLE_HTTP",
    *,
    server_name: str = "",
    local: bool = False,
) -> str:
    if local:
        return local_server(server_name or "jodal").call_tool(tool_name, arguments)
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
