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
- A recorded turn names the session it belongs to and carries the assistant
  row's `steps`, `usage` and `cost` — the brain is the only writer of a turn
  and the only place all three are known at once.
- `ChatStore.prune(rows)` removes chunks for messages the live record no longer
  returns, which is what makes deleting a chat reach the index instead of
  leaving it answering `recall_chat` forever.
- Both chat routes record *after* the response, as a background task — and the
  turn still always lands, including with Chroma down.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import json
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.retrieval import ChatStore, LexicalHashEmbeddings, MEMORY_URL
from lodestar_brain.server import ChatBody, create_app
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
    scoped = respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': echoed}))
    every = respx.get(f'{BOARD}/api/chat/messages/all').mock(
        return_value=httpx.Response(200, json={'messages': echoed}))

    client = BoardClient(BOARD)
    assert client.record_chat(sent) == echoed
    assert json.loads(post.calls.last.request.content) == {'messages': sent}
    assert client.list_chat() == echoed
    # An empty board is omitted, never sent as '': the server files an unnamed
    # batch under its default board, and it can only do that if it can tell the
    # two apart. Named, it travels as a query parameter on the read and in the
    # body on the write, beside the session it belongs with.
    assert 'board' not in scoped.calls.last.request.url.params
    assert client.record_chat(sent, board_id='b-home') == echoed
    assert json.loads(post.calls.last.request.content)['boardId'] == 'b-home'
    assert client.list_chat('b-home') == echoed
    assert 'board=b-home' in str(scoped.calls.last.request.url)
    # The index reads every board at once — pruning from one board's messages
    # would drop all the others out of recall.
    assert client.list_all_chat() == echoed
    assert every.called


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
def test_the_stream_records_its_turn_after_the_last_event():
    """Bookkeeping is not something the user waits for — and is never skipped.

    Recording used to happen beside the `done` frame, so the last event of every
    turn was held back for a round trip to Node and an embedding pass: the answer
    finished arriving after the work of filing it. It is a background task now,
    which Starlette awaits once the final chunk is sent.

    Both halves are asserted, because moving it is only safe if it still always
    happens. The route is driven directly rather than through TestClient for
    exactly that reason: TestClient runs the whole app — background tasks
    included — before a single byte is readable, so it can prove the turn lands
    and can never prove it landed *after*. Chroma is off, which is the
    configuration that used to lose turns outright.
    """
    record = respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    app = create_app(Settings(llm_provider='fake', embedder='fake',
                              board_api_url=BOARD, chroma_url=''))
    stream = next(route.endpoint for route in app.routes
                  if getattr(route, 'path', '') == '/agent/chat/stream')

    async def drive():
        response = await stream(ChatBody(messages=[
            {'role': 'user', 'content': 'the wifi password is hunter2'}]))
        frames = [chunk async for chunk in response.body_iterator]
        return response, frames

    response, frames = asyncio.run(drive())
    assert any('event: done' in str(frame) for frame in frames)
    assert not record.called, (
        'the turn was recorded before the browser had the whole stream')

    asyncio.run(response.background())   # what Starlette does after the last chunk
    assert record.called, 'a turn must reach the record even with Chroma down'
    sent = json.loads(record.calls.last.request.content)['messages']
    assert [m['role'] for m in sent] == ['user', 'assistant']


# This is an integration test.
@respx.mock
def test_boot_syncs_the_index_from_the_record():
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
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
    record = respx.get(f'{BOARD}/api/chat/messages/all').mock(
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
    # `pruned` rides along because the route now syncs in both directions: the
    # browser fires it after deleting a chat, and a delete is rows LEAVING the
    # record. Nothing has been deleted here, so it is 0.
    assert res.json() == {'indexed': 1, 'pruned': 0, 'memory': True}
    matches = client.post('/rag/recall',
                          json={'text': 'wifi password'}).json()['matches']
    assert any('hunter2' in m['text'] for m in matches), (
        'an imported message must be recallable after the reindex')
    assert client.post('/rag/chat/reindex').json() == {
        'indexed': 0, 'pruned': 0, 'memory': True}, (
        'a second reindex must find nothing new')

    # A chat deleted while the brain was running: the record stops returning its
    # rows, and this route is what takes them out of the index.
    record.mock(return_value=httpx.Response(200, json={'messages': []}))
    assert client.post('/rag/chat/reindex').json() == {
        'indexed': 0, 'pruned': 1, 'memory': True}
    assert not client.post('/rag/recall',
                           json={'text': 'wifi password'}).json()['matches'], (
        'a deleted chat must stop answering recall')

    # Memory off: the truthful answer, not a 500 — the import itself already
    # succeeded into assistant.db and the next boot's sync will index it.
    off = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))
    assert off.post('/rag/chat/reindex').json() == {'indexed': 0, 'memory': False}


