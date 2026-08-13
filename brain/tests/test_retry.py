"""A transient network blip must not cost the user the turn.

The brain chains several HTTP hops per turn (board, safety, model) and until now
none of them retried: one dropped connection or stray 500 failed the whole call.
The fix is a small bounded retry (`lodestar_brain.net`) applied only to
idempotent *reads* — a retried write is a duplicate, so `record_chat`,
`create_proposal` and `create_edit` stay single-shot, and these tests pin both
directions.

The contract under test:

- `net.retry(fn)` / `net.aretry(fn)` call `fn`, retry **once** on a transient
  failure (`httpx.TransportError`, or an `httpx.HTTPStatusError` of 500+), and
  re-raise anything else — a 4xx is a bad request and stays bad.
- The pause between attempts is `RETRY_BASE_DELAY` plus jitter, read at call
  time so a test can zero it.
- `BoardClient._get` goes through the retry; `BoardClient._post` never does.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from lodestar_brain import net
from lodestar_brain.board.client import BoardClient

BOARD = 'http://board.invalid'


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request('GET', f'{BOARD}/api/state')
    return httpx.HTTPStatusError(
        f'{code}', request=request, response=httpx.Response(code, request=request))


# This is a unit test.
def test_the_helper_retries_a_transient_once_and_never_a_4xx(monkeypatch):
    """One retry recovers a blip; a 4xx re-raises on the first attempt because
    re-sending a bad request cannot make it a good one."""
    monkeypatch.setattr(net, 'RETRY_BASE_DELAY', 0.0)

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError('wire fell out')
        return 'ok'

    assert net.retry(flaky) == 'ok'
    assert len(calls) == 2, 'one transient failure, one retry, done'

    # A 4xx is never retried — one attempt, the error re-raised as-is.
    calls.clear()

    def bad_request():
        calls.append(1)
        raise _status_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        net.retry(bad_request)
    assert len(calls) == 1

    # Exhausted retries re-raise the *last* transient error rather than
    # swallowing it — the caller's own error handling still sees the truth.
    calls.clear()

    def always_down():
        calls.append(1)
        raise _status_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        net.retry(always_down)
    assert len(calls) == net.RETRY_ATTEMPTS


# This is a unit test.
@respx.mock
def test_board_reads_retry_a_blip_and_board_writes_never_do(monkeypatch):
    """`list_cards` survives a 500-then-200 server; `record_chat` given a 500
    raises after exactly one request, because a retried write is a duplicate
    row in the durable chat record."""
    monkeypatch.setattr(net, 'RETRY_BASE_DELAY', 0.0)
    client = BoardClient(BOARD)

    read = respx.get(f'{BOARD}/api/state').mock(side_effect=[
        httpx.Response(500),
        httpx.Response(200, json={'cards': [{'id': 'c1', 'title': 'water plants'}]}),
    ])
    cards = asyncio.run(client.list_cards())
    assert [c['id'] for c in cards] == ['c1']
    assert read.call_count == 2, 'the 500 was retried, the 200 answered'

    write = respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.record_chat([{'role': 'user', 'content': 'hi'}]))
    assert write.call_count == 1, 'a failed write is reported, not re-sent'
