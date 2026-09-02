"""Chat recall stops holding the event loop, and the corpus it ranks is cached.

`ChatStore.search` reads *every* chunk in the collection and builds BM25 over it
before it ranks anything, and until now it did that on every call — from
`find_related`, which is a coroutine, and from `/rag/recall`, which is a route.
So one recall was the whole process stopped, and the work it stopped it for was
work it had already done.

**Measured on 2026-09-02** (this machine, `BRAIN_EMBEDDER=fake`, the in-process
Chroma, 40-word messages, one chunk each; `min` of repeated calls):

| chunks | search, cold | search, warm | inline recall | worst 5 ms heartbeat gap |
| --- | --- | --- | --- | --- |
| 50  |  7.7 ms | 4.0 ms |  7.7 ms | 7.8 ms inline / 5.8 ms offloaded |
| 200 | 16.8 ms | 5.2 ms | 16.0 ms | 21.6 ms inline / 10.1 ms offloaded |
| 500 | 35.6 ms | 6.7 ms | 30.3 ms | 30.9 ms inline / 17.7 ms offloaded |

A local store is quick enough that the offload alone reads as noise; the cache is
what took the recall itself from 35.6 ms to 6.7 ms at 500 chunks (13.7 ms of it
was fetching the corpus, 16.3 ms building BM25), and a write puts it back to
30.7 ms, which is the invalidation working.

Against the real Chroma container (:8004, the test stack, 200 messages) the same
shape holds with a network in it: a cold
recall is 25.9 ms and a warm one 13.4 ms, and while an inline recall ran the loop
served **0** other requests where `asearch` served every one that came due.

The stall is easiest to see at the latency a *loaded* Chroma has, so the first
test below rigs one: with every store call waiting 300 ms, a 618 ms recall inline
served **0** other requests, and the same recall through `asearch` served **29**
(worst wait 23 ms).

Two honest costs. Offloading makes one recall slightly *slower* in wall-clock
time — 26.6 ms inline against 32.2 ms through the thread hop, on the real store —
which is the point: the process now spends that time serving other people.
And none of this is measured against `heydariAI/persian-embeddings`, which is
what the default embedder is; it only ever adds blocking CPU to the same call,
so the direction is safe to state and the magnitudes are not.
"""
import asyncio
import time

import pytest

from lodestar_brain.retrieval import (MEMORY_URL, ChatStore,
                                      LexicalHashEmbeddings)

PASSWORD = 'the wifi password is hunter2'
QUERY = 'wifi password'


def row(id, content, created=1751000000000, session='s1', board='main'):
    return {'id': id, 'role': 'user', 'content': content, 'createdAt': created,
            'sessionId': session, 'boardId': board}


def store(collection, rows=(row(1, PASSWORD),)):
    made = ChatStore(MEMORY_URL, LexicalHashEmbeddings(), collection=collection)
    made.index_messages(list(rows))
    return made


def slow(monkeypatch, made, seconds=0.3):
    """A Chroma that answers like one over a network: every call waits.

    The latency is what the blocking is measured in, so it is rigged rather than
    hoped for — an in-process store is fast enough that a test asserting "the
    loop was free" would pass with the offload removed.
    """
    calls = []
    for name in ('get', 'similarity_search'):
        real = getattr(made.store, name)

        def waiting(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            time.sleep(seconds)
            return _real(*args, **kwargs)

        monkeypatch.setattr(made.store, name, waiting)
    return calls


async def _served_during(recall):
    """How many times a 20 ms heartbeat — stand-in for /health, or any other
    request — got its turn while `recall` ran, and what the recall returned."""
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)          # let the heartbeat reach its first await
    hits = await recall()
    beat.cancel()
    return ticks, hits