# This is an integration test.
@respx.mock
def test_the_recorded_turn_names_its_session_and_what_it_spent():
    """A turn is recorded into the chat it belongs to, with its receipt.

    `session_id` rides on ChatBody and is forwarded to the record, because the
    brain is the only writer of a turn and a row without a session would be a
    turn absent from every list. `steps`, `usage` and `cost` go with it: the
    Assistant re-renders tool evidence and the price when a historic chat is
    reopened, and the brain is the only place all three are known at once.
    """
    record = respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))

    res = client.post('/agent/chat', json={
        'session_id': 's-berlin',
        'messages': [{'role': 'user', 'content': 'plan the berlin trip'}]})
    assert res.status_code == 200
    body = json.loads(record.calls.last.request.content)
    assert body['sessionId'] == 's-berlin'
    user, assistant = body['messages']
    assert [user['role'], assistant['role']] == ['user', 'assistant']
    assert isinstance(assistant['steps'], list), (
        'the assistant row carries its tool steps, so reopening the chat shows '
        'the evidence and not only the prose')
    assert assistant['usage']['total_tokens'] > 0
    # 0.0 because a local model genuinely costs nothing — see
    # test_a_turn_reports_what_it_cost_and_says_nothing_when_it_cannot. The
    # unknown-price direction (None, never a fabricated zero) is pricing.py's.
    assert assistant['cost'] == 0.0
    assert 'steps' not in user and 'cost' not in user, (
        'a user row has no receipt of its own — the fields belong to the turn '
        'the model took')

    # No session named: the record still takes it, and Node files it under the
    # reserved 'adhoc' chat. Sixteen tests and every eval post without a
    # session, and none of them should be a lost turn.
    client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'no session named'}]})
    assert 'sessionId' not in json.loads(record.calls.last.request.content), (
        'an unnamed session is omitted, not sent as an empty string — the '
        'server decides the fallback, and it must be able to tell them apart')


# This is an integration test (in-process Chroma, no server, no disk).
def test_prune_drops_chunks_the_record_no_longer_returns():
    """The missing half of a derived index.

    `sync` only ever adds, so deleting a chat used to leave its chunks
    answering `recall_chat` forever — a conversation you deleted resurfacing in
    an answer, which is the worst possible version of a history feature. `prune`
    is what makes the soft delete reach Chroma; the browser fires
    /rag/chat/reindex after a delete so it happens at once rather than at the
    next boot.
    """
    store = memory_store('chat-prune')
    kept = row(1, 'the wifi password is hunter2')
    deleted = row(2, 'dentist appointment moved to friday')
    store.index_messages([kept, deleted])
    assert any('dentist' in h['text'] for h in store.search('dentist'))

    # The live record no longer returns row 2 — its session was soft-deleted.
    assert store.prune([kept]) == 1, 'only the vanished row is pruned'
    assert not store.search('dentist'), (
        'a deleted chat must stop answering recall')
    assert any('hunter2' in h['text'] for h in store.search('wifi password')), (
        'and pruning must not take the surviving chat with it')
    assert store.prune([kept]) == 0, 'a second prune finds nothing to do'
    # An empty record prunes everything rather than reading it as "no news".
    # The opposite reading would make a wiped record un-prunable, which is the
    # one case where leaving chunks behind is most obviously wrong.
    assert store.prune([]) == 1


# This is an integration test (in-process Chroma, no server, no disk).
def test_a_restored_message_is_indexed_again():
    """Deleting one turn is reversible, so the index has to be reversible too.

    A single message can now be soft-deleted from a chat and restored from the
    assistant's trash. Both halves reach Chroma through the same route the
    browser already fires after a delete: `prune` takes the hidden turn out,
    `sync` puts the restored one back. Without the second half, restore would
    return a turn the assistant could still never recall — visible in the
    transcript, absent from its own memory.
    """
    store = memory_store('chat-restore')
    kept = row(1, 'the wifi password is hunter2')
    hidden = row(2, 'dentist appointment moved to friday')
    store.index_messages([kept, hidden])

    assert store.prune([kept]) == 1
    assert not store.search('dentist')

    # The turn is back in the live record. sync only ever adds, which is exactly
    # what a restore needs: the surviving chunks are left where they are.
    assert store.sync([kept, hidden]) == 1, 'only the returning turn is indexed'
    assert any('dentist' in h['text'] for h in store.search('dentist')), (
        'a restored turn is recallable again')
    assert store.sync([kept, hidden]) == 0, 'and it is not indexed twice'


# This is an integration test.
@respx.mock
def test_a_board_down_at_boot_does_not_take_the_brain_down():
    # The boot sync is best-effort: the brain must serve even when Node is
    # not up yet (compose starts them together, in no promised order).
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
        side_effect=httpx.ConnectError('board is down'))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url=MEMORY_URL, chat_collection='chat-board-down')))
    assert client.get('/health').json()['ok'] is True
