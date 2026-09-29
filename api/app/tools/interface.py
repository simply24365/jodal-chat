"""Tool ABC. Port of Onyx tools/interface.py, minus emitter/DB."""

from __future__ import annotations

import abc
from typing import Any, Generic, TypeVar

from ..models import Packet, ToolResponse

TOverride = TypeVar("TOverride")


class Tool(abc.ABC, Generic[TOverride]):
    # --- 루프가 읽는 툴 정책 메타데이터 (기본값: 정책 없음) ---
    # 이 툴이 이번 턴에 누적 몇 번까지 호출될 수 있는지. None 은 무제한.
    # 초과하면 루프가 이 툴의 exhausted_reminder 를 히스토리에 추가한다.
    max_calls_per_turn: int | None = None
    # 호출 한도 초과 시 모델에게 보여줄 안내문. max_calls_per_turn 이 있을 때만 의미가 있다.
    exhausted_reminder: str | None = None
    # 이 툴이 성공한 직후 모델을 다음 단계로 유도하는 문장(예: web_search → open_url).
    # None 이면 후속 유도 없음. Onyx select_reminder_text 의 일반화.
    followup_reminder: str | None = None

    @property
    @abc.abstractmethod
    def id(self) -> int:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def description(self) -> str:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def tool_definition(self) -> dict:
        raise NotImplementedError

    def emit_start(self, turn_index: int, tab_index: int) -> Packet | None:
        return None

    @abc.abstractmethod
    def run(
        self,
        turn_index: int,
        tab_index: int,
        override_kwargs: TOverride,
        **llm_kwargs: Any,
    ) -> tuple[ToolResponse, list[Packet]]:
        raise NotImplementedError


class ToolCallException(Exception):
    """Expected tool error; llm_facing_message goes back to the model."""

    def __init__(self, message: str, llm_facing_message: str) -> None:
        super().__init__(message)
        self.llm_facing_message = llm_facing_message