# This is a unit test.
def test_a_slow_recall_no_longer_stops_the_process_answering(monkeypatch):
    """The requirement: a lightweight request stays responsive during a recall.

    Both doors are exercised in one test on purpose. Asserting only that
    `asearch` left the loop free proves nothing about the bug — a store fast
    enough would satisfy it either way — so the synchronous call is measured
    first and has to show the stall this exists to remove.
    """
    made = store('nonblocking-slow')
    made.search(QUERY, k=3)          # one paid fetch, as in ordinary use
    calls = slow(monkeypatch, made)

    async def inline():
        return made.search(QUERY, k=3)

    blocked, inline_hits = asyncio.run(_served_during(inline))
    offloaded, hits = asyncio.run(
        _served_during(lambda: made.asearch(QUERY, k=3)))

    assert calls, 'the rigged store was never reached — nothing was measured'
    assert blocked == 0, (
        f'the loop served {blocked} heartbeats during an inline recall; the '
        'store is not slow enough for this test to mean anything')
    assert offloaded >= 3, (
        f'the loop served only {offloaded} heartbeats during a >0.3s asearch — '
        'the blocking store work ran on the event loop, so one recall stalls '
        'every other request in the process')
    # And the hop changed nothing about the answer or how it is reported.
    assert [hit['text'] for hit in hits] == [hit['text'] for hit in inline_hits]
    assert PASSWORD in hits[0]['text']


# This is a unit test.
def test_an_exception_in_the_worker_thread_surfaces_unchanged():
    """Error handling is part of the contract the offload must preserve: the
    caller's `except` has to read the same after the work moved threads."""
    made = store('nonblocking-raises')

    def broken(*_args, **_kwargs):
        raise RuntimeError('chroma is gone')

    made.store.get = broken
    made._corpus_key = None          # force the fetch this call cannot make

    with pytest.raises(RuntimeError, match='chroma is gone'):
        asyncio.run(made.asearch(QUERY, k=3))


# This is an integration test: a real Chroma client, in process, no disk.
def test_the_corpus_is_fetched_once_and_answers_the_same():
    """The cache may make a recall cheaper and must not make it different.

    Two claims, and the second is the one worth a test: that the fetch really is
    skipped. Without it a cache that quietly re-read the collection every time
    would pass every correctness assertion in this file.
    """
    made = store('nonblocking-cached', [row(1, PASSWORD),
                                        row(2, 'we changed the wifi router'),
                                        row(3, 'dentist on friday')])
    fetches = []
    real_get = made.store.get
    made.store.get = lambda *a, **kw: (fetches.append(1), real_get(*a, **kw))[1]

    first = made.search(QUERY, k=3)
    repeat = [made.search(QUERY, k=3) for _ in range(4)]
    assert fetches == [1], (
        f'the collection was read {len(fetches)} times for five identical '
        'searches')
    assert all(hits == first for hits in repeat), 'a cached search drifted'

    # The same question, asked with the cache thrown away: the answer a caller
    # would have got before any of this existed.
    made._corpus_key = None
    made._lexical_cache.clear()
    assert made.search(QUERY, k=3) == first
    assert len(fetches) == 2, 'an invalidated corpus must be re-read'


# This is an integration test: a real Chroma client, in process, no disk.
def test_a_new_memory_is_recallable_without_a_restart():
    """The invalidation requirement, from both sides of the identity key.

    Our own write bumps the write counter; a writer that is not this object —
    another brain, an import, a `prune` elsewhere — changes the chunk count. The
    second half is asserted by writing *through* the underlying store, which is
    the only way to be sure the counter is not carrying the whole test.
    """
    made = store('nonblocking-fresh')
    assert [hit['text'] for hit in made.search('boiler', k=3)] == []

    made.index_messages([row(2, 'the boiler needs descaling in March')])
    assert any('boiler' in hit['text'] for hit in made.search('boiler', k=3)), (
        'a message indexed after the first search must be recallable at once')

    made.store.add_texts(['the landlord kept the deposit'], ids=['3:0'],
                         metadatas=[{'message_id': 3, 'role': 'user',
                                     'created_day': 20260701,
                                     'session_id': 's9', 'board_id': 'main'}])
    assert any('landlord' in hit['text'] for hit in made.search('deposit', k=3)), (
        'a chunk written by someone else changes the count, and the count is '
        'half the cache key for exactly this reason')


