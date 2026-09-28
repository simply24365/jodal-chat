"""open_url tool. Crawl-only port of Onyx OpenURLTool (no DB/index).

Onyx resolves URLs against indexed documents first and crawls as fallback.
This service has no document index, so every URL is fetched live over HTTP
and converted to text with the stdlib HTML parser (no extra deps).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any
import json

import requests

from .. import config
from ..models import Packet, SearchDoc, ToolResponse
from ..utils import setup_logger
from .interface import Tool, ToolCallException

logger = setup_logger("custom_chat.open_url_tool")

URLS_FIELD = "urls"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")
_MAX_BYTES = 2_000_000
# Min chars for a page to count as useful (Onyx MIN_CONTENT_CHARS). Pages with
# *some* text below this still go to the model; only near-empty pages fail.
_MIN_CONTENT_CHARS = 200
_EMPTY_PAGE_CHARS = 50
_SNIPPET_CHARS = 1000


class _TextExtractor(HTMLParser):
    """Stdlib HTML -> text. Drops script/style, keeps title, block newlines."""

    _BLOCKS = {
        "p", "div", "section", "article", "header", "footer", "main",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br",
        "blockquote", "pre", "hr",
    }
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCKS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCKS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, collapsed text) from an HTML document."""
    parser = _TextExtractor()
    parser.feed(html)
    title = " ".join("".join(parser.title_parts).split())
    lines = (" ".join(chunk.split()) for chunk in "".join(parser.parts).split("\n"))
    text = "\n".join(line for line in lines if line)
    return title, text


