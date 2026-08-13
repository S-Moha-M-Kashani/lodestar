"""A synchronous door onto an asynchronous tool.

The tools that read the board are coroutines, because the brain answers on an
async route and a board read is almost entirely waiting: awaited, it leaves the
loop free for everything else the turn is doing; blocking, it pins it.

But a graph compiled by `create_agent` has both a sync and an async path, and
LangChain's sync `ToolNode` calls `tool.invoke`, which on a coroutine-only tool
raises *"StructuredTool does not support sync invocation"* rather than falling
back. The callers on that path are real: the evals and several tests run
`agent.run`, and a dozen unit tests call a tool object directly. Worse, the
failure would not look like one — `ToolErrorMiddleware` turns it into
`{'error': …}`, so the model would read a broken door as a broken board and say
so, and every one of those tests would still pass while measuring nothing.

So each tool is written once, as a coroutine, and given a door: a plain function
that runs the coroutine on a loop of its own. `functools.wraps` is load-bearing
rather than cosmetic — `inspect.signature` follows `__wrapped__`, which is how
LangChain finds the `config: RunnableConfig` parameter it has to inject, and
without it a tool called through the sync door would silently lose the board it
was scoped to.
"""
from __future__ import annotations

import asyncio
import functools

from langchain_core.tools import BaseTool


def with_sync_door(tool: BaseTool) -> BaseTool:
    """The same tool, answering on both doors. Returns it for chaining.

    `asyncio.run` and not `get_event_loop`: this door is only ever taken by a
    caller that has no loop running, and a loop it creates and closes leaves
    nothing behind for the next one. A caller that *does* have a loop is on the
    async path already and never reaches here — if one ever did, `asyncio.run`
    says so loudly, which is the right failure for a mistake that would
    otherwise deadlock.
    """
    coroutine = tool.coroutine
    if coroutine is None:                       # already synchronous
        return tool

    @functools.wraps(coroutine)
    def door(*args, **kwargs):
        return asyncio.run(coroutine(*args, **kwargs))

    tool.func = door
    return tool


__all__ = ['with_sync_door']

"""Alternatives considered
========================

Why did you write your own async-to-sync bridge?
------------------------------------------------

Because the whole bridge is four lines, and the interesting part is not the
bridging — it is that LangChain looks up the injected `config` parameter through
`inspect.signature`, so the wrapper has to carry the wrapped function's
signature. Any library that returns a `*args, **kwargs` wrapper gets that wrong
in a way nothing reports: the tool runs, the board id is quietly missing, and an
Assistant scoped to one board answers about another.

**Why the obvious option fails.** The obvious option is to write each tool twice
— `StructuredTool.from_function(func=…, coroutine=…)` is the framework's own
shape for exactly this, and it is the shape this produces. What fails is the
*second body*: two implementations of `update_card` that must agree about which
fields a suggestion carries is precisely the duplication that lets one of them
drift, and the drift would be invisible because the two doors are exercised by
different tests.

**Why not the framework.** LangChain has the seam and not the fallback:
`BaseTool` holds both `func` and `coroutine`, and `_run` raises when the sync one
is absent — a deliberate refusal, because a framework cannot know whether the
caller is inside a loop. It also ships `run_in_executor` for the opposite
direction (sync work off an async path), which is the direction it *can* decide
safely. This module makes the choice the framework leaves to the application, in
the one place that knows the answer.

**The libraries that would do it** (checked 2026-08-13):

- **`asgiref.sync.async_to_sync`** — the mature one, from Django. Handles a
  running loop by delegating to a worker thread, propagates contextvars, and is
  the greenfield pick if this ever has to survive being called from inside a
  loop. It is a dependency this brain does not have, for four lines it does not
  need, and it does not preserve the signature either.
- **`anyio.from_thread.run`** — already installed (httpx depends on it), but it
  requires a *blocking portal* already started from the async side, which is a
  lifecycle this has no place to hang.
- **`nest_asyncio`** — patches the running loop so a nested `run` works. It
  monkey-patches asyncio globally to make a mistake survivable; a brain that
  needs it has the bug somewhere else.
- **`unsync`**, **`syncer`** — decorators that produce dual-mode functions.
  Unmaintained enough that the risk outweighs the four lines.

**Why they were not adopted, and what would change it.** No dependency earns its
place at this size, and the one thing that has to be right — the signature — is
something none of them do. What would change it: a sync caller appearing *inside*
a running loop. `asyncio.run` raises there, deliberately, so the failure is loud;
if that ever happens in real use rather than in a test, `asgiref` is the answer
and this module becomes an import.
"""
