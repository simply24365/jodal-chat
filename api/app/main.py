"""FastAPI service: POST /chat (SSE or JSON), GET /health, GET /tools, MCP /mcp."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from . import config, prompts
from .llm import LLM
from .loop import LoopResult, run_loop
from .models import (
    ChatFullResponse,
    ChatMessageSimple,
    ChatRequest,
    ChatTurnMessage,
    MCPServerConfig,
    MessageType,
    Packet,
    SearchDoc,
)
from .mcp_server import lifespan as mcp_lifespan
from .mcp_server import mcp_asgi_app
from .tools.interface import Tool
from .tools.mcp_tool import build_mcp_tools
from .tools.open_url import OpenURLTool
from .tools.web_search import WebSearchTool, build_provider
from .utils import setup_logger
logger = setup_logger("custom_chat.main")

app = FastAPI(title="jodal-api", lifespan=mcp_lifespan)

# 조달데이터허브 툴 11개를 MCP 로 직접 서빙한다 (mcp/ Cloudflare Worker 대체).
# Agent 쪽은 MCP 클라이언트(config.MCP_SERVERS)로 이 경로를 호출한다.
app.mount("/mcp", mcp_asgi_app())

# v0 session store: session_id -> prior turns (user/assistant text only).
_sessions: OrderedDict[str, list[ChatTurnMessage]] = OrderedDict()


def _build_history(
    req: ChatRequest, *, has_web_search: bool, has_open_url: bool,
    has_jodal: bool = False,
) -> list[ChatMessageSimple]:
    base = config.SYSTEM_PROMPT or prompts.DEFAULT_SYSTEM_PROMPT
    system = prompts.build_system_prompt(
        base,
        should_cite_documents=has_web_search or has_open_url or has_jodal,
        has_web_search=has_web_search,
        has_open_url=has_open_url,
        has_jodal=has_jodal,
    )
    history: list[ChatMessageSimple] = [
        ChatMessageSimple(message=system, message_type=MessageType.SYSTEM)
    ]
    prior = req.history
    if req.session_id and req.session_id in _sessions and not req.history:
        prior = _sessions[req.session_id]
    for turn in prior[-20:]:
        history.append(
            ChatMessageSimple(
                message=turn.content,
                message_type=(
                    MessageType.USER if turn.role == "user" else MessageType.ASSISTANT
                ),
            )
        )
    history.append(
        ChatMessageSimple(message=req.message, message_type=MessageType.USER)
    )
    return history


def _build_tools(req: ChatRequest) -> list[Tool]:
    tools: list[Tool] = []
    if req.web_search:
        # Search toolkit rides one flag: web_search finds, open_url reads.
        # (Onyx ties live-fetch to the same Web Search chat toggle.)
        try:
            tools.append(WebSearchTool(tool_id=1))
        except RuntimeError as e:
            logger.warning("Web search disabled: %s", e)
        tools.append(OpenURLTool(tool_id=2))
    servers = req.mcp_servers
    if servers is None:
        servers = [MCPServerConfig(**s) for s in config.MCP_SERVERS]
    if servers:
        tools.extend(build_mcp_tools(servers, start_id=100))
    return tools


def _run(req: ChatRequest) -> Iterator[Packet | LoopResult]:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    try:
        llm = LLM()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM init failed: {e}")
    tools = _build_tools(req)
    has_web_search = any(isinstance(t, WebSearchTool) for t in tools)
    has_open_url = any(isinstance(t, OpenURLTool) for t in tools)
    has_jodal = any("jodal" in (t.name or "") for t in tools)
    history = _build_history(
        req, has_web_search=has_web_search, has_open_url=has_open_url,
        has_jodal=has_jodal,
    )
    gen = run_loop(
        history=history,
        tools=tools,
        llm=llm,
        max_cycles=req.max_cycles,
    )
    result: LoopResult | None = None
    while True:
        try:
            item = next(gen)
        except StopIteration as stop:
            result = stop.value
            break
        yield item
    assert result is not None
    if req.session_id:
        prior = _sessions.get(req.session_id, [])
        prior = [*prior, ChatTurnMessage(role="user", content=req.message)]
        if result.answer:
            prior.append(ChatTurnMessage(role="assistant", content=result.answer))
        _sessions[req.session_id] = prior[-40:]
        while len(_sessions) > config.MAX_SESSIONS:
            _sessions.popitem(last=False)
    yield result


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/tools")
def list_tools() -> dict:
    defs: list[dict] = []
    defs.append(OpenURLTool(tool_id=2).tool_definition())
    try:
        defs.append(WebSearchTool(tool_id=1).tool_definition())
    except RuntimeError as e:
        defs.append({"web_search": f"disabled: {e}"})
    servers = [MCPServerConfig(**s) for s in config.MCP_SERVERS]
    if servers:
        for t in build_mcp_tools(servers, start_id=100):
            defs.append(t.tool_definition())
    return {"tools": defs}


@app.post("/chat", response_model=None)
def chat(req: ChatRequest) -> StreamingResponse | JSONResponse:
    if not req.stream:
        items = list(_run(req))
        result = items[-1]
        assert isinstance(result, LoopResult)
        body = ChatFullResponse(
            answer=result.answer or "",
            tool_calls=result.tool_calls,
            top_documents=list(result.citation_docs.values()),
        )
        return JSONResponse(content=body.model_dump())
    gen = _run(req)

    def event_stream() -> Iterator[str]:
        result: LoopResult | None = None
        try:
            while True:
                try:
                    item = next(gen)  # type: ignore[arg-type]
                except StopIteration as stop:
                    result = stop.value
                    break
                if isinstance(item, Packet):
                    yield json.dumps(item.model_dump(), ensure_ascii=False) + "\n"
            if result is not None:
                yield json.dumps(
                    {"type": "final", "answer": result.answer}, ensure_ascii=False
                ) + "\n"
        except HTTPException as e:
            yield json.dumps({"type": "error", "error": e.detail}) + "\n"
        except Exception as e:
            logger.exception("Chat stream failed")
            yield json.dumps({"type": "error", "error": str(e)}) + "\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return {"session_id": session_id, "history": _sessions.get(session_id, [])}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    _sessions.pop(session_id, None)
    return {"deleted": session_id}


def top_documents(result: LoopResult) -> list[SearchDoc]:
    return list(result.citation_docs.values())
