"""Single LLM step: stream deltas, emit packets, return LlmStepResult.

Port of Onyx run_llm_step_pkt_generator, stripped of tracing,
prompt-cache, images, citation processor, deep-research, and XML filtering.
The stream-consumption core (delta accumulation -> tool kickoffs) is kept.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Generator
from typing import Any

from .llm import LLM
from .models import (
    AssistantMessage,
    ChatMessageSimple,
    DeltaToolCall,
    LanguageModelInput,
    LlmStepResult,
    MessageType,
    Packet,
    SystemMessage,
    ToolCall,
    ToolCallKickoff,
    ToolMessage,
    UserMessage,
)
from .utils import sanitize_string, setup_logger

logger = setup_logger("custom_chat.llm_step")


def translate_history(history: list[ChatMessageSimple]) -> LanguageModelInput:
    """ChatMessageSimple -> OpenAI chat-completions messages."""
    messages: LanguageModelInput = []
    for msg in history:
        if msg.message_type == MessageType.SYSTEM:
            messages.append(SystemMessage(content=msg.message))
        elif msg.message_type == MessageType.USER:
            messages.append(UserMessage(content=msg.message))
        elif msg.message_type == MessageType.ASSISTANT:
            tool_calls = None
            if msg.tool_calls:
                tool_calls = [
                    ToolCall(
                        id=tc.tool_call_id,
                        function={
                            "name": tc.tool_name,
                            "arguments": json.dumps(tc.tool_arguments),
                        },
                    )
                    for tc in msg.tool_calls
                ]
            messages.append(
                AssistantMessage(content=msg.message or None, tool_calls=tool_calls)
            )
        elif msg.message_type == MessageType.TOOL_CALL_RESPONSE:
            if not msg.tool_call_id:
                logger.warning("Dropping tool response without tool_call_id")
                continue
            messages.append(
                ToolMessage(content=msg.message, tool_call_id=msg.tool_call_id)
            )
        else:
            logger.warning("Unknown message type %s; skipping", msg.message_type)
    return messages


def _update_tool_call_with_delta(
    in_progress: dict[int, dict[str, Any]], delta: DeltaToolCall
) -> None:
    index = delta.index
    if index not in in_progress:
        in_progress[index] = {
            "id": f"fallback_{uuid.uuid4().hex}",
            "name": None,
            "arguments": "",
        }
    if delta.id:
        in_progress[index]["id"] = delta.id
    if delta.function:
        if delta.function.name:
            in_progress[index]["name"] = delta.function.name
        if delta.function.arguments:
            in_progress[index]["arguments"] += delta.function.arguments


def _extract_kickoffs(
    in_progress: dict[int, dict[str, Any]], turn_index: int
) -> list[ToolCallKickoff]:
    calls: list[ToolCallKickoff] = []
    tab = 0
    for data in in_progress.values():
        if data.get("id") and data.get("name"):
            raw = data.get("arguments")
            parsed = _parse_tool_args(raw)
            # 표준 에이전트 패턴: 인자 파싱 실패(문자열이 있었는데 JSON 이
            # 아니었음) 시 조용한 {} 로 툴을 실행하지 않는다 — 빈 인자 실행은
            # 엉뚱한 호출로 이어지고 모델이 원인을 모른다. 대신 원문을 보존해
            # runner 가 "인자 재생성" tool-response 를 돌려줄 수 있게 한다.
            #
            # 주의: parsed 가 빈 dict 인 경우는 두 가지다 — (a) 무인자 툴의
            # 정상 호출(catalog_health 등, raw=="{}"), (b) 진짜 파싱 실패.
            # falsy 체크(not parsed)로는 (a)를 (b)로 오판해 무인자 툴이
            # 3회 연속 "인자 재생성" 루프에 빠진다(실측: n03). JSON 으로
            # 파싱된 경우는 정상으로 본다.
            invalid = (
                isinstance(raw, str)
                and bool(raw.strip())
                and parsed == {}
                and raw.strip() not in ("{}", "null", "undefined")
            )
            calls.append(
                ToolCallKickoff(
                    tool_call_id=data["id"],
                    tool_name=data["name"],
                    tool_args=parsed,
                    turn_index=turn_index,
                    tab_index=tab,
                    args_unparsed=raw.strip() if invalid else None,
                )
            )
            tab += 1
    return calls


def _try_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not (
        (stripped.startswith("[") and stripped.endswith("]"))
        or (stripped.startswith("{") and stripped.endswith("}"))
    ):
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    """Port of Onyx _parse_tool_args_to_dict (string/double-encoded tolerant)."""
    if raw_args is None:
        return {}
    if isinstance(raw_args, dict):
        return {
            k: _try_parse_json_string(
                sanitize_string(v) if isinstance(v, str) else v
            )
            for k, v in raw_args.items()
        }
    if not isinstance(raw_args, str):
        return {}
    raw_args = sanitize_string(raw_args)
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {k: _try_parse_json_string(v) for k, v in parsed.items()}
    if isinstance(parsed, str):
        try:
            parsed2 = json.loads(parsed)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed2, dict):
            return {k: _try_parse_json_string(v) for k, v in parsed2.items()}
    return {}


def _find_json_objects(text: str) -> list[dict]:
    """Balanced-brace JSON scan (port of Onyx find_all_json_objects)."""
    objects: list[dict] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[i : j + 1])
                            if isinstance(parsed, dict):
                                objects.append(parsed)
                        except json.JSONDecodeError:
                            pass
                        break
        i += 1
    return objects


def _tool_name_index(tool_definitions: list[dict]) -> dict[str, dict]:
    name_to_params: dict[str, dict] = {}
    for tool_def in tool_definitions:
        func = (tool_def.get("function") or {}) if tool_def.get("type") == "function" else {}
        if func.get("name"):
            name_to_params[func["name"]] = func
    return name_to_params


# Non-JSON tool-call syntaxes we have actually seen from the chat models when
# they answer in the text channel instead of the function-call channel. Without
# this the pseudo call leaks to the user as "[tool: mcp_jodal_search_reports]"
# and the turn ends with no answer.
#   [tool: NAME]   [tool: NAME, {"query": "x"}]   [NAME({"query": "x"})]
#   <tool name="NAME">{...}</tool>   <tool>NAME</tool>
_TEXTUAL_TOOL_CALL_RE = re.compile(
    r"""
      \[\s*tool\s*:\s*(?P<a_name>[A-Za-z0-9_.-]+)\s*(?P<a_args>[^\]\n]*)\]
    | \[\s*(?P<b_name>[A-Za-z0-9_.-]+)\s*\(\s*(?P<b_args>[^\]]*?)\s*\)\s*\]
    | <tool\s+name\s*=\s*["'](?P<c_name>[A-Za-z0-9_.-]+)["']\s*>(?P<c_args>.*?)</tool>
    | <tool\s*>\s*(?P<d_name>[A-Za-z0-9_.-]+)\s*(?P<d_args>[^<]*)</tool\s*>
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def _parse_textual_args(raw: str | None) -> dict[str, Any]:
    """Best-effort args from a textual tool call. Empty dict when absent."""
    text = (raw or "").strip().strip(",").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Bare `key: value, key: value` form some models emit.
        parsed = {}
        for part in re.split(r",(?![^{]*\})", text):
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            key = key.strip().strip("\"'")
            if not key:
                continue
            value = value.strip().rstrip(",").strip()
            if not value:
                continue
            try:
                parsed[key] = json.loads(value)
            except json.JSONDecodeError:
                parsed[key] = value.strip("\"'")
    return parsed if isinstance(parsed, dict) else {}


def extract_textual_tool_calls(
    response_text: str | None,
    name_to_params: dict[str, dict],
    turn_index: int,
) -> tuple[list[ToolCallKickoff], str]:
    """Pull `[tool: NAME]`-style calls out of the text. Returns (calls, cleaned)."""
    if not response_text:
        return [], response_text or ""
    matched: list[tuple[str, dict[str, Any]]] = []

    def _take(match: re.Match[str]) -> str:
        name = ""
        args = ""
        for group in ("a", "b", "c", "d"):
            candidate = match.groupdict().get(f"{group}_name")
            if candidate:
                name = candidate.strip()
                args = match.groupdict().get(f"{group}_args") or ""
                break
        if name not in name_to_params:
            return match.group(0)  # not ours — leave the user's text alone
        matched.append((name, _parse_textual_args(args)))
        return ""  # drop the syntax from user-visible text

    cleaned = _TEXTUAL_TOOL_CALL_RE.sub(_take, response_text)
    calls = [
        ToolCallKickoff(
            tool_call_id=f"extracted_{uuid.uuid4().hex[:8]}",
            tool_name=name,
            tool_args=args,
            turn_index=turn_index,
            tab_index=tab,
        )
        for tab, (name, args) in enumerate(matched)
    ]
    return calls, cleaned


def has_textual_tool_calls(
    response_text: str | None, tool_definitions: list[dict]
) -> bool:
    """True when the text channel carries a `[tool: NAME]`-style call."""
    if not response_text:
        return False
    _, cleaned = extract_textual_tool_calls(
        response_text, _tool_name_index(tool_definitions), 0
    )
    return cleaned != response_text


def extract_tool_calls_from_text(
    response_text: str | None,
    tool_definitions: list[dict],
    turn_index: int,
) -> list[ToolCallKickoff]:
    """Fallback for models that emit tool calls as text instead of tool_calls."""
    if not response_text or not tool_definitions:
        return []
    name_to_params = _tool_name_index(tool_definitions)
    if not name_to_params:
        return []
    # Textual syntax first: a `[tool: NAME, {...}]` also carries a JSON object,
    # so scanning the original text for JSON would report the same call twice.
    textual, remainder = extract_textual_tool_calls(
        response_text, name_to_params, turn_index
    )
    matched: list[tuple[str, dict[str, Any]]] = []
    for obj in _find_json_objects(remainder):
        hit = _match_json_to_tool(obj, name_to_params)
        if hit:
            matched.append(hit)
    # The scan is a port of Onyx find_all_json_objects and yields nested objects
    # too, so `{"name": T, "arguments": {...}}` also reports its own `arguments`
    # as a bare-props match. Identical (name, args) pairs are one call.
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for name, args in matched:
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deduped.append((name, args))
    kickoffs = [
        ToolCallKickoff(
            tool_call_id=f"extracted_{uuid.uuid4().hex[:8]}",
            tool_name=name,
            tool_args=args,
            turn_index=turn_index,
            tab_index=tab,
        )
        for tab, (name, args) in enumerate(deduped)
    ]
    return kickoffs + textual


def strip_textual_tool_calls(
    response_text: str | None, tool_definitions: list[dict]
) -> str:
    """Remove `[tool: ...]`-style syntax from text bound for the user."""
    if not response_text:
        return response_text or ""
    _, cleaned = extract_textual_tool_calls(
        response_text, _tool_name_index(tool_definitions), 0
    )
    return cleaned


def _match_json_to_tool(
    obj: dict[str, Any], name_to_params: dict[str, dict]
) -> tuple[str, dict[str, Any]] | None:
    if "name" in obj and obj["name"] in name_to_params:
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict):
            return (obj["name"], args)
    if "function" in obj and isinstance(obj["function"], dict):
        fn = obj["function"]
        if fn.get("name") in name_to_params:
            args = fn.get("arguments", fn.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if isinstance(args, dict):
                return (fn["name"], args)
    for name, func in name_to_params.items():
        if name in obj and isinstance(obj[name], dict):
            return (name, obj[name])
    for name, func in name_to_params.items():
        props = (func.get("parameters") or {}).get("properties", {})
        required = (func.get("parameters") or {}).get("required", [])
        if props and all(r in obj for r in required):
            if any(p in obj for p in props):
                return (name, {k: v for k, v in obj.items() if k in props})
    return None


def run_llm_step(
    history: list[ChatMessageSimple],
    tool_definitions: list[dict],
    tool_choice: str,
    llm: LLM,
    turn_index: int,
) -> tuple[LlmStepResult, list[Packet]]:
    """Stream one step; collect packets for the caller to emit."""
    packets: list[Packet] = []
    llm_messages = [m.model_dump(exclude_none=True) for m in translate_history(history)]

    in_progress: dict[int, dict[str, Any]] = {}
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_open = False
    answer_open = False
    finish_reason: str | None = None

    for chunk in llm.stream(
        messages=llm_messages,
        tools=tool_definitions or None,
        tool_choice=tool_choice if tool_definitions else None,
    ):
        if chunk.choice.finish_reason:
            finish_reason = str(chunk.choice.finish_reason)
        delta = chunk.choice.delta
        if not delta.content and not delta.reasoning_content and not delta.tool_calls:
            continue
        if delta.reasoning_content:
            reasoning_parts.append(delta.reasoning_content)
            if not reasoning_open:
                packets.append(Packet(turn_index=turn_index, type="reasoning_start"))
                reasoning_open = True
            packets.append(
                Packet(
                    turn_index=turn_index,
                    type="reasoning_delta",
                    data={"reasoning": delta.reasoning_content},
                )
            )
        if delta.content:
            if reasoning_open:
                packets.append(Packet(turn_index=turn_index, type="reasoning_done"))
                reasoning_open = False
            answer_parts.append(delta.content)
            if not answer_open:
                packets.append(Packet(turn_index=turn_index, type="message_start"))
                answer_open = True
            packets.append(
                Packet(
                    turn_index=turn_index, type="message_delta", data={"content": delta.content}
                )
            )
        for tc_delta in delta.tool_calls:
            if reasoning_open:
                packets.append(Packet(turn_index=turn_index, type="reasoning_done"))
                reasoning_open = False
            _update_tool_call_with_delta(in_progress, tc_delta)

    if reasoning_open:
        packets.append(Packet(turn_index=turn_index, type="reasoning_done"))

    answer = "".join(answer_parts) or None
    reasoning = "".join(reasoning_parts) or None
    tool_calls = _extract_kickoffs(in_progress, turn_index) or None
    return (
        LlmStepResult(
            reasoning=reasoning,
            answer=answer,
            tool_calls=tool_calls,
            raw_answer=answer,
            finish_reason=finish_reason,
        ),
        packets,
    )


_CITATION_RE = re.compile(r"\[(\d+)\]")
_STRIP_RE = re.compile(r"\[?\[(\d+(?:\s*,\s*\d+)*)\]\]?")

# Numbers at/above this are never real citations (a search turn yields a
# handful of docs); typically years ([2024]) or versions. Left alone.
_NON_CITATION_FLOOR = 1000


def extract_citation_numbers(answer: str) -> list[int]:
    return [int(m.group(1)) for m in _CITATION_RE.finditer(answer or "")]


def strip_unknown_citations(
    answer: str, known: set[int], *, strip_all_under_floor: bool = False
) -> tuple[str, list[int]]:
    """Remove [N] markers with no backing document.

    Port of the Onyx DynamicCitationProcessor unknown-citation skip
    (HYPERLINK mode drops numbers missing from the mapping instead of
    emitting them). Code-fenced spans are left alone (Onyx in_code_block
    guard). Empty known set returns input unchanged: with no search
    context there is nothing to validate against.

    `strip_all_under_floor` escapes that rule for the case where the turn
    produced no document at all: every [N] below the floor is then
    unsupported, and leaving them points the reader at a source that does
    not exist.
    """
    if not answer:
        return answer, []
    if not known and not strip_all_under_floor:
        return answer, []
    removed: list[int] = []

    def _repl(m: re.Match) -> str:
        nums = [int(n) for n in m.group(1).split(",")]
        if strip_all_under_floor:
            kept = [n for n in nums if n >= _NON_CITATION_FLOOR]
        else:
            kept = [n for n in nums if n in known or n >= _NON_CITATION_FLOOR]
        removed.extend(
            n for n in nums if n not in known and n < _NON_CITATION_FLOOR
        )
        if not kept:
            return ""
        if len(kept) == len(nums):
            return m.group(0)
        return "[" + ", ".join(str(n) for n in kept) + "]"

    parts: list[str] = []
    pos = 0
    for m in _STRIP_RE.finditer(answer):
        if answer.count("```", 0, m.start()) % 2 == 1:
            continue
        parts.append(answer[pos : m.start()])
        parts.append(_repl(m))
        pos = m.end()
    parts.append(answer[pos:])
    return "".join(parts), sorted(set(removed))
