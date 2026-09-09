"""Which agent answers a turn — the seam, and the only place that chooses.

`BRAIN_AGENT` names an implementation. It follows the idiom every other backend
in this service follows: a named value, an unknown one raising at boot, and no
`auto` mode. The incumbent is the default, so an operator who sets nothing is
served exactly what they were served before this module existed.

**Its own module, beside `llm.py` and `safety.py`, and not inside either agent's
package.** A selector living in `agent/` would die with the package it chooses
from, and one living in `server.py` would be the single seam in this service
whose choice sits in its caller rather than beside the thing being chosen.

**Not a registry.** Two names, two branches, one raise. The one-entry
agent-builder registry this project had was deliberately removed in August 2026,
and a dict of factories to manage a two-agent overlap that ends with one of them
deleted would be that mistake made again with a better excuse. When Widget 1
goes, this file becomes one line or nothing at all.

**Why an operator switch and never a UI control.** The model picker chooses an
*engine*, which is a per-turn question the user owns. Which implementation is in
service is an operator question for the length of a migration, and putting it in
the browser would mean a frontend change in a ladder whose whole premise is that
the frontend does not move.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool

from .agent import LodestarAgent
from .board.client import BoardClient
from .config import Settings
from .widget2 import Widget2Agent
from .widget2.tools import make_tools

#: The names `BRAIN_AGENT` accepts. Named in the error below, so an operator who
#: mistypes one is told what exists rather than left to read this file.
AGENTS = ('widget1', 'widget2')


def make_agent(settings: Settings, *, tools: list[BaseTool],
               max_steps: int, board: BoardClient):
    """Build the agent `settings.agent` names, or refuse to start.

    **Each agent's capabilities are decided inside its own package, and that is
    the point of the ladder rather than a detail of this function.** `tools` is
    Widget 1's list, assembled in `server.py` across five factories; Widget 2's
    is `make_tools(board)`, assembled in one place that can be read end to end.
    Handing Widget 2 Widget 1's list would make its capability set a fact about
    the composition root instead of a decision somebody took — which is exactly
    how a capability arrives without a review.

    So this function passes each what it takes: `board`, because Widget 2's
    tools all reach the same board over the same client and need nothing else,
    and `max_steps`, which both now spend — it means tool *rounds*, and it was
    correctly refused while Widget 2 had no tools to spend it on.
    """
    if settings.agent == 'widget1':
        return LodestarAgent(settings=settings, tools=tools,
                             max_steps=max_steps)
    if settings.agent == 'widget2':
        return Widget2Agent(settings=settings, tools=make_tools(board),
                            max_steps=max_steps)
    raise ValueError(
        f'unknown BRAIN_AGENT {settings.agent!r}; expected '
        + ' or '.join(repr(name) for name in AGENTS))


__all__ = ['AGENTS', 'make_agent']
