import base64
import itertools
import json

import httpx
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import MAX_CHARS, MAX_MESSAGES, create_app
from lodestar_brain.voice.fake import FAKE_TRANSCRIPT


# This is an integration test.
def test_health():
    client = TestClient(create_app(Settings(llm_provider='fake', embedder='fake')))
    res = client.get('/health')
    assert res.status_code == 200
    assert res.json() == {'ok': True, 'service': 'lodestar-brain'}


def fake_app():
    return create_app(Settings(llm_provider='fake', embedder='fake',
                               transcriber='fake',
                               board_api_url='http://board.test'))


def board_state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards})


def card(id, title):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '', 'num': 1,
            'tags': [], 'createdAt': 1, 'updatedAt': 1}


# This is an integration test.
@respx.mock
def test_chat_echo_roundtrip():
    client = TestClient(fake_app())
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'hello brain'}]})
    assert res.status_code == 200
    body = res.json()
    assert body['reply'] == 'FAKE: hello brain'
    assert body['mutated'] is False
    assert body['steps'] == []
    # What the turn spent, so the Assistant can show it. Reported by the offline
    # backend too — otherwise the whole path is untestable without a paid model.
    assert body['usage']['total_tokens'] == (body['usage']['input_tokens']
                                             + body['usage']['output_tokens'])
    assert body['usage']['output_tokens'] > 0


# This is an integration test.
@respx.mock
def test_chat_accepts_the_providers_the_picker_offers_and_refuses_the_rest():
    """The Assistant's provider selector rides along on every chat turn, so the
    route is the boundary that has to reject a provider this brain cannot serve.
    A fake brain answers regardless — the offline contract belongs to the server,
    not to a browser deciding when to leave the field out."""
    client = TestClient(fake_app())
    # The two CLI subscriptions are in this list now, and being in it is the
    # whole change: the picker offers them, so the route has to accept them.
    # Left out, they were a 422 from the request model before `make_chat_model`
    # was ever asked — a validation error the browser could do nothing with,
    # for a backend the brain can serve perfectly well.
    for provider in ('ollama', 'openrouter', 'claude-cli', 'codex-cli'):
        res = client.post('/agent/chat', json={
            'messages': [{'role': 'user', 'content': 'hello brain'}],
            'provider': provider})
        assert res.status_code == 200, provider
        assert res.json()['reply'] == 'FAKE: hello brain'
        # A CLI turn spends subscription quota, which is not a per-token bill and
        # not a free turn either. `pricing.py` reports nothing rather than a
        # measurement nobody made, and the route must carry that None through
        # instead of rendering it as 0.0 on the way out.
        if provider.endswith('-cli'):
            assert res.json()['cost'] is None, provider
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'hello brain'}],
        'provider': 'anthropic'})
    assert res.status_code == 422


# This is an integration test.
@respx.mock
def test_both_chat_routes_refuse_a_conversation_past_the_caps():
    """Two caps, because there are two ways to arrive with too much.

    The browser sends the whole conversation on every turn, so a chat that runs
    long is a bigger request each time. A thousand one-word messages and one
    novel-length message both blow past a context window, and a character cap
    alone lets the first through. Checked on both routes: a second route is a
    second place to forget.
    """
    client = TestClient(fake_app())
    too_many = [{'role': 'user', 'content': 'hi'} for _ in range(MAX_MESSAGES + 1)]
    too_long = [{'role': 'user', 'content': 'x' * (MAX_CHARS + 1)}]
    for path in ('/agent/chat', '/agent/chat/stream'):
        for messages in (too_many, too_long):
            res = client.post(path, json={'messages': messages})
            assert res.status_code == 413, path
    # And an ordinary turn still gets through — a cap that closes the door is
    # not a cap.
    assert client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'hello brain'}]}).status_code == 200


