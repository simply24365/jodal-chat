"""Web search provider clients. Jina verified live; Tavily ported from Onyx."""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime

import requests
from pydantic import BaseModel, field_validator

from ..utils import setup_logger

logger = setup_logger("custom_chat.web_search")

DEFAULT_MAX_RESULTS = 10


class WebSearchResult(BaseModel):
    title: str
    link: str
    snippet: str
    published_date: datetime | None = None

    @field_validator("link")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


class WebSearchProvider(abc.ABC):
    @abc.abstractmethod
    def search(self, query: str) -> Sequence[WebSearchResult]:
        raise NotImplementedError


class JinaClient(WebSearchProvider):
    """POST https://s.jina.ai/ {q, num} -> data[{title, url, description}]."""

    def __init__(
        self, api_key: str, base_url: str = "https://s.jina.ai/", num: int = 10
    ) -> None:
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/"
        self._num = max(1, min(num, 20))

    def search(self, query: str) -> Sequence[WebSearchResult]:
        resp = requests.post(
            self._url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"q": query, "num": self._num},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data") or []
        results: list[WebSearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            link = (item.get("url") or "").strip()
            if not link:
                continue
            title = (item.get("title") or "").strip()
            snippet = (item.get("description") or item.get("content") or "").strip()
            if not title and not snippet:
                continue
            results.append(
                WebSearchResult(
                    title=title or link,
                    link=link,
                    # Short snippets: tiny TPM tiers resend full history every
                    # cycle, so payload size compounds per turn.
                    snippet=snippet[:1000],
                )
            )
        return results[: self._num]


class TavilyClient(WebSearchProvider):
    """POST https://api.tavily.com/search (port of Onyx tavily_client)."""

    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, num_results: int = 10) -> None:
        self._api_key = api_key
        self._num = max(1, min(num_results, 20))

    def search(self, query: str) -> Sequence[WebSearchResult]:
        resp = requests.post(
            self.URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "max_results": self._num,
                "search_depth": "basic",
                "topic": "general",
            },
            timeout=30,
        )
        resp.raise_for_status()
        results: list[WebSearchResult] = []
        for item in resp.json().get("results") or []:
            if not isinstance(item, dict):
                continue
            link = (item.get("url") or "").strip()
            if not link:
                continue
            title = (item.get("title") or "").strip()
            snippet = (item.get("content") or "").strip()
            if not title and not snippet:
                continue
            results.append(
                WebSearchResult(
                    title=title or link, link=link, snippet=snippet[:1000]
                )
            )
        return results[: self._num]
