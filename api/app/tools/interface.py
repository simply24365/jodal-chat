"""Tool ABC. Port of Onyx tools/interface.py, minus emitter/DB."""

from __future__ import annotations

import abc
from typing import Any, Generic, TypeVar

from ..models import Packet, ToolResponse

TOverride = TypeVar("TOverride")


class Tool(abc.ABC, Generic[TOverride]):
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