# This is an integration test.
@respx.mock
def test_chat_add_proposes_a_card_without_mutating_the_board():
    # create_card now proposes; the board is unchanged until the user
    # confirms. The two events are distinct flags because the frontend reacts
    # differently: `mutated` means adopt the board, `proposed` means refresh the
    # proposals list.
    proposal = respx.post('http://board.test/api/proposals').mock(
        return_value=httpx.Response(200, json=card('n1', 'What is Leiden clustering?')))
    put = respx.put('http://board.test/api/state').mock(return_value=board_state([]))
    client = TestClient(fake_app())
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'add: What is Leiden clustering?'}]})
    body = res.json()
    assert proposal.called
    assert not put.called, 'proposing must not write the board'
    assert body['proposed'] is True
    assert body['mutated'] is False
    step, = body['steps']
    assert step['tool'] == 'create_card'
    assert step['arguments'] == {'title': 'What is Leiden clustering?'}
    # `result` rides along with the name and the arguments: the Assistant shows
    # what each tool answered, and 'pending' is how the user learns this card is
    # a proposal rather than a board row. Asserted by key rather than whole-dict
    # so that widening a tool's return is not a failing server test.
    assert step['result']['id'] == 'n1'
    assert step['result']['pending'] is True


# This is a unit test.
def test_tool_classification_separates_proposing_from_mutating():
    from lodestar_brain.server import MUTATING_TOOLS, PROPOSING_TOOLS
    assert PROPOSING_TOOLS == {'create_card', 'update_card'}
    # An edit waits for the user now, so nothing mutates the board and the
    # browser is never told to adopt server state mid-conversation.
    assert MUTATING_TOOLS == set()


# This is an integration test.
@respx.mock
def test_chat_echo_reports_neither_flag():
    client = TestClient(fake_app())
    body = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'just talking'}]}).json()
    assert body['mutated'] is False
    assert body['proposed'] is False


# This is an integration test.
def test_a_turn_reports_what_it_cost_and_says_nothing_when_it_cannot():
    """`cost` rides alongside `usage`, on both chat routes.

    On the wire rather than computed in the browser: the model is chosen per
    request and the prices are a remote document, so a client doing this
    arithmetic would need its own price table and its own guess at which model
    answered. Here both are known for certain.

    The fake backend has no per-token bill, which makes it the case worth
    pinning: `cost` is 0.0 because that is *true* of a local model, not because
    the figure was missing. `test_pricing.py` owns the other direction — an
    unknown model or an unreachable price list reports None, and the Assistant
    then shows no figure at all.
    """
    client = TestClient(fake_app())
    body = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'just talking'}]}).json()
    assert body['usage'], 'the fake backend reports usage; cost rides with it'
    assert body['cost'] == 0.0

    # The stream's `done` event carries the same turn, so it must carry the same
    # cost — two routes reporting one turn differently is the drift _turn_json
    # exists to prevent.
    with client.stream('POST', '/agent/chat/stream', json={
            'messages': [{'role': 'user', 'content': 'just talking'}]}) as res:
        frames = [line for line in res.iter_lines() if line.startswith('data: ')]
    assert json.loads(frames[-1][6:])['cost'] == 0.0


# ---- POST /agent/transcribe ----------------------------------------------

WAV = b'RIFF....WAVEfake-pcm-bytes'


def b64(raw):
    return base64.b64encode(raw).decode()


# This is an integration test.
def test_transcribe_returns_text():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': b64(WAV), 'format': 'wav'})
    assert res.status_code == 200
    assert res.json() == {'text': FAKE_TRANSCRIPT}


# This is an integration test.
def test_transcribe_defaults_to_wav():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 200
    assert res.json()['text'] == FAKE_TRANSCRIPT


# This is an integration test.
def test_transcribe_rejects_malformed_base64():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': 'not!valid!base64!'})
    assert res.status_code == 400


# This is an integration test.
def test_transcribe_rejects_unsupported_format():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe',
                      json={'audio': b64(WAV), 'format': 'webm'})
    assert res.status_code == 400


# This is an integration test.
def test_transcribe_rejects_empty_audio():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': ''})
    assert res.status_code == 400


# This is an integration test.
def test_transcribe_requires_an_audio_field():
    client = TestClient(fake_app())
    assert client.post('/agent/transcribe', json={}).status_code == 422


# This is an integration test.
def test_transcribe_never_touches_the_board():
    # Transcription is stateless: no board read, no board write. respx with no
    # routes registered means any outbound call at all raises.
    with respx.mock:
        client = TestClient(fake_app())
        res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 200


# This is an integration test.
@respx.mock
def test_transcribe_forwards_the_picked_omni_model():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={
            'choices': [{'message': {'content': 'spoken words'}}]}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test')))
    res = client.post('/agent/transcribe', json={
        'audio': b64(WAV), 'format': 'wav', 'model': 'google/gemini-2.5-flash'})
    assert res.status_code == 200
    assert res.json() == {'text': 'spoken words'}
    assert json.loads(route.calls.last.request.content)['model'] == 'google/gemini-2.5-flash'


