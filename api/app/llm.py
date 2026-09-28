"""Thin LLM wrapper. Mirrors Onyx LitellmLLM.stream, minus DB/tracing.

xkiro/agnes는 OpenAI SDK 직접 호출 (직결, shim 미경유).
groq/gemini는 litellm 경유. tool_calls는 표준 delta로 정규화.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import litellm
from openai import OpenAI

from . import config
from .models import Delta, DeltaToolCall, FunctionCall, ModelResponseStream, StreamingChoice
from .utils import setup_logger

logger = setup_logger("custom_chat.llm")

litellm.drop_params = True
litellm.telemetry = False

XKIRO_DIRECT_BASE = "https://api.xkiro.com/v1"
AGNES_DIRECT_BASE = "https://apihub.agnes-ai.com/v1"
# litellm을 타지 않고 OpenAI SDK로 직결 호출하는 provider들
DIRECT_PROVIDERS = ("xkiro", "agnes")

class _Endpoint:
    def __init__(self, provider: str, model: str, api_key: str | None,
                 api_base: str | None) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.api_base = api_base

    @property
    def litellm_model(self) -> str:
        # "<provider>/<model>" routes via that provider; a model id that
        # already carries a vendor prefix (e.g. "openai/gpt-oss-120b" on
        # groq) still needs the provider prefix: "groq/openai/gpt-oss-120b".
        if self.provider in ("openai",):
            return self.model
        if self.model.startswith(self.provider + "/"):
            return self.model
        return f"{self.provider}/{self.model}"

    def kwargs(self, temperature: float) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"temperature": temperature}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        return kwargs


def _chain() -> list[_Endpoint]:
    """xkiro → agnes → groq → gemini (키 있는 것만, LLM_CHAIN 순서).
    xkiro/agnes는 OpenAI 호환 직결이라 provider 태그만 쓰고 litellm을 타지 않는다."""
    eps: list[_Endpoint] = []
    if config.XKIRO_API_KEY:
        eps.append(_Endpoint("xkiro", config.XKIRO_MODEL, config.XKIRO_API_KEY, XKIRO_DIRECT_BASE))
    if config.AGNES_API_KEY:
        eps.append(_Endpoint("agnes", config.AGNES_MODEL, config.AGNES_API_KEY, AGNES_DIRECT_BASE))
    if config.GROQ_API_KEY:
        eps.append(_Endpoint("groq", config.GROQ_MODEL, config.GROQ_API_KEY, None))
    if config.GEMINI_API_KEY:
        eps.append(_Endpoint("gemini", config.GEMINI_MODEL, config.GEMINI_API_KEY, None))
    order = [p.strip() for p in (config.LLM_CHAIN or "").split(",") if p.strip()]
    if order:
        rank = {name: i for i, name in enumerate(order)}
        eps.sort(key=lambda e: rank.get(e.provider, 99))
    return eps


class LLM:
    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.temperature = config.LLM_TEMPERATURE if temperature is None else temperature
        if provider is not None or model is not None or api_key is not None or api_base is not None:
            # 명시 지정 시 단일 endpoint (기존 동작 유지)
            self._endpoints = [_Endpoint(
                provider or config.LLM_PROVIDER,
                model or config.LLM_MODEL,
                config.LLM_API_KEY if api_key is None else api_key,
                config.LLM_API_BASE if api_base is None else api_base,
            )]
        else:
            self._endpoints = _chain()

    @property
    def litellm_model(self) -> str:
        return self._endpoints[0].litellm_model if self._endpoints else config.LLM_MODEL

    def _kwargs(self) -> dict[str, Any]:
        if self._endpoints:
            return self._endpoints[0].kwargs(self.temperature)
        return {"temperature": self.temperature}

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[ModelResponseStream]:
        if not self._endpoints:
            raise RuntimeError("LLM 키 없음 (XKIRO_API_KEY/AGNES_API_KEY/GROQ_API_KEY/GEMINI_API_KEY)")
        last_err: Exception | None = None
        for ep in self._endpoints:
            if ep.provider in DIRECT_PROVIDERS:
                # OpenAI 호환 직결 (shim 미경유): OpenAI SDK stream → 표준 delta.
                try:
                    yield from self._direct_stream(ep, messages, tools, tool_choice, max_tokens)
                    return
                except Exception as e:
                    last_err = e
                    logger.warning("llm %s 실패 → 다음 후보: %s", ep.provider, e)
                    print(f"[llm-fallback] {ep.provider} FAIL: {str(e)[:150]}", flush=True)
                    continue
            try:
                chunks = litellm.completion(**{
                    "model": ep.litellm_model,
                    "messages": messages,
                    "stream": True,
                    **ep.kwargs(self.temperature),
                    **({"tools": tools, "tool_choice": tool_choice or "auto"} if tools else {}),
                    **({"max_tokens": max_tokens} if max_tokens else {}),
                })
                it = iter(chunks)
                first = next(it)
                yield _parse_chunk(first)
                for raw in it:
                    yield _parse_chunk(raw)
                return
            except Exception as e:
                last_err = e
                logger.warning("llm %s 실패 → 다음 후보: %s", ep.litellm_model, e)
                print(f"[llm-fallback] {ep.litellm_model} FAIL: {str(e)[:150]}", flush=True)
                continue
        logger.exception("전 endpoint 실패")
        assert last_err is not None
        raise last_err

    def _direct_stream(
        self,
        ep: _Endpoint,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        max_tokens: int | None,
    ) -> Iterator[ModelResponseStream]:
        client = OpenAI(base_url=ep.api_base, api_key=ep.api_key)
        kwargs: dict[str, Any] = {
            "model": ep.model,
            "messages": messages,  # type: ignore[arg-type]
            "temperature": self.temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools  # type: ignore[assignment]
            kwargs["tool_choice"] = tool_choice or "auto"  # type: ignore[assignment]
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        # OpenAI SDK stream chunk → slim delta (tool_calls 포함)
        for raw in client.chat.completions.create(**kwargs):  # type: ignore[call-overload]
            try:
                data = raw.model_dump()
            except Exception:
                continue
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            if choice.get("finish_reason"):
                yield ModelResponseStream(
                    choice=StreamingChoice(
                        finish_reason=str(choice["finish_reason"]),
                        delta=Delta(),
                    )
                )
                continue
            delta = choice.get("delta") or {}
            tcs: list[DeltaToolCall] = []
            for tc in delta.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tcs.append(DeltaToolCall(
                    id=tc.get("id"), index=tc.get("index", 0),
                    function=FunctionCall(name=fn.get("name"), arguments=fn.get("arguments")),
                ))
            if not delta.get("content") and not delta.get("reasoning_content") and not tcs:
                continue
            yield ModelResponseStream(
                choice=StreamingChoice(
                    delta=Delta(
                        content=delta.get("content"),
                        reasoning_content=delta.get("reasoning_content"),
                        tool_calls=tcs,
                    )
                )
            )


def _parse_chunk(raw: Any) -> ModelResponseStream:
    """Normalize a litellm chunk to our slim delta shape."""
    try:
        data = raw.model_dump()
    except Exception:
        data = raw if isinstance(raw, dict) else {}
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    delta = choice.get("delta") or {}
    tool_calls: list[DeltaToolCall] = []
    for tc in delta.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_calls.append(
            DeltaToolCall(
                id=tc.get("id"),
                index=tc.get("index", 0),
                function=FunctionCall(
                    name=fn.get("name"), arguments=fn.get("arguments")
                ),
            )
        )
    return ModelResponseStream(
        choice=StreamingChoice(
            finish_reason=choice.get("finish_reason"),
            delta=Delta(
                content=delta.get("content"),
                # Groq (gpt-oss) emits `reasoning`; litellm normalizes to
                # `reasoning_content` only sometimes — accept both.
                reasoning_content=delta.get("reasoning_content") or delta.get("reasoning"),
                tool_calls=tool_calls,
            ),
        )
    )
