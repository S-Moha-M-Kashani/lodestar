"""Which agent answers a turn — the seam, and the only place that chooses.

`BRAIN_AGENT` names an implementation. It follows the idiom every other backend
in this service follows: a named value, an unknown one raising at boot, and no
`auto` mode. **There is one agent.** Widget 2 is that agent, and it is the
default. `widget1` is not a name this seam accepts: the previous agent was
retired, and naming it fails the same way any other unknown value fails.

**Its own module, beside `llm.py` and `safety.py`, and not inside the agent's
package.** A selector living in the agent would die with a package it chooses
from, and one living in `server.py` would be the single seam in this service
whose choice sits in its caller rather than beside the thing being chosen.

**Not a registry.** One name, one branch, one raise. The one-entry
agent-builder registry this project had was deliberately removed in August 2026,
and a dict of factories for a single agent would be that mistake again.

**Why an operator switch and never a UI control.** The model picker chooses an
*engine*, which is a per-turn question the user owns. Which implementation is in
service is an operator question, and putting it in the browser would mean a
frontend change. With one agent the switch only refuses a wrong name.
"""
from __future__ import annotations

from .board.client import BoardClient
from .config import Settings
from .widget2 import Widget2Agent
from .widget2.tools import make_tools

#: The names `BRAIN_AGENT` accepts. Named in the error below, so an operator who
#: mistypes one is told what exists rather than left to read this file.
AGENTS = ('widget2',)


def make_agent(settings: Settings, *, max_steps: int, board: BoardClient):
    """Build the agent `settings.agent` names, or refuse to start.

    The capability set is decided inside the agent's own package.
    `make_tools(board)` is that set, assembled in one place that can be read
    end to end. Building it here would make the capabilities a fact about the
    composition root instead of a decision somebody took.
    """
    if settings.agent == 'widget2':
        return Widget2Agent(settings=settings, tools=make_tools(board),
                            max_steps=max_steps)
    raise ValueError(
        f'unknown BRAIN_AGENT {settings.agent!r}; expected '
        + ' or '.join(repr(name) for name in AGENTS))


__all__ = ['AGENTS', 'make_agent']
