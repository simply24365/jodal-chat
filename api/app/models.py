"""Pydantic models: chat messages, tool calls, packets, API shapes."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL_RESPONSE = "tool_call_response"


class ToolCallSimple(BaseModel):
    tool_call_id: str
    tool_name: str
    tool_arguments: dict[str, Any]


class ChatMessageSimple(BaseModel):
    message: str
    message_type: MessageType
    tool_call_id: str | None = None
    tool_calls: list[ToolCallSimple] | None = None


class ToolCallKickoff(BaseModel):
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any]
    turn_index: int = 0
    tab_index: int = 0


class ToolResponse(BaseModel):
    rich_response: Any | None = None
    llm_facing_response: str
    tool_call: ToolCallKickoff | None = None


class LlmStepResult(BaseModel):
    reasoning: str | None = None
    answer: str | None = None
    tool_calls: list[ToolCallKickoff] | None = None
    raw_answer: str | None = None
    finish_reason: str | None = None


class ToolChoiceOptions(str, Enum):
    REQUIRED = "required"
    AUTO = "auto"
    NONE = "none"


# --- LLM wire types (OpenAI chat-completions shape, subset of Onyx) ---


class FunctionCall(BaseModel):
    name: str | None = None
    arguments: str | None = None


class DeltaToolCall(BaseModel):
    id: str | None = None
    index: int = 0
    function: FunctionCall | None = None


class Delta(BaseModel):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[DeltaToolCall] = Field(default_factory=list)


class StreamingChoice(BaseModel):
    finish_reason: str | None = None
    delta: Delta = Field(default_factory=Delta)


class ModelResponseStream(BaseModel):
    choice: StreamingChoice = Field(default_factory=StreamingChoice)


class ToolCall(BaseModel):
    type: Literal["function"] = "function"
    id: str
    function: FunctionCall


class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: str


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class ToolMessage(BaseModel):
    role: Literal["tool"] = "tool"
    content: str
    tool_call_id: str


ChatCompletionMessage = SystemMessage | UserMessage | AssistantMessage | ToolMessage
LanguageModelInput = list[ChatCompletionMessage]


# --- Streaming packets (subset of Onyx streaming_models) ---


class Packet(BaseModel):
    turn_index: int = 0
    tab_index: int = 0
    # Discriminator; payload keys ride alongside (flat, like Onyx model_dump).
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


# --- Search docs ---


class SearchDoc(BaseModel):
    document_id: str
    title: str
    link: str | None = None
    snippet: str


# --- API ---


class ChatTurnMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


class MCPServerConfig(BaseModel):
    name: str
    url: str
    transport: str = "STREAMABLE_HTTP"
    headers: dict[str, str] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str
    history: list[ChatTurnMessage] = Field(default_factory=list)
    session_id: str | None = None
    stream: bool = True
    max_cycles: int | None = None
    web_search: bool = True
    mcp_servers: list[MCPServerConfig] | None = None


class ToolCallResult(BaseModel):
    tool_name: str
    tool_arguments: dict[str, Any]
    tool_result: str
    search_docs: list[SearchDoc] | None = None


class ChatFullResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCallResult] = Field(default_factory=list)
    top_documents: list[SearchDoc] = Field(default_factory=list)
    error: str | None = None
