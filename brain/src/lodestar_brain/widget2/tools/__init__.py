"""Widget 2's tools — the one place that knows which ones exist.

Five modules and three tools. `find_cards` answers a question about the board,
`create_card` proposes a new card and `update_card` suggests a change to one;
`fields.py`, `render.py` and `spans.py` are the pure leaves the three share, so
that a card is described one way, a field is spelled one way and a span of time
is measured one way.

**Assembled here and nowhere else, which is what makes the list a decision.**
`make_tools` is the whole answer to "what can this agent do", and `agents.py`
calls it — so an operator reads the capability set off `GET /health`, a reviewer
reads it off this function, and neither has to trust that the two agree. Widget
1's list is assembled in `server.py` across five factories, which is how it
became a list nobody had read end to end.

**What is deliberately absent, and absent rather than gated:**

- **No delete and no purge.** The board's durability promise is that a card is
  destroyed only by a person, twice, through the Trash. A tool that could
  delete one and merely happened to be gated would put that promise in a
  policy; not having the tool puts it in the shape of the surface.
- **No habit completion.** A record a model can write into is not a record.
- **None of Widget 1's other five** — `find_related`, `recall_chat`,
  `web_search`, `remember_fact`, `daily_recap`. The owner saw all eight and
  took none: *"I dont want any of the tools of widget 1 for now."* "For now" is
  his word, so this is a deferral, and Widget 1 keeps every one of them.

**Both writers wait for him.** `create_card` files a proposal that is invisible
until he accepts it; `update_card` files a suggestion that applies only when he
opens the card and saves. So this list contains no board write at all, and
invariant 1 stays enforced by the absence of a route rather than by anyone
remembering a rule.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool

from ...board.client import BoardClient
from .read import make_find_cards
from .write import make_write_tools


def make_tools(board: BoardClient) -> list[BaseTool]:
    """Every tool Widget 2 holds, given the board it may read.

    One argument, because all three tools reach the same board over the same
    client and none of them needs anything else — no index, no store, no search
    provider, no model of its own. That is the shape of this agent's capability
    set rather than a simplification of it: a second argument here would mean a
    tool had grown a dependency somebody should have argued for.
    """
    return [make_find_cards(board), *make_write_tools(board)]


__all__ = ['make_tools']