# This is an integration test.
@respx.mock
def test_every_transcription_uses_the_one_chat_completions_wire_format():
    """One wire format, as the module's own docstring states: audio rides in as an
    `input_audio` content part on a normal chat completion — whatever model the
    request names. A second branch that picked /audio/transcriptions for some
    models was reverted: that endpoint takes multipart form-data rather than this
    JSON body, and no test ever exercised it. respx fails an unmocked call, so
    mocking only /chat/completions is what enforces the single route."""
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={
            'choices': [{'message': {'content': 'spoken words'}}]}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test')))
    res = client.post('/agent/transcribe', json={
        'audio': b64(WAV), 'model': 'some/other-omni-model'})
    assert res.status_code == 200
    assert res.json() == {'text': 'spoken words'}
    assert route.called


# This is an integration test.
@respx.mock
def test_transcribe_maps_upstream_failure_to_502():
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(500, json={'error': 'nope'}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test')))
    res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 502


# This is an integration test.
@respx.mock
def test_a_model_that_drops_the_audio_is_reported_not_transcribed():
    # The nemotron omni free model answers an apology instead of a transcript
    # because its provider discards the audio. That apology must never be
    # returned as if it were speech — the caller has to learn the model is at
    # fault, and the detail has to say so.
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={'choices': [{'message': {
            'content': "I'm sorry, but I need the audio file to transcribe it."}}]}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test',
        omni_model='nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free')))
    res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 502
    detail = res.json()['detail'].lower()
    assert 'audio' in detail
    assert 'nemotron' in detail, 'the detail must name the model at fault'


# ---- Chat memory: chunks from assistant chat land in a per-board Chroma ---
# Offline: chroma_url='memory' (in-process client), so these tests need no
# Docker container. The HTTP path lives in test_chat_memory_server.py.

def memory_app(collection):
    return create_app(Settings(llm_provider='fake', embedder='fake',
                               transcriber='fake',
                               board_api_url='http://board.test',
                               chroma_url='memory',
                               chat_collection=str(collection)))


def stub_chat_record():
    """A board stub speaking the chat record (Session 7): POST echoes rows
    back with ids, GET has nothing recorded before the test. Chat reaches the
    Chroma index only through the record now, so a memory test without this
    stub records nothing — loudly in the logs, silently in its asserts."""
    counter = itertools.count(1)

    def echo(request):
        sent = json.loads(request.content)['messages']
        return httpx.Response(200, json={'messages': [
            {'id': next(counter), 'role': m['role'], 'content': m['content'],
             'createdAt': m.get('createdAt', 1754130000000)} for m in sent]})

    respx.get('http://board.test/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    respx.post('http://board.test/api/chat/messages').mock(side_effect=echo)


# This is an integration test.
@respx.mock
def test_chat_records_both_sides_and_recall_finds_them():
    stub_chat_record()
    client = TestClient(memory_app('chat-both-sides'))
    client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'the wifi password is hunter2'}]})
    res = client.post('/rag/recall', json={'text': 'wifi password', 'k': 4})
    assert res.status_code == 200
    matches = res.json()['matches']
    assert matches, 'chat exchange was not recorded'
    assert any('wifi password' in m['text'] for m in matches)
    roles = {m['metadata']['role'] for m in matches}
    assert {'user', 'assistant'} <= roles  # reply is recorded too (FAKE: echo)


# This is an integration test.
@respx.mock
def test_recall_orders_by_relevance():
    stub_chat_record()
    client = TestClient(memory_app('chat-ordering'))
    for text in ['the wifi password is hunter2',
                 'dentist appointment moved to friday']:
        client.post('/agent/chat', json={'messages': [
            {'role': 'user', 'content': text}]})
    res = client.post('/rag/recall', json={'text': 'dentist appointment', 'k': 1})
    assert 'dentist' in res.json()['matches'][0]['text']