# This is a unit test.
def test_a_failed_refresh_raises_instead_of_serving_the_old_corpus():
    """A cache is allowed to be cheap; it is not allowed to be wrong.

    Once the identity has moved, the entry is a corpus we *know* is out of date.
    If the fetch that would replace it fails, the honest answer is the failure —
    "Chroma is unreachable" is true, and an answer from the previous generation
    would be a stale recall nothing marks as stale.
    """
    made = store('nonblocking-stale')
    warm = made.search(QUERY, k=3)
    assert warm, 'the search has to work before its failure means anything'

    made.index_messages([row(2, 'the wifi password changed to correct-horse')])

    def broken(*_args, **_kwargs):
        raise RuntimeError('chroma is unreachable')

    real_get = made.store.get
    made.store.get = broken
    with pytest.raises(RuntimeError):
        made.search(QUERY, k=3)

    # And the entry is gone rather than merely unread: the store recovers into
    # the *new* corpus, not into the one it was serving before the failure.
    made.store.get = real_get
    recovered = [hit['text'] for hit in made.search(QUERY, k=5)]
    assert any('correct-horse' in text for text in recovered)


# This is an integration test: a real Chroma client, in process, no disk.
def test_concurrent_recalls_are_serialised_and_agree_with_one_recall_alone():
    """The named risk of the offload: two threads inside one store.

    `recall_chat` is a synchronous tool, so LangChain has always run it in an
    executor thread — two overlapping turns already reached one Chroma client,
    one embedder and one BM25 build with nothing serialising them, and the
    corpus cache adds shared mutable state to that. Neither `chromadb` nor
    `BM25Okapi` promises anything about concurrent use, and the failure would
    not raise: it would be a ranking built from one generation's documents and
    another's postings.

    So agreement is asserted, and — because agreement alone passes with the lock
    removed, which was checked — so is the serialisation itself: no two store
    calls may overlap in time. That is the property the lock exists for, and it
    is observable.
    """
    made = store('nonblocking-concurrent', [row(1, PASSWORD),
                                            row(2, 'dentist on friday')])
    alone = made.search(QUERY, k=3)
    assert [hit['text'] for hit in alone] == [PASSWORD]

    spans = []
    real = made.store.similarity_search

    def timed(*args, **kwargs):
        start = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            # Long enough that an unserialised pair overlaps beyond any doubt,
            # short enough that eight of them are a blink.
            time.sleep(0.02)
            spans.append((start, time.perf_counter()))

    made.store.similarity_search = timed

    async def scenario():
        writes = [made.aindex_messages([row(100 + n, f'note {n} about the '
                                            'boiler and the deposit')])
                  for n in range(3)]
        recalls = [made.asearch(QUERY, k=3) for _ in range(8)]
        return await asyncio.gather(*recalls, *writes)

    results = asyncio.run(scenario())
    for hits in results[:8]:
        assert [hit['text'] for hit in hits] == [PASSWORD], (
            'a recall running beside other recalls and a write returned '
            'something a recall on its own does not')
    # The interleaved writes are in the index, so the searches were not answered
    # from a corpus frozen before them.
    assert len(made.search('boiler deposit', k=5)) == 3

    assert len(spans) >= 8, f'only {len(spans)} recalls reached the store'
    overlaps = [(a, b) for a, b in zip(sorted(spans), sorted(spans)[1:])
                if b[0] < a[1]]
    assert not overlaps, (
        f'{len(overlaps)} pair(s) of store calls ran at the same time: the '
        'guard is not serialising access, so one Chroma client and one BM25 '
        'index are being used from two threads at once')
