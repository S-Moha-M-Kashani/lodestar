import base64
import json

import httpx
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app
from lodestar_brain.voice.fake import FAKE_TRANSCRIPT


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


@respx.mock
def test_chat_accepts_the_providers_the_picker_offers_and_refuses_the_rest():
    """The Assistant's provider selector rides along on every chat turn, so the
    route is the boundary that has to reject a provider this brain cannot serve.
    A fake brain answers regardless — the offline contract belongs to the server,
    not to a browser deciding when to leave the field out."""
    client = TestClient(fake_app())
    for provider in ('ollama', 'openrouter'):
        res = client.post('/agent/chat', json={
            'messages': [{'role': 'user', 'content': 'hello brain'}],
            'provider': provider})
        assert res.status_code == 200, provider
        assert res.json()['reply'] == 'FAKE: hello brain'
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'hello brain'}],
        'provider': 'anthropic'})
    assert res.status_code == 422


@respx.mock
def test_chat_add_proposes_a_card_without_mutating_the_board():
    # create_question now proposes; the board is unchanged until the user
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
    assert step['tool'] == 'create_question'
    assert step['arguments'] == {'title': 'What is Leiden clustering?'}
    # `result` rides along with the name and the arguments: the Assistant shows
    # what each tool answered, and 'pending' is how the user learns this card is
    # a proposal rather than a board row. Asserted by key rather than whole-dict
    # so that widening a tool's return is not a failing server test.
    assert step['result']['id'] == 'n1'
    assert step['result']['pending'] is True


def test_tool_classification_separates_proposing_from_mutating():
    from lodestar_brain.server import MUTATING_TOOLS, PROPOSING_TOOLS
    assert PROPOSING_TOOLS == {'create_question'}
    assert MUTATING_TOOLS == {'update_question'}
    # An edit still applies immediately, so it must stay a mutation.
    assert 'update_question' not in PROPOSING_TOOLS


@respx.mock
def test_chat_echo_reports_neither_flag():
    client = TestClient(fake_app())
    body = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'just talking'}]}).json()
    assert body['mutated'] is False
    assert body['proposed'] is False


# ---- POST /agent/transcribe ----------------------------------------------

WAV = b'RIFF....WAVEfake-pcm-bytes'


def b64(raw):
    return base64.b64encode(raw).decode()


def test_transcribe_returns_text():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': b64(WAV), 'format': 'wav'})
    assert res.status_code == 200
    assert res.json() == {'text': FAKE_TRANSCRIPT}


def test_transcribe_defaults_to_wav():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 200
    assert res.json()['text'] == FAKE_TRANSCRIPT


def test_transcribe_rejects_malformed_base64():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': 'not!valid!base64!'})
    assert res.status_code == 400


def test_transcribe_rejects_unsupported_format():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe',
                      json={'audio': b64(WAV), 'format': 'webm'})
    assert res.status_code == 400


def test_transcribe_rejects_empty_audio():
    client = TestClient(fake_app())
    res = client.post('/agent/transcribe', json={'audio': ''})
    assert res.status_code == 400


def test_transcribe_requires_an_audio_field():
    client = TestClient(fake_app())
    assert client.post('/agent/transcribe', json={}).status_code == 422


def test_transcribe_never_touches_the_board():
    # Transcription is stateless: no board read, no board write. respx with no
    # routes registered means any outbound call at all raises.
    with respx.mock:
        client = TestClient(fake_app())
        res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 200


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


@respx.mock
def test_every_transcription_uses_the_one_chat_completions_wire_format():
    """One wire format, as the module's own docstring states: audio rides in as an
    `input_audio` content part on a normal chat completion. A second branch that
    sniffed `openai/whisper-` out of the model name and posted JSON to
    /audio/transcriptions was reverted — that slug is absent from OpenRouter's
    published catalogue (measured 2026-07-31: 337 models, no whisper entry), the
    OpenAI-compatible transcription endpoint takes multipart form-data rather than
    this JSON body, and no test ever exercised it. respx fails an unmocked call,
    so mocking only /chat/completions is what enforces the single route."""
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={
            'choices': [{'message': {'content': 'spoken words'}}]}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test')))
    res = client.post('/agent/transcribe', json={
        'audio': b64(WAV), 'model': 'openai/whisper-large-v3-turbo'})
    assert res.status_code == 200
    assert res.json() == {'text': 'spoken words'}
    assert route.called


@respx.mock
def test_transcribe_maps_upstream_failure_to_502():
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(500, json={'error': 'nope'}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', transcriber='openrouter',
        openrouter_api_key='sk-test', board_api_url='http://board.test')))
    res = client.post('/agent/transcribe', json={'audio': b64(WAV)})
    assert res.status_code == 502


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


@respx.mock
def test_chat_records_both_sides_and_recall_finds_them():
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


@respx.mock
def test_recall_orders_by_relevance():
    client = TestClient(memory_app('chat-ordering'))
    for text in ['the wifi password is hunter2',
                 'dentist appointment moved to friday']:
        client.post('/agent/chat', json={'messages': [
            {'role': 'user', 'content': text}]})
    res = client.post('/rag/recall', json={'text': 'dentist appointment', 'k': 1})
    assert 'dentist' in res.json()['matches'][0]['text']


@respx.mock
def test_paired_stores_are_isolated_per_board():
    # Two brains, two collections — the board.db brain must never see chat
    # recorded by the board-3001.db brain, and vice versa.
    main = TestClient(memory_app('chat-board-3000'))
    test = TestClient(memory_app('chat-board-3001'))
    main.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'production secret rotation is tuesday'}]})
    res = test.post('/rag/recall', json={'text': 'secret rotation', 'k': 5})
    assert all('rotation' not in m['text'] for m in res.json()['matches'])


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


def test_recall_requires_a_text_field(tmp_path):
    client = TestClient(memory_app(tmp_path))
    assert client.post('/rag/recall', json={}).status_code == 422


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
    client = TestClient(memory_app('chat-stream'))
    events = sse_events(client, {'messages': [
        {'role': 'user', 'content': 'add: What is RRF?'}]})

    kinds = [name for name, _ in events]
    assert kinds.count('done') == 1 and kinds[-1] == 'done'
    assert kinds.index('step') < kinds.index('done'), 'the step arrived too late to be progress'
    step = next(data for name, data in events if name == 'step')
    assert step['tool'] == 'create_question' and step['result']['id'] == 'n1'

    # The tool is announced when it is *requested*, not only when it answers.
    # A web search runs for seconds, and without this the slowest stretch of a
    # research turn emits nothing at all — indistinguishable from a hang.
    assert kinds.index('calling') < kinds.index('step')
    assert next(data for name, data in events if name == 'calling') == {
        'tool': 'create_question', 'arguments': {'title': 'What is RRF?'}}

    done = events[-1][1]
    assert done['reply'] == 'FAKE: created "What is RRF?"'
    assert done['proposed'] is True and done['mutated'] is False
    assert done['steps'] == [step]

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

def test_models_route_says_nothing_is_verified_on_a_remote_backend():
    """OpenRouter is a paid API with hundreds of models; probing it on every
    settings render would be absurd, so `verified` is False and the frontend's
    curated list stands. False must not read as "serves nothing"."""
    client = TestClient(create_app(Settings(llm_provider='openrouter',
                                            embedder='fake', transcriber='fake')))
    body = client.get('/agent/models').json()
    assert body == {'provider': 'openrouter', 'default': 'openai/gpt-5-nano',
                    'verified': False, 'models': []}


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