# This is an integration test.
@respx.mock
def test_paired_stores_are_isolated_per_board():
    # Two brains, two collections — the board.db brain must never see chat
    # recorded by the board-3001.db brain, and vice versa.
    stub_chat_record()
    main = TestClient(memory_app('chat-board-3000'))
    test = TestClient(memory_app('chat-board-3001'))
    main.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'production secret rotation is tuesday'}]})
    # The positive half first, so this test can never pass vacuously with a
    # brain that records nothing at all.
    own = main.post('/rag/recall', json={'text': 'secret rotation', 'k': 5})
    assert any('rotation' in m['text'] for m in own.json()['matches'])
    res = test.post('/rag/recall', json={'text': 'secret rotation', 'k': 5})
    assert all('rotation' not in m['text'] for m in res.json()['matches'])


# This is an integration test.
@respx.mock
def test_unreachable_chroma_does_not_stop_the_brain_from_serving():
    # If the Docker Chroma is down, the brain must still boot and answer chat —
    # memory degrades to off rather than taking the whole service with it.
    app = create_app(Settings(llm_provider='fake', embedder='fake',
                              transcriber='fake',
                              board_api_url='http://board.test',
                              chroma_url='http://127.0.0.1:9',
                              chat_collection='chat-down'))
    client = TestClient(app)
    assert client.get('/health').json()['ok'] is True
    chat = client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'hello brain'}]})
    assert chat.status_code == 200
    res = client.post('/rag/recall', json={'text': 'hello', 'k': 3})
    assert res.status_code == 200
    assert res.json() == {'matches': [], 'memory': False}


# This is an integration test.
@respx.mock
def test_having_no_matches_and_having_no_memory_are_told_apart():
    """Both answer with an empty list, and they mean opposite things.

    "Nothing recorded about that" is a claim about the user's history;
    "chat memory is off" is a claim about the service. Reporting the second as
    the first sends someone hunting for a conversation the brain was never able
    to store — the same objection that made /rag/communities 404 rather than
    answer with an empty list.
    """
    off = TestClient(fake_app())          # default Settings: chroma_url is ''
    off.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'hello brain'}]})
    assert off.post('/rag/recall', json={'text': 'hello', 'k': 3}).json() == {
        'matches': [], 'memory': False}

    on = TestClient(memory_app('chat-nothing-said'))
    assert on.post('/rag/recall', json={'text': 'hello', 'k': 3}).json() == {
        'matches': [], 'memory': True}


# This is an integration test.
def test_recall_validates_its_body(tmp_path):
    client = TestClient(memory_app(tmp_path))
    assert client.post('/rag/recall', json={}).status_code == 422
    # k is bounded exactly as RecallChatArgs' already was. Unbounded, one
    # request reads out the whole collection — and this route is reachable from
    # the browser, while the tool is only reachable through the model.
    assert client.post('/rag/recall', json={'text': 'hi', 'k': 999}).status_code == 422
    assert client.post('/rag/recall', json={'text': 'hi', 'k': 0}).status_code == 422


# This is an integration test.
@respx.mock
def test_rag_reindex_says_whether_it_had_to_rebuild():
    respx.get('http://board.test/api/state').mock(return_value=board_state(
        [card('a', 'kubernetes pods scaling'), card('b', 'kubernetes pod limits')]))
    client = TestClient(fake_app())
    assert client.post('/rag/reindex').json() == {'cards': 2, 'rebuilt': True}
    # The same board again is not re-embedded. That is the fingerprint, and it
    # is what makes a rebuild-per-tool-call affordable with a real encoder.
    assert client.post('/rag/reindex').json() == {'cards': 2, 'rebuilt': False}
    # Community detection was removed on 2026-08-01, to be revisited. The route
    # 404s rather than answering [] — an empty list reads as "no themes found",
    # which is a claim about the board rather than about the feature.
    assert client.get('/rag/communities').status_code == 404


# --- POST /agent/chat/stream ------------------------------------------------
# The same turn, reported as it happens. It exists because nothing streamed
# before and the wait was a motionless "Thinking…" that read as a hang.

def sse_events(client, body, path='/agent/chat/stream'):
    """Collect an SSE response as [(event name, parsed data), …]."""
    events, name = [], None
    with client.stream('POST', path, json=body) as res:
        assert res.status_code == 200
        assert res.headers['content-type'].startswith('text/event-stream')
        for line in res.iter_lines():
            if line.startswith('event: '):
                name = line.removeprefix('event: ')
            elif line.startswith('data: '):
                events.append((name, json.loads(line.removeprefix('data: '))))
    return events


