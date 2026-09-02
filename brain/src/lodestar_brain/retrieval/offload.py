"""The boundary a blocking store is reached through: one lock, one thread hop.

`ChatStore` wraps a Chroma client, an embedder and a BM25 index, none of which
promises anything about being called from two threads at once, and all of which
block — a Chroma call is an HTTP round trip, an embedding is CPU, and building
BM25 over the whole collection is CPU proportional to it. The brain is one
asyncio process, so both facts are hazards of the same shape: work that must not
run on the event loop, and must not run twice at the same time.

This module is that boundary, and it is deliberately tiny. Two rules, and the
second is the one that is easy to get wrong:

1. **The work takes the lock**, in whatever thread it ends up on.
2. **The door does not.** `offload` only moves the call to a worker thread; if it
   acquired the lock before handing over, the event loop would sit waiting for
   another recall to finish — a stall with the offload machinery in place, which
   is the failure that looks fixed.

A guarded method may call another guarded method (`reconcile` calls `sync`, which
calls `index_messages`), so the lock is reentrant. A store that is only ever
reached through one process's `StoreGuard` is serialised by it; two `ChatStore`
objects over one Chroma *service* are not, and never could be from here — that
is the service's own concurrency, and Chroma answers for it.
"""
import asyncio
import threading
from collections.abc import Callable
from typing import Any


class StoreGuard:
    """One lock and one thread hop — the whole of the async boundary.

    Used as a context manager by the blocking methods themselves, and through
    `offload` by the coroutines that call them:

        def search(self, ...):
            with self._guard:
                ...blocking work...

        async def asearch(self, ...):
            return await self._guard.offload(self.search, ...)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> 'StoreGuard':
        self._lock.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()

    async def offload(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Run a blocking, self-guarding call in a worker thread.

        The lock is *not* taken here: it is taken by `fn`, in the thread it runs
        in. Taking it on this side would make the event loop wait for whatever
        else is inside the store — the stall this module exists to remove.

        An exception raised in the thread surfaces here unchanged, so a caller's
        `try/except` reads exactly as it did before the hop.
        """
        return await asyncio.to_thread(fn, *args, **kwargs)


"""Alternatives considered

**"Why a hand-written lock plus `asyncio.to_thread`, rather than a library?"**

*Short answer.* Because the amount of machinery this needs is a reentrant lock
and one `to_thread`, and every library that would supply it either supplies the
same thing under another name or changes the failure modes of a store that holds
a live socket. The value here is not the code, it is the rule that the door does
not hold the lock — and no library can hold that rule for us.

*Why the obvious option fails.* The obvious option is "no boundary at all", and
it fails twice over. `find_related` is a coroutine that called
`ChatStore.search` inline, so one recall stopped the process answering anything
— measured on 2026-09-02 with the in-process Chroma and the hash embedder, a
500-chunk recall held the loop for 30 ms and delayed a 5 ms heartbeat by 30.9 ms;
with a real Chroma over HTTP and `heydariAI/persian-embeddings` the same call is
hundreds of milliseconds, all of it on the loop. The second failure is quieter:
`recall_chat` is a synchronous tool, so LangChain already runs it in an executor
thread (`BaseTool._arun` → `run_in_executor`), which means two overlapping turns
were already reaching one Chroma client and one BM25 build from two threads with
nothing serialising them. Nothing raised; it simply was not a claim anyone had
checked.

*Why not the framework.* LangChain gives the hop and nothing else: `run_in_executor`
is `loop.run_in_executor` with the current context copied, which is what
`asyncio.to_thread` does. It has no notion of a store that must be entered by one
caller at a time, and `BaseTool`'s sync-to-async fallback is precisely the path
that created the unguarded concurrency above. Where the framework *is* the answer
we use it: `Chroma` is `langchain_chroma`'s, the retriever is a `BaseRetriever`,
and `AsyncSqliteSaver`/`AsyncSqliteStore` are LangGraph's async-native stores —
this module exists for the one dependency that has no async-native form here.

*The libraries that would do it.* `anyio.to_thread.run_sync` with a
`CapacityLimiter` — the closest fit, already an indirect dependency through
FastAPI, and it would let one limiter cap store concurrency at 1 instead of a
lock; rejected only because the brain is asyncio-only and `to_thread` is stdlib,
so the dependency buys a synonym. `concurrent.futures.ThreadPoolExecutor(max_workers=1)`
per store — serialises by construction with no lock at all, and is the option to
take if fairness ever matters (a lock makes no queueing promise); it costs a
thread per store for its whole life and turns every call into a future.
`ProcessPoolExecutor` — real parallelism past the GIL, and unusable: the Chroma
client and the embedder are not picklable and a live socket cannot cross a
process. `aiochroma`/an async Chroma client — there is no maintained one, and
`langchain_chroma`'s async methods are the sync ones behind
`run_in_executor` anyway. Redis or a task queue — a second service, for a
single-user board.

*Why not adopted, and what would change it.* Decisively: `to_thread` plus a
reentrant lock is eleven lines of stdlib for a store that is called a handful of
times a turn, and the corpus cache in `chat.py` is what actually removed the
cost — the boundary only stops one caller from starving the others. What would
change it is *contention*: if recalls ever overlap often enough that serialising
them shows up as latency, the answer is the single-worker executor plus a wider
read/write split (many concurrent searches over one immutable corpus generation,
writers excluded), and the measurement that would justify it is the wait time
inside `StoreGuard` — which is not instrumented today, deliberately, because
this board's traffic is one person.
"""
