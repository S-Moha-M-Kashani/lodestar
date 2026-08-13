"""The same tool, asked the same question twice in one turn, answered once.

A turn that reaches for three tools fetches `/api/state` three times: `list_cards`
reads the board, `find_related` reads it again to rebuild its index, `daily_recap`
reads it a third time. Nothing there changed between the three, and the model
often asks for the same thing twice besides — a second `find_related` with the
same text after the first answer did not settle the question.

So a tool result is remembered for as long as it cannot have gone stale, which
here is exactly one turn.

**Why one turn is the right scope, and not a guess.** The two tools that write
are `create_card` and `update_card`, and neither writes to the board: one files a
proposal and the other a suggested edit, both invisible until the *user* accepts
them. Nothing the agent can do inside a turn changes what `list_cards` returns.
The staleness window is therefore not "however long the cache lives" but "how
long this turn takes", and the only writer who could race it is the user, editing
their own board in another tab during the seconds an answer takes.

**And those two tools are excluded anyway.** Not because they would be stale — a
proposal is idempotent enough — but because a cached proposal would swallow the
second request: ask for the same card twice and the second call would return the
first proposal's id without filing anything, so the user would accept one card
having asked for two. A cache that silently discards a user's request is worse
than the fetch it saved.

The turn is identified by the id of the last human message, which is what makes
this work with or without a checkpointer: a resumed thread carries every previous
turn's messages, and the newest one it ends on is this turn's question.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage

# The confirmation gate, restated as a cache rule. It is the same pair as
# `server.PROPOSING_TOOLS` and a test asserts that it stays the same pair: these
# are the tools whose result is a thing the user is being asked about, and an
# answer that was really given to an earlier question is not one of those.
NEVER_CACHED = frozenset({'create_card', 'update_card'})

# Bounded, because a long-lived brain would otherwise hold every tool result of
# every conversation. Entries are keyed by turn, so ordinary use evicts itself;
# the bound is what happens when it does not.
MAX_ENTRIES = 64


def _turn_of(state: Any) -> str:
    """Which turn this call belongs to: the newest thing the user said.

    A message id rather than a counter — the graph renumbers nothing, and a
    counter would need somewhere to live in state that a checkpoint would then
    have to carry.
    """
    messages = (state or {}).get('messages', []) if isinstance(state, dict) else []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.id or '')
    return ''


def _key(request: Any) -> tuple | None:
    """What makes two calls the same call, or None for a call that is never one.

    The board is named rather than fingerprinted. Fingerprinting its *contents*
    would mean fetching the board to decide whether to avoid fetching the board,
    which is the cost this exists to remove; the turn is what bounds staleness
    instead, and the argument for that is in the module docstring.
    """
    call = request.tool_call or {}
    name = call.get('name', '')
    if not name or name in NEVER_CACHED:
        return None
    configurable = (getattr(request.runtime, 'config', None)
                    or {}).get('configurable', {})
    return (configurable.get('thread_id', ''),
            configurable.get('board_id', ''),
            _turn_of(request.state),
            name,
            # Sorted, so two calls that named the same arguments in a different
            # order are one call. `default=str` because a model can put anything
            # in there and a cache must not be the thing that raises.
            json.dumps(call.get('args') or {}, sort_keys=True, default=str))


def _reissued(message: ToolMessage, request: Any) -> ToolMessage:
    """The remembered answer, addressed to the call that is asking now.

    `tool_call_id` is how the graph pairs an answer with its request, so handing
    back the stored message unchanged would answer the first call twice and leave
    the second one hanging — a transcript the model reads as a tool that never
    replied. The id is cleared for `add_messages` to assign a fresh one.
    """
    return message.model_copy(update={'tool_call_id': request.tool_call['id'],
                                      'id': None})


class ToolResultCache(AgentMiddleware):
    """One turn's worth of tool answers, per agent.

    Per instance rather than per module: two agents have different tools, and a
    process-wide cache would answer one agent's call with another's result.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        super().__init__()
        self.max_entries = max_entries
        self._answers: OrderedDict[tuple, ToolMessage] = OrderedDict()

    def wrap_tool_call(self, request, handler):
        key = _key(request)
        if key is None:
            return handler(request)
        if (hit := self._hit(key)) is not None:
            return _reissued(hit, request)
        return self._remember(key, handler(request))

    async def awrap_tool_call(self, request, handler):
        # Defined explicitly: astream is the path the route takes, and the base
        # class raises rather than falling back, so a cache that existed only
        # synchronously would be a cache that never ran in production.
        key = _key(request)
        if key is None:
            return await handler(request)
        if (hit := self._hit(key)) is not None:
            return _reissued(hit, request)
        return self._remember(key, await handler(request))

    def _hit(self, key: tuple) -> ToolMessage | None:
        if key not in self._answers:
            return None
        self._answers.move_to_end(key)
        return self._answers[key]

    def _remember(self, key: tuple, message):
        # A Command changes the graph's state rather than returning a value, so
        # there is nothing to remember and replaying one would re-apply an edit.
        if isinstance(message, ToolMessage):
            self._answers[key] = message
            while len(self._answers) > self.max_entries:
                self._answers.popitem(last=False)
        return message


