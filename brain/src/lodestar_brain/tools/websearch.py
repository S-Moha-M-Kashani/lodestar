"""Web search behind a SearchProvider protocol. v1 backend: DuckDuckGo via
the `ddgs` package (keyless). Swap by passing a different provider to
make_search_tool().

An optional `UrlSafety` (see `safety.py`) decides what the model is allowed to
see: unsafe results are dropped and the safe ones stand as the alternative, and a
query that names one specific unsafe site gets a verdict instead of a search.
"""
from typing import Protocol

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..safety import NOT_SAFE, host_of, urls_in


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


def _refusal(url: str, reason: str) -> list[dict]:
    """What the model gets instead of results. A row rather than a raised error:
    the model has to be able to tell the user *which* site and *why*, and a tool
    error is rendered as a failure of the assistant rather than a finding."""
    return [{'unsafe': True, 'url': url,
             'note': f'{NOT_SAFE} — {reason}' if reason else NOT_SAFE}]


def make_search_tool(provider: SearchProvider, safety=None) -> BaseTool:
    """`safety` is optional so the eval harness, which have no
    opinion about link reputation, keep the one-argument form."""

    @tool('web_search', args_schema=WebSearchArgs)
    def web_search(query: str, max_results: int = 5) -> list[dict]:
        """Search the public web. Returns results with title, url, and snippet.
        Use for researching a question; cite urls in your reply.

        A result whose destination fails a safety check is not returned. If the
        query names one specific unsafe site, the answer is a note saying so —
        report that to the user rather than searching for a substitute silently.
        """
        if safety is not None:
            # A question *about* a site is answered about that site. Checked
            # before searching, so insisting on a known-bad destination costs no
            # request to it and no list of substitutes the user did not ask for.
            for named in urls_in(query):
                verdict = safety.check(named)
                if not verdict.safe:
                    return _refusal(named, verdict.reason)

        results = provider.search(query, max_results=max_results)
        if safety is None:
            return results

        kept, dropped = [], []
        for row in results:
            verdict = safety.check(row.get('url', ''))
            (kept if verdict.safe else dropped).append(row)
        if kept or not dropped:
            return kept
        # Everything was dropped. An empty list would read as "nothing is written
        # about this", which is a different and misleading fact.
        hosts = ', '.join(sorted({host_of(r.get('url', '')) for r in dropped}))
        return _refusal(hosts, f'every result led somewhere unsafe ({hosts})')

    return web_search
