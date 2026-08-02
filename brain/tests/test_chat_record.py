"""Stage 2 of Session 7: chat history becomes a record.

The brain records every chat turn through the Node API (assistant.db is the
record, written like every board write — never SQLite directly), and Chroma
becomes a derived index rebuilt from that record. What this closes: before,
`remember()` wrote chunks straight to Chroma and returned early when Chroma
was down, so every turn taken meanwhile was silently lost.

Contract under test:

- `BoardClient.record_chat(messages)` POSTs {'messages': [...]} to
  /api/chat/messages and returns the inserted rows (with ids);
  `BoardClient.list_chat()` GETs the live record.
- `ChatStore.index_messages(rows)` chunks each row's content into the index
  with deterministic per-chunk ids derived from the message id (so indexing
  the same row twice never duplicates), carrying metadata
  {message_id, role, created_day} — created_day from the MESSAGE's createdAt,
  not from today, so time-scoped recall works on imported history.
- `ChatStore.sync(rows)` indexes only rows the index does not know yet and
  returns how many it added — the rebuild path for turns recorded while
  Chroma was down.
- `create_app` records through the board API even when Chroma is off, never
  lets a recording failure destroy a turn, and syncs the index from the
  record at boot.
"""
from datetime import datetime, timezone

import httpx
import json
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.retrieval import ChatStore, LexicalHashEmbeddings, MEMORY_URL
from lodestar_brain.server import create_app
from lodestar_brain.tools.board import BoardClient

BOARD = 'http://board.test'


def ms(year, month, day):
    return int(datetime(year, month, day, 12, 0,
                        tzinfo=timezone.utc).timestamp() * 1000)


def row(id, content, role='user', created=ms(2026, 7, 1)):
    return {'id': id, 'role': role, 'content': content, 'createdAt': created}


def memory_store(collection):
    # One collection per test: chromadb shares the in-process client across
    # instances with identical settings, so the default 'chat' collection
    # would leak rows between tests in the same pytest process.
    return ChatStore(MEMORY_URL, LexicalHashEmbeddings(), collection=collection)


# This is a unit test.
@respx.mock
def test_board_client_records_and_lists_chat():
    sent = [{'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'}]
    echoed = [row(1, 'hi'), row(2, 'hello', role='assistant')]
    post = respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': echoed}))
    respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': echoed}))

    client = BoardClient(BOARD)
    assert client.record_chat(sent) == echoed
    assert json.loads(post.calls.last.request.content) == {'messages': sent}
    assert client.list_chat() == echoed


# This is an integration test (in-process Chroma, no server, no disk).
def test_index_messages_is_idempotent_and_dates_chunks_from_the_message():
    store = memory_store('chat-idempotent')
    message = row(7, 'رفتم اداره مالیات و جریمه رو دادم', created=ms(2026, 7, 1))
    store.index_messages([message])
    count = store.count()
    assert count > 0

    # The same row again must not grow the index — this is what makes a
    # rebuild from the record safe to run at every boot.
    store.index_messages([message])
    assert store.count() == count

    hits = store.search('مالیات')
    assert hits, 'the indexed message must be recallable'
    assert hits[0]['metadata']['message_id'] == 7
    assert hits[0]['metadata']['role'] == 'user'
    assert hits[0]['metadata']['created_day'] == 20260701, (
        'created_day comes from the message, not from today — otherwise '
        'time-scoped recall silently skips every imported message')


# This is an integration test (in-process Chroma, no server, no disk).
def test_sync_indexes_only_what_the_index_missed():
    store = memory_store('chat-sync')
    first = row(1, 'the wifi password is hunter2')
    missed = row(2, 'dentist appointment moved to friday')
    store.index_messages([first])

    assert store.sync([first, missed]) == 1, 'only the missed row is new'
    assert any('dentist' in h['text'] for h in store.search('dentist')), (
        'a turn recorded while Chroma was down is recallable after sync')
    assert store.sync([first, missed]) == 0, 'a second sync finds nothing new'


# This is an integration test.
@respx.mock
def test_every_turn_lands_in_the_record_even_without_chroma():
    # chroma_url='' — memory is off, exactly the state that used to lose turns.
    record = respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))

    res = client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'remember the wifi password'}]})
    assert res.status_code == 200
    assert record.called, 'the turn must reach the durable record'
    sent = json.loads(record.calls.last.request.content)['messages']
    assert [m['role'] for m in sent] == ['user', 'assistant']
    assert sent[0]['content'] == 'remember the wifi password'

    # A recording failure is logged, never fatal: the reply the user is
    # already reading must not be turned into a 500 after the fact.
    record.mock(return_value=httpx.Response(500))
    res = client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'still answers'}]})
    assert res.status_code == 200


# This is an integration test.
@respx.mock
def test_boot_syncs_the_index_from_the_record():
    respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': [
            row(1, 'the wifi password is hunter2')]}))
    respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url=MEMORY_URL, chat_collection='chat-boot-sync')))

    res = client.post('/rag/recall', json={'text': 'wifi password'})
    assert res.status_code == 200
    body = res.json()
    assert body['memory'] is True
    assert any('hunter2' in m['text'] for m in body['matches']), (
        'history recorded before this boot is recallable after it')


# This is an integration test.
@respx.mock
def test_reindex_route_rebuilds_the_index_from_the_record():
    """Stage 4: an import appends to the record while the brain is already
    running, so the boot sync has already happened. POST /rag/chat/reindex is
    that same sync on demand — and it answers honestly when memory is off."""
    record = respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url=MEMORY_URL, chat_collection='chat-reindex')))

    # The record grows after boot — exactly what an import does.
    record.mock(return_value=httpx.Response(200, json={'messages': [
        row(1, 'the wifi password is hunter2')]}))
    res = client.post('/rag/chat/reindex')
    assert res.status_code == 200
    assert res.json() == {'indexed': 1, 'memory': True}
    matches = client.post('/rag/recall',
                          json={'text': 'wifi password'}).json()['matches']
    assert any('hunter2' in m['text'] for m in matches), (
        'an imported message must be recallable after the reindex')
    assert client.post('/rag/chat/reindex').json() == {'indexed': 0, 'memory': True}, (
        'a second reindex must find nothing new')

    # Memory off: the truthful answer, not a 500 — the import itself already
    # succeeded into assistant.db and the next boot's sync will index it.
    off = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))
    assert off.post('/rag/chat/reindex').json() == {'indexed': 0, 'memory': False}


# This is an integration test.
@respx.mock
def test_a_board_down_at_boot_does_not_take_the_brain_down():
    # The boot sync is best-effort: the brain must serve even when Node is
    # not up yet (compose starts them together, in no promised order).
    respx.get(f'{BOARD}/api/chat/messages').mock(
        side_effect=httpx.ConnectError('board is down'))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url=MEMORY_URL, chat_collection='chat-board-down')))
    assert client.get('/health').json()['ok'] is True