__all__ = ['MAX_ENTRIES', 'NEVER_CACHED', 'ToolResultCache']

"""Alternatives considered
========================

Why did you write your own tool cache?
--------------------------------------

Because the framework's looks like it exists and does not. `create_agent` takes a
`cache=` argument, forwards it to `.compile(cache=)`, and then sets a
`cache_policy` on none of its nodes — and LangGraph has no tool-level caching at
all, only node-level. So passing a cache is accepted, silently does nothing, and
reports nothing: the most expensive kind of API, one that looks like the feature
you wanted. Verified against langchain 1.3.14 / langgraph 1.2.10 by reading both,
after a run showed three identical board fetches with the parameter set.

**Why the obvious option fails.** The obvious option is `functools.lru_cache` on
the tool functions. It fails on the scope: a module-level cache has no idea which
turn, which thread or which board it is caching for, so `list_cards` would answer
tomorrow's question with today's board and there is nothing in the key that could
notice. Adding those to the key means reaching the run config from inside every
tool, which is precisely the per-tool bookkeeping middleware exists to remove —
and a tool added next year would forget it, the same argument the fence makes.

**Why not the framework.** The seam is the framework's: `wrap_tool_call` /
`awrap_tool_call` is where a result can be substituted, `ToolMessage` is the type,
and `Command` is the case that must not be. What the framework does not supply is
any notion of "how long is this answer good for", which on this board has a
precise answer — one turn, because the only two tools that could change the board
do not write to it. That sentence is the whole design and no library knows it.

**The libraries that would do it** (checked 2026-08-13):

- **`cachetools`** — `TTLCache`/`LRUCache` with the eviction policy written for
  you. The one to reach for if this ever needs more than an `OrderedDict`.
- **`langchain_core.caches`** (`InMemoryCache`, `SQLiteCache`) — real and in use
  elsewhere, but it caches *model* calls keyed on the prompt, not tool calls.
- **`joblib.Memory`** / **`diskcache`** — persistent memoisation across runs.
  Persistence is the opposite of what is wanted here: a board that changed while
  the brain was down would come back cached.
- **`aiocache`** — async-first, decorator-based, pluggable backends. Nice API,
  and every one of its backends is a service this board does not run.
- Greenfield, on someone else's budget: still this, because the interesting part
  is the key and not the storage.

**Why they were not adopted, and what would change it.** An `OrderedDict` with a
`move_to_end` is nine lines and no dependency, and the hard part of this module —
which calls are the same call, and which must never be — is not something any of
them would have decided. What would change it: a measured hit rate worth
persisting. If a trace shows most hits landing on `web_search` for queries
repeated across *turns* rather than within one, then the scope is wrong, the
right key is the query alone with a TTL, and `cachetools.TTLCache` is the tool —
at which point the board tools would have to be excluded, because they are the
ones the turn scope was protecting.
"""