class OpenURLTool(Tool[dict]):
    NAME = "open_url"
    DESCRIPTION = "Open and read the content of one or more URLs."
    DISPLAY_NAME = "Open URL"

    def __init__(self, tool_id: int = 2) -> None:
        self._id = tool_id

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
                    "Open and read the content of one or more URLs. "
                    "Use after web_search to read the most promising pages, "
                    "or when the user gives a URL directly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        URLS_FIELD: {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of URLs to open and read. "
                                "Returns the text content of each page."
                            ),
                        },
                    },
                    "required": [URLS_FIELD],
                },
            },
        }

    def emit_start(self, turn_index: int, tab_index: int) -> Packet:
        return Packet(
            turn_index=turn_index, tab_index=tab_index, type="open_url_start"
        )

    def run(
        self,
        turn_index: int,
        tab_index: int,
        override_kwargs: dict,
        **llm_kwargs: Any,
    ) -> tuple[ToolResponse, list[Packet]]:
        packets: list[Packet] = []
        urls, rejected = _normalize_urls(llm_kwargs.get(URLS_FIELD))
        if len(urls) > config.OPEN_URL_MAX_URLS:
            logger.warning(
                "open_url received %d URLs, capping at %d",
                len(urls),
                config.OPEN_URL_MAX_URLS,
            )
            urls = urls[: config.OPEN_URL_MAX_URLS]
        if not urls:
            raise ToolCallException(
                message=f"Missing required '{URLS_FIELD}' parameter in open_url tool call",
                llm_facing_message=(
                    f"The open_url tool requires a '{URLS_FIELD}' parameter "
                    "containing an array of URLs. Please provide "
                    'like: {"urls": ["https://example.com"]}'
                ),
            )
        packets.append(
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="open_url_urls_delta",
                data={"urls": urls},
            )
        )

        with ThreadPoolExecutor(max_workers=min(len(urls), 5)) as pool:
            fetched = list(pool.map(self._safe_fetch, urls))

        pages: list[tuple[str, str, str]] = []  # (url, title, text)
        failed: dict[str, str] = dict(rejected)
        for url, (title, text, error) in zip(urls, fetched):
            if error is not None:
                failed[url] = error
            elif text is not None:
                pages.append((url, title, text))
        if failed and pages:
            logger.warning("Partial open_url failure: %s", failed)
        if not pages:
            raise ToolCallException(
                message=f"All open_url fetches failed: {failed}",
                llm_facing_message=(
                    "Could not read any of the requested pages: "
                    + "; ".join(f"{u} ({e})" for u, e in failed.items())
                ),
            )

        start_num = int((override_kwargs or {}).get("starting_citation_num", 1))
        docs: list[SearchDoc] = []
        payload_results: list[dict] = []
        citation_mapping: dict[int, str] = {}
        total_chars = 0
        for url, title, text in pages:
            if total_chars + _MIN_CONTENT_CHARS > config.OPEN_URL_MAX_CHARS_TOTAL:
                break
            budget = config.OPEN_URL_MAX_CHARS_TOTAL - total_chars
            content = text[: min(config.OPEN_URL_MAX_CHARS_PER_PAGE, budget)]
            if len(content.strip()) < _EMPTY_PAGE_CHARS:
                failed[url] = "page had no readable text"
                continue
            num = start_num + len(docs)
            doc_id = f"OPEN_URL_DOC_{url}"
            docs.append(
                SearchDoc(
                    document_id=doc_id,
                    title=title or url,
                    link=url,
                    snippet=content[:_SNIPPET_CHARS],
                )
            )
            citation_mapping[num] = doc_id
            payload_results.append(
                {"document": num, "title": title or url, "url": url, "content": content}
            )
            total_chars += len(content)
            packets.append(
                Packet(
                    turn_index=turn_index,
                    tab_index=tab_index,
                    type="citation_info",
                    data={
                        "citation_number": num,
                        "document_id": doc_id,
                        "title": title or url,
                        "link": url,
                    },
                )
            )
        if not docs:
            raise ToolCallException(
                message=f"open_url produced no readable content: {failed}",
                llm_facing_message=(
                    "None of the requested pages had readable content: "
                    + "; ".join(f"{u} ({e})" for u, e in failed.items())
                ),
            )
        packets.append(
            Packet(
                turn_index=turn_index,
                tab_index=tab_index,
                type="open_url_documents_delta",
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

    def _safe_fetch(self, url: str) -> tuple[str, str | None, str | None]:
        """Return (title, text, error). Never raises."""
        try:
            resp = requests.get(
                url,
                timeout=config.OPEN_URL_TIMEOUT_SECONDS,
                headers={"User-Agent": "custom-chat-webservice/0.1"},
                stream=True,
            )
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if any(
                content_type.startswith(prefix)
                for prefix in ("image/", "audio/", "video/")
            ):
                return "", None, f"unsupported content type: {content_type}"
            body = b"".join(resp.iter_content(chunk_size=65536))[:_MAX_BYTES]
            encoding = resp.encoding or "utf-8"
            raw = body.decode(encoding, errors="replace")
            if "html" in content_type:
                title, text = html_to_text(raw)
            elif content_type.startswith("text/") or not content_type:
                title, text = "", raw.strip()
            else:
                return "", None, f"unsupported content type: {content_type}"
            if len(text.strip()) < _EMPTY_PAGE_CHARS:
                return title, None, "page had no readable text"
            return title, text, None
        except Exception as e:
            return "", None, str(e)


def _normalize_urls(raw: Any) -> tuple[list[str], dict[str, str]]:
    """Split raw urls arg into (fetchable urls, rejected url -> reason)."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return [], {}
    urls: list[str] = []
    rejected: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw:
        url = item.strip() if isinstance(item, str) else ""
        if not url or url in seen:
            continue
        seen.add(url)
        lowered = url.lower()
        if not lowered.startswith(("http://", "https://")):
            rejected[url or "(empty)"] = "only http(s) URLs can be opened"
            continue
        if lowered.split("?")[0].split("#")[0].endswith(_IMAGE_EXTS):
            rejected[url] = "image files cannot be opened"
            continue
        urls.append(url)
    return urls, rejected