# This is an integration test — real route, real agent, real in-process Chroma;
# only the model, the embedder and the board's HTTP are stood in for.
@respx.mock
def test_the_streamed_turn_reports_each_tool_then_agrees_with_the_buffered_one():
    """This route exists to make the wait visible, not to be a second brain, so
    its `done` payload must be the buffered route's payload exactly.

    Two ways it goes wrong quietly, both asserted here. Tool output travels the
    same LangGraph channel as the model's own tokens, so an unfiltered token
    stream would paste a tool's JSON into the reply. And chat memory is recorded
    by the route, not by the agent — a second route is a second place to forget.
    """
    respx.post('http://board.test/api/proposals').mock(
        return_value=httpx.Response(200, json=card('n1', 'What is RRF?')))
    stub_chat_record()
    client = TestClient(memory_app('chat-stream'))
    events = sse_events(client, {'messages': [
        {'role': 'user', 'content': 'add: What is RRF?'}]})

    kinds = [name for name, _ in events]
    assert kinds.count('done') == 1 and kinds[-1] == 'done'
    assert kinds.index('step') < kinds.index('done'), 'the step arrived too late to be progress'
    step = next(data for name, data in events if name == 'step')
    assert step['tool'] == 'create_card' and step['result']['id'] == 'n1'

    # The tool is announced when it is *requested*, not only when it answers.
    # A web search runs for seconds, and without this the slowest stretch of a
    # research turn emits nothing at all — indistinguishable from a hang.
    assert kinds.index('calling') < kinds.index('step')
    assert next(data for name, data in events if name == 'calling') == {
        'tool': 'create_card', 'arguments': {'title': 'What is RRF?'}}

    done = events[-1][1]
    assert done['reply'] == 'FAKE: created "What is RRF?"'
    assert done['proposed'] is True and done['mutated'] is False
    assert done['steps'] == [step]
    # Including what it spent: this turn made two model calls, so a `done` that
    # reported one of them would understate every turn that used a tool.
    assert done['usage']['output_tokens'] > 0

    tokens = ''.join(data['text'] for name, data in events if name == 'token')
    assert 'n1' not in tokens, 'tool output must not stream as reply text'
    matches = client.post('/rag/recall', json={'text': 'RRF', 'k': 4}).json()['matches']
    assert {'user', 'assistant'} <= {m['metadata']['role'] for m in matches}


# This is an integration test.
@respx.mock
def test_a_stream_that_dies_says_so_instead_of_going_quiet(monkeypatch):
    """The headers are long gone by the time the model fails, so there is no
    status code left to fail with. Without an explicit event the browser just
    sees a short stream and sits on "Thinking…" forever — the exact hang this
    route was added to remove."""
    from lodestar_brain.agent import LodestarAgent

    async def boom(self, *args, **kwargs):
        raise RuntimeError('model exploded')
        yield  # never reached; makes boom an async generator

    monkeypatch.setattr(LodestarAgent, 'astream', boom)
    events = sse_events(TestClient(fake_app()),
                        {'messages': [{'role': 'user', 'content': 'hello'}]})
    assert events[-1][0] == 'error'
    assert 'model exploded' in events[-1][1]['message']


# --- which models this brain can serve --------------------------------------
# The browser sends a model with every chat turn, so a picker offering slugs the
# backend cannot load is a broken Assistant with no way out of it from the UI.

# This is an integration test.
def test_models_route_says_nothing_is_verified_on_a_remote_backend():
    """OpenRouter is a paid API with hundreds of models; probing it on every
    settings render would be absurd, so `verified` is False and the frontend's
    curated list stands. False must not read as "serves nothing"."""
    client = TestClient(create_app(Settings(llm_provider='openrouter',
                                            embedder='fake', transcriber='fake')))
    body = client.get('/agent/models').json()
    # `cli` is lifted out before the payload is compared, and it has to be: it
    # answers "which subscriptions could serve this board", so its values depend
    # on which binaries exist on the machine running the suite. Asserting its
    # *shape* is the part that is the same everywhere — the picker reads both
    # backends off it and would offer neither if a key went missing.
    cli = body.pop('cli')
    assert sorted(cli) == ['claude-cli', 'codex-cli']
    assert all(isinstance(known, bool) for known in cli.values())
    assert body == {'provider': 'openrouter', 'default': 'openai/gpt-5-nano',
                    'verified': False, 'models': []}


