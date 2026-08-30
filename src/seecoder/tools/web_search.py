"""A bounded local web search tool for the agent.

The search is performed by the agent process itself over ordinary HTTP (it is not a hosted
code/file tool). The fetcher is injectable so the tool is fully testable offline; the live
path degrades to a structured error when the network or parser is unavailable.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from seecoder.types import ToolResult

DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
_MAX_RESULTS = 8
_MAX_HTML_BYTES = 400_000
_TIMEOUT_S = 8
_RESULT_LINK_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_SNIPPET_RE = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def _default_fetcher(search_url: str = DEFAULT_SEARCH_URL, timeout_s: int = _TIMEOUT_S) -> Callable[[str], str]:
    def fetch(query: str) -> str:
        url = search_url.format(query=urllib.parse.quote(query))
        request = urllib.request.Request(url, headers={"User-Agent": "seecoder-agent/0.1"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            payload = response.read(_MAX_HTML_BYTES)
        return payload.decode("utf-8", errors="replace")

    return fetch


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _normalize_url(raw: str) -> str:
    if raw.startswith("//"):
        return "https:" + raw
    return raw


def _parse_results(page: str, maximum: int) -> list[dict[str, str]]:
    links = _RESULT_LINK_RE.findall(page)
    snippets = _SNIPPET_RE.findall(page)
    results: list[dict[str, str]] = []
    for index, (href, title) in enumerate(links[:maximum]):
        results.append({
            "title": _strip_tags(title)[:200],
            "url": _normalize_url(href)[:500],
            "snippet": _strip_tags(snippets[index])[:400] if index < len(snippets) else "",
        })
    return results


class WebSearchTool:
    capability = "read"
    name = "web_search"
    description = (
        "Search the web for a query and return bounded results (title, url, snippet). "
        "Use it to look up documentation or current information that is not in the workspace."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The web search query"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, fetcher: Callable[[str], str] | None = None, *, timeout_s: int = _TIMEOUT_S) -> None:
        self.fetcher = fetcher if fetcher is not None else _default_fetcher(timeout_s=timeout_s)

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("'query' must be a non-empty string")
        max_results = arguments.get("max_results", 5)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= _MAX_RESULTS:
            raise ValueError(f"'max_results' must be an integer between 1 and {_MAX_RESULTS}")
        try:
            page = self.fetcher(query.strip())
        except Exception as error:
            return ToolResult.failure("WebSearchUnavailable", f"Web search failed: {type(error).__name__}")
        results = _parse_results(page, max_results)
        return ToolResult.success({"query": query.strip(), "results": results}, meta={"truncated": len(results) >= max_results})
