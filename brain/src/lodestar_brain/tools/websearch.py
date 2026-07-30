"""Web search behind a SearchProvider protocol. v1 backend: DuckDuckGo via
the `ddgs` package (keyless). Swap by passing a different provider to
make_search_tool()."""
from typing import Protocol

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[dict]: ...


class DdgsSearch:
    def search(self, query: str, max_results: int = 5) -> list[dict]:
        from ddgs import DDGS  # imported lazily so offline tests never touch it
        with DDGS() as ddgs:
            return [{'title': r.get('title', ''), 'url': r.get('href', ''),
                     'snippet': r.get('body', '')}
                    for r in ddgs.text(query, max_results=max_results)]


class WebSearchArgs(BaseModel):
    query: str
    max_results: int = Field(5, ge=1, le=10)


def make_search_tool(provider: SearchProvider) -> BaseTool:
    @tool('web_search', args_schema=WebSearchArgs)
    def web_search(query: str, max_results: int = 5) -> list[dict]:
        """Search the public web. Returns results with title, url, and snippet.
        Use for researching a question; cite urls in your reply."""
        return provider.search(query, max_results=max_results)

    return web_search
