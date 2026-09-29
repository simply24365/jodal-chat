"""web_search tool. Port of Onyx WebSearchTool.run, minus DB/citations infra."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import config
from ..models import Packet, SearchDoc, ToolResponse
from ..utils import setup_logger
from .interface import Tool, ToolCallException
from .providers import DEFAULT_MAX_RESULTS, JinaClient, TavilyClient, WebSearchResult
from ..prompts import OPEN_URL_REMINDER

logger = setup_logger("custom_chat.web_search_tool")

QUERIES_FIELD = "queries"


def build_provider() -> Any:
    """Env-driven provider. Raises RuntimeError when unconfigured."""
    if config.WEB_SEARCH_PROVIDER == "tavily":
        if not config.TAVILY_API_KEY:
            raise RuntimeError("TAVILY_API_KEY is not set")
        return TavilyClient(api_key=config.TAVILY_API_KEY)
    if not config.JINA_API_KEY:
        raise RuntimeError("JINA_API_KEY is not set")
    return JinaClient(api_key=config.JINA_API_KEY, base_url=config.JINA_SEARCH_URL)


class WebSearchTool(Tool[dict]):
    NAME = "web_search"
    DESCRIPTION = "Search the web for information."
    DISPLAY_NAME = "Web Search"
    # 검색 직후에는 원문을 읽어야 근거가 생긴다 (Onyx select_reminder_text 의
    # 일반화: 루프는 툴 이름을 모르고 followup_reminder 속성만 읽는다).
    followup_reminder = OPEN_URL_REMINDER

    def __init__(self, tool_id: int = 1, provider: Any | None = None) -> None:
        self._id = tool_id
        self._provider = provider or build_provider()

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    "Search the web for information. Returns a list of search "
                    "results with titles, URLs, and snippets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        QUERIES_FIELD: {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "One or more queries to look up on the web.",
                        },
                    },
                    "required": [QUERIES_FIELD],
                },
            },
        }

    def emit_start(self, turn_index: int, tab_index: int) -> Packet:
        return Packet(
            turn_index=turn_index, tab_index=tab_index, type="search_tool_start"
        )

    def run(
        self,
        turn_index: int,
        tab_index: int,
        override_kwargs: dict,
        **llm_kwargs: Any,
    ) -> tuple[ToolResponse, list[Packet]]:
        packets: list[Packet] = []
        raw = llm_kwargs.get(QUERIES_FIELD)
        queries = _normalize_queries(raw)
        if not queries:
            raise ToolCallException(
                message="No valid web search queries",
                llm_facing_message=(
                    "No valid web search queries were provided (empty or "
                    "whitespace-only). Please provide a real search query."
                ),
            )
        packets.append(
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="search_tool_queries_delta",
                data={"queries": queries},
            )
        )

        with ThreadPoolExecutor(max_workers=min(len(queries), 5)) as pool:
            per_query = list(pool.map(self._safe_search, queries))

        valid: list[list[WebSearchResult]] = []
        failed: dict[str, str] = {}
        for query, (results, error) in zip(queries, per_query):
            if error is not None:
                failed[query] = error
            elif results is not None:
                valid.append(results)
        if failed and valid:
            logger.warning("Partial web search failure: %s", json.dumps(failed))
        if not valid:
            raise ToolCallException(
                message=f"All web search queries failed: {failed}",
                llm_facing_message=f"All web search queries failed: {json.dumps(failed)}",
            )

        # Round-robin interleave (port of Onyx), then cap.
        merged: list[WebSearchResult] = []
        seen: set[tuple[str, str]] = set()
        indices = [0] * len(valid)
        while len(merged) < DEFAULT_MAX_RESULTS:
            added = False
            for idx, results in enumerate(valid):
                if len(merged) >= DEFAULT_MAX_RESULTS:
                    break
                if indices[idx] < len(results):
                    r = results[indices[idx]]
                    key = (r.title, r.link)
                    if key not in seen:
                        seen.add(key)
                        merged.append(r)
                        added = True
                    indices[idx] += 1
            if not added:
                break
        if not merged:
            raise ToolCallException(
                message="Web search returned no results",
                llm_facing_message=(
                    "Web search completed but found no results. "
                    "Try rephrasing or different search terms."
                ),
            )

        start_num = int((override_kwargs or {}).get("starting_citation_num", 1))
        docs: list[SearchDoc] = []
        payload_results: list[dict] = []
        citation_mapping: dict[int, str] = {}
        for i, r in enumerate(merged):
            num = start_num + i
            doc_id = f"WEB_SEARCH_DOC_{r.link}"
            docs.append(
                SearchDoc(
                    document_id=doc_id, title=r.title, link=r.link, snippet=r.snippet
                )
            )
            citation_mapping[num] = doc_id
            payload_results.append(
                {"document": num, "title": r.title, "url": r.link, "content": r.snippet}
            )
            packets.append(
                Packet(
                    turn_index=turn_index,
                    tab_index=tab_index,
                    type="citation_info",
                    data={
                        "citation_number": num,
                        "document_id": doc_id,
                        "title": r.title,
                        "link": r.link,
                    },
                )
            )
        packets.append(
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="search_tool_documents_delta",
                data={"documents": [d.model_dump() for d in docs]},
            )
        )
        llm_str = json.dumps({"results": payload_results}, ensure_ascii=False)
        response = ToolResponse(
            rich_response={
                "search_docs": [d.model_dump() for d in docs],
                "citation_mapping": citation_mapping,
            },
            llm_facing_response=llm_str,
        )
        return response, packets

    def _safe_search(
        self, query: str
    ) -> tuple[list[WebSearchResult] | None, str | None]:
        try:
            return list(self._provider.search(query)), None
        except Exception as e:
            logger.warning("Web search query %r failed: %s", query, e)
            return None, str(e)


def _normalize_queries(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for q in raw:
        if isinstance(q, str) and q.strip():
            out.append(" ".join(q.strip().split()))
    return out