# This is an integration test.
@respx.mock
def test_models_route_lists_what_the_local_daemon_serves():
    respx.get('http://localhost:11434/api/tags').mock(
        return_value=httpx.Response(200, json={'models': [{'name': 'qwen3.5:2b'},
                                                          {'name': 'gemma4:e2b'}]}))
    client = TestClient(create_app(Settings(llm_provider='ollama',
                                            model='gemma4:e2b',
                                            embedder='fake', transcriber='fake')))
    body = client.get('/agent/models').json()
    assert body['provider'] == 'ollama' and body['verified'] is True
    assert body['models'] == ['gemma4:e2b', 'qwen3.5:2b']
    assert body['default'] == 'gemma4:e2b'


# This is an integration test.
@respx.mock
def test_a_local_daemon_that_is_down_claims_nothing_rather_than_an_empty_list():
    """A daemon that is not running is a normal state, not an error. Reporting
    verified=True with no models would empty the picker instead of leaving the
    presets in place."""
    respx.get('http://localhost:11434/api/tags').mock(
        side_effect=httpx.ConnectError('nope'))
    client = TestClient(create_app(Settings(llm_provider='ollama',
                                            embedder='fake', transcriber='fake')))
    body = client.get('/agent/models').json()
    assert body['verified'] is False and body['models'] == []


# This is an integration test.
@respx.mock
def test_the_models_route_reads_the_configured_base_url():
    """So the same setting reaches a daemon on another host without a code
    change — and so a wrong URL surfaces as an unverified backend rather than
    silently probing localhost."""
    respx.get('http://gpu.lan:11434/api/tags').mock(
        return_value=httpx.Response(200, json={'models': [{'name': 'x:1b'}]}))
    client = TestClient(create_app(Settings(
        llm_provider='ollama', embedder='fake', transcriber='fake',
        ollama_base_url='http://gpu.lan:11434/v1')))
    assert client.get('/agent/models').json()['models'] == ['x:1b']


# This is an integration test.
def test_openrouter_key_is_set_from_the_ui_and_never_echoed():
    """The key can be typed into the Assistant instead of edited into a file.

    Contract of the pair of routes:
    - GET /agent/key answers only {'configured': bool} — whether the agent's
      *effective* settings carry a non-empty OpenRouter key. The key itself is
      write-only: no response from either route may ever contain it.
    - POST /agent/key {'key': 'sk-…'} hands the agent new settings carrying the
      key (whitespace-trimmed).
    - POST /agent/key {'key': ''} clears the override and restores whatever the
      brain booted with — the env var, not the empty string, so a Docker stack
      configured by env cannot be un-configured by an accidental empty save.
    """
    secret = 'sk-or-test-123456'
    client = TestClient(fake_app())                    # boots with no key
    assert client.get('/agent/key').json() == {'configured': False}

    res = client.post('/agent/key', json={'key': f'  {secret}  '})
    assert res.status_code == 200
    assert res.json() == {'configured': True}
    assert secret not in res.text
    res = client.get('/agent/key')
    assert res.json() == {'configured': True}
    assert secret not in res.text

    assert client.post('/agent/key', json={'key': ''}).json() == {
        'configured': False}

    # Booted WITH a key: clearing restores the boot key, it does not erase it.
    keyed = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='fake',
        board_api_url='http://board.test', openrouter_api_key='sk-boot')))
    assert keyed.get('/agent/key').json() == {'configured': True}
    keyed.post('/agent/key', json={'key': secret})
    assert keyed.post('/agent/key', json={'key': ''}).json() == {
        'configured': True}
    assert 'sk-boot' not in keyed.get('/agent/key').text


# This is a unit test.
def test_reconfigure_swaps_settings_and_drops_the_graph_cache():
    """A key set at runtime must reach the model calls that follow. The agent
    caches one compiled graph per provider/model pair with the credential baked
    into the constructed chat model, so new settings that keep the cache would
    keep answering with the old (missing) key — configured in the UI, refused
    on the wire, and nothing raises."""
    from dataclasses import replace

    from lodestar_brain.agent.graph import LodestarAgent

    agent = LodestarAgent(settings=Settings(llm_provider='fake',
                                            embedder='fake'), tools=[])
    before = agent._graph(None, None)
    agent.reconfigure(replace(agent.settings, openrouter_api_key='sk-or-new'))
    assert agent.settings.openrouter_api_key == 'sk-or-new'
    assert agent._graph(None, None) is not before
