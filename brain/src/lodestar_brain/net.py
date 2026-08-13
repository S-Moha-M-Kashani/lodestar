"""One bounded retry for idempotent reads — and nothing else.

A turn chains several HTTP hops (board, safety, model), and a single dropped
connection or stray 500 used to fail the whole turn. `retry`/`aretry` recover
exactly that class of blip: a transport error, or a 5xx from a server that may
answer the second time. A 4xx is never retried — re-sending a bad request
cannot make it a good one — and *writes* are never passed through here at all,
because a retried write is a duplicate (`record_chat` filing the same turn
twice, `create_proposal` offering the same card twice).

Worst case added latency is bounded and small: one extra attempt, so one extra
timeout plus under half a second of pause (`RETRY_BASE_DELAY` plus the same
again in jitter). The chat-model path deliberately does not use this module —
the OpenAI SDK under `init_chat_model` already retries twice with backoff.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar('T')

# Total attempts, not extra ones: 2 means the call runs at most twice.
RETRY_ATTEMPTS = 2
# Seconds before the retry, plus uniform jitter of the same size. Read at call
# time, so tests can zero it.
RETRY_BASE_DELAY = 0.25


def _transient(exc: Exception) -> bool:
    """A failure the second attempt could plausibly not have."""
    if isinstance(exc, httpx.TransportError):
        return True
    return (isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code >= 500)


def _pause() -> float:
    return RETRY_BASE_DELAY + random.uniform(0, RETRY_BASE_DELAY)


def retry(fn: Callable[[], T]) -> T:
    """Call `fn`; on a transient failure, wait briefly and try once more."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS or not _transient(exc):
                raise
            time.sleep(_pause())
    raise AssertionError('unreachable')  # the loop always returns or raises


async def aretry(fn: Callable[[], Awaitable[T]]) -> T:
    """`retry` for coroutines, sleeping on the loop rather than the thread."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS or not _transient(exc):
                raise
            await asyncio.sleep(_pause())
    raise AssertionError('unreachable')


__all__ = ['RETRY_ATTEMPTS', 'RETRY_BASE_DELAY', 'aretry', 'retry']

"""Alternatives considered
========================

Why is this retry loop yours?
-----------------------------

Because it is twenty lines, the suite must stay offline with no extras, and the
decisions that matter — retry *what*, at *which* call sites, and never on a
write — live at the call sites regardless of who owns the loop.

**Why the obvious option fails.** The obvious option is
`httpx.AsyncHTTPTransport(retries=N)`: already a dependency, one constructor
argument. But it retries *connect* failures only — never a read timeout, never
a 5xx — so it misses both failure modes actually seen here (a server that
answered slowly and a server that answered 500). It also attaches to a client,
and `BoardClient` deliberately builds a client per call (a pool binds to the
loop that created it, and this process runs more than one loop), so the
transport would be reconstructed per call anyway.

**Why not the framework.** LangChain retries its own model calls (the OpenAI
SDK's `DEFAULT_MAX_RETRIES = 2` rides along under `init_chat_model`, which is
why `llm.py` is not a caller of this module), but offers nothing for arbitrary
HTTP the tools make. Its `InMemoryRateLimiter` was already rejected for the
server-side bucket for pacing by sleeping; the same objection applies here to
anything that would hold a turn open longer than one bounded pause.

**The libraries that would do it** (checked 2026-08-13):

- **`tenacity`** — the standard, and the greenfield pick: decorators, wait
  strategies, sync and async. Buys nothing at two call sites that a plain loop
  does not, and is a new runtime dependency in a package whose tests run with
  no extras.
- **`stamina`** — tenacity with safer defaults and a smaller surface. Same
  trade, same verdict.
- **`httpx-retries` / transport wrappers** — status-code-aware transports;
  still per-client objects, which collides with the per-call-client decision in
  `board/client.py`.

**Why they were not adopted.** Decisively: two call sites and a hard
offline-suite constraint do not buy a dependency. **What would change the
decision:** a third or fourth call site with different backoff needs (per-call
budgets, retry-after handling, circuit breaking). The day this module grows a
strategy argument is the day it should become `tenacity` instead.
"""
