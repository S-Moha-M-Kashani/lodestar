"""Web search behind a SearchProvider protocol. v1 backend: DuckDuckGo via
the `ddgs` package (keyless). Swap by passing a different provider to
make_search_tool()."""
from typing import Protocol

from .base import Tool


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[dict]: ...


class DdgsSearch:
    def search(self, query: str, max_results: int = 5) -> list[dict]:
        from ddgs import DDGS  # imported lazily so offline tests never touch it
        with DDGS() as ddgs:
            return [{'title': r.get('title', ''), 'url': r.get('href', ''),
                     'snippet': r.get('body', '')}
                    for r in ddgs.text(query, max_results=max_results)]


def make_search_tool(provider: SearchProvider) -> Tool:
    def web_search(query: str, max_results: int = 5) -> list[dict]:
        return provider.search(query, max_results=max_results)

    return Tool(
        'web_search',
        'Search the public web. Returns results with title, url, and snippet. '
        'Use for researching a question; cite urls in your reply.',
        {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 10}},
         'required': ['query']},
        web_search)
