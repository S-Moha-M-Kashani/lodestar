"""The brain is not a person, and the board now asks everyone to log in.

Since 2026-09-01 the Node board requires an authenticated session even over
loopback, so the one non-human caller of its API needs a credential of its own.
It gets a shared service token rather than the user's password: the plaintext
then lives in one head and in no service's environment, and revoking the
brain's access does not mean changing what the owner types.

The contract under test:

- every call `BoardClient` makes carries `Authorization: Bearer <token>`, reads
  and writes alike;
- with no token configured it sends no Authorization header at all — it does
  not invent one, and it does not send an empty Bearer that an unconfigured
  board might match;
- the token reaches the client from `BOARD_API_TOKEN` through `Settings`.
"""
from __future__ import annotations

import asyncio

import httpx
import respx

from lodestar_brain.board.client import BoardClient
from lodestar_brain.config import load_settings

BOARD = 'http://board.invalid'
TOKEN = 'x' * 43


# This is a unit test.
@respx.mock
def test_every_call_carries_the_service_token():
    read = respx.get(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'cards': []}))
    write = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json={'card': {'id': 'a'}}))

    client = BoardClient(BOARD, token=TOKEN)
    asyncio.run(client.list_cards())
    asyncio.run(client.create_proposal({'title': 'x'}))

    for route in (read, write):
        sent = route.calls.last.request
        assert sent.headers['authorization'] == f'Bearer {TOKEN}', (
            'a board call went out unauthenticated')

    # Whitespace around the value is trimmed rather than signed into the
    # header: a token pasted out of a terminal usually brings a newline.
    padded = BoardClient(BOARD, token=f'  {TOKEN}\n')
    asyncio.run(padded.list_cards())
    assert read.calls.last.request.headers['authorization'] == f'Bearer {TOKEN}'


# This is a unit test.
@respx.mock
def test_an_unconfigured_brain_sends_no_credential():
    route = respx.get(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'cards': []}))
    asyncio.run(BoardClient(BOARD).list_cards())
    # Not an empty Bearer: a board that has no token configured must not be
    # matchable by sending one, and the brain must not be the thing that tries.
    assert 'authorization' not in route.calls.last.request.headers


# This is a configuration invariant.
def test_the_token_comes_from_the_environment():
    assert load_settings({'BOARD_API_TOKEN': TOKEN}).board_api_token == TOKEN
    # Absent means empty, which means the brain gets 401 from a board doing its
    # job — the safe direction to fail in, and the default for every unit test
    # and eval, which answer their own mocked board.
    assert load_settings({}).board_api_token == ''
