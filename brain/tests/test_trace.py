"""The developer trace: the exact envelope a turn handed the model.

Lodestar has never had one. `assistant.db` holds a two-role transcript — what
the user said and what came back — while the system prompt, the tool calls and
the tool results live only inside the brain for the length of a turn. A
developer debugging "why did it answer that?" could see neither, and the one
thing they must never be handed is a *reconstruction*: a system prompt guessed
from the browser's messages is a plausible story about a request nobody made.

Contract under test:

- `TurnTrace` + `TraceCollector` capture the message list at the moment it is
  handed to the model (`on_chat_model_start`), which is the only place that
  list exists. The captured order is the model's, not a rebuild.
- One turn is one record: a stable trace id, the session and board it belongs
  to, a status, timestamps, and ordered `system` / `human` / `ai` / `tool`
  entries carrying tool calls and results.
- A turn that fails records the failure and no answer; a turn cut off at the
  step limit is `interrupted`, not `completed`.
- The brain posts the record to the board — in flight when the turn starts, and
  again when it settles — and only when `BRAIN_TRACE` says to. Off by default,
  and an unknown value raises at boot like every other backend seam.
- Tracing changes nothing a user sees: the reply stream is byte-for-byte the
  turn it always was.
"""
import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.agent.trace import TraceCollector, TurnTrace
from lodestar_brain.config import Settings, load_settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.server import create_app

BOARD = 'http://board.test'
SETTINGS = Settings(llm_provider='fake')


@tool
def weather(city: str) -> dict:
    """The weather somewhere."""
    return {'city': city, 'sky': 'clear'}


def call(name, args, i=0):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


def build(script, tools=(), **kw):
    return LodestarAgent(settings=SETTINGS, tools=list(tools),
                         system_prompt='you are lodestar',
                         llm=FakeChat(script=script), **kw)


def roles(record):
    return [e['role'] for e in record['entries']]


# This is a unit test.
def test_the_collector_captures_the_list_the_model_was_given():
    """The envelope, not a rebuild. The handler is handed the exact list the
    model is about to be called with, system message included."""
    trace = TurnTrace(session_id='chat-1')
    collector = TraceCollector(trace)
    collector.on_chat_model_start(
        {}, [[SystemMessage('you are lodestar'), HumanMessage('hello')]])
    trace.settle(AIMessage('hi there'))
    record = trace.as_dict()

    assert roles(record) == ['system', 'human', 'ai']
    assert [e['content'] for e in record['entries']] == [
        'you are lodestar', 'hello', 'hi there']
    # Numbered in the order the model saw them, so a renderer never has to
    # decide an order of its own.
    assert [e['seq'] for e in record['entries']] == [0, 1, 2]
    assert record['status'] == 'completed'
    assert record['session_id'] == 'chat-1'
    assert record['trace_id']
    assert record['started_at'] <= record['ended_at']


# This is a unit test.
def test_a_tool_round_is_captured_with_its_arguments_and_its_answer():
    """The whole tape: the call the model made, what the tool answered, and the
    reply it then wrote — in that order, with the pairing preserved."""
    trace = TurnTrace(session_id='chat-2')
    collector = TraceCollector(trace)
    # The LAST envelope of a turn is the fullest one: it holds everything the
    # model had in front of it when it wrote the answer.
    collector.on_chat_model_start({}, [[SystemMessage('sys'), HumanMessage('weather in bern?')]])
    collector.on_chat_model_start({}, [[
        SystemMessage('sys'), HumanMessage('weather in bern?'),
        call('weather', {'city': 'bern'}),
        ToolMessage(content='{"sky": "clear"}', tool_call_id='c0', name='weather'),
    ]])
    trace.settle(AIMessage('clear skies in bern'))
    record = trace.as_dict()

    assert roles(record) == ['system', 'human', 'ai', 'tool', 'ai']
    assert record['entries'][2]['metadata']['tool_calls'] == [
        {'name': 'weather', 'args': {'city': 'bern'}, 'id': 'c0'}]
    assert record['entries'][3]['metadata']['tool_call_id'] == 'c0'
    assert record['entries'][3]['metadata']['name'] == 'weather'
    assert record['entries'][4]['content'] == 'clear skies in bern'


# This is a unit test.
def test_a_failed_turn_keeps_what_happened_and_invents_no_answer():
    trace = TurnTrace(session_id='chat-3')
    TraceCollector(trace).on_chat_model_start({}, [[SystemMessage('sys'),
                                                    HumanMessage('go')]])
    trace.fail('upstream exploded')
    record = trace.as_dict()

    assert record['status'] == 'failed'
    assert record['error'] == 'upstream exploded'
    assert roles(record) == ['system', 'human']       # no fabricated 'ai'
    assert record['ended_at'] is not None


# This is a unit test.
def test_an_unfinished_turn_reads_as_in_flight_until_it_settles():
    trace = TurnTrace(session_id='chat-4')
    assert trace.as_dict()['status'] == 'in_flight'
    assert trace.as_dict()['ended_at'] is None
    trace.interrupt()
    assert trace.as_dict()['status'] == 'interrupted'


# This is an integration test.
def test_the_agent_captures_the_envelope_it_really_sent():
    """The end-to-end version of the first two tests, through a real graph run:
    what the trace shows is what `FakeChat` was actually called with, in the
    same order — which is what makes 'not a reconstruction' testable rather
    than asserted."""
    seen: list[list] = []

    class Recording(FakeChat):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    agent = LodestarAgent(settings=SETTINGS, tools=[weather],
                          system_prompt='you are lodestar',
                          llm=Recording(script=[call('weather', {'city': 'bern'}),
                                                AIMessage('clear skies')]))
    trace = TurnTrace(session_id='chat-5')
    result = agent.run([{'role': 'user', 'content': 'weather in bern?'}], trace=trace)
    record = trace.as_dict()

    assert result.reply == 'clear skies'
    assert roles(record) == ['system', 'human', 'ai', 'tool', 'ai']
    # The projection the page renders is the projection of the last real call,
    # plus the answer that call produced. Compared against what the model was
    # handed, so a renderer that reordered or invented an entry fails here.
    handed = [(m.type, m.content) for m in seen[-1]]
    shown = [(e['role'], e['content']) for e in record['entries'][:-1]]
    assert [(t.replace('human', 'human'), c) for t, c in handed] == shown
    assert record['entries'][-1]['content'] == 'clear skies'


# This is an integration test.
def test_a_step_limited_turn_is_interrupted_rather_than_completed():
    agent = LodestarAgent(settings=SETTINGS, tools=[weather],
                          system_prompt='sys', max_steps=1,
                          llm=FakeChat(script=[call('weather', {'city': 'a'}, 0),
                                               call('weather', {'city': 'b'}, 1),
                                               call('weather', {'city': 'c'}, 2)]))
    trace = TurnTrace(session_id='chat-6')
    agent.run([{'role': 'user', 'content': 'loop'}], trace=trace)
    assert trace.as_dict()['status'] == 'interrupted'


# This is a configuration invariant.
def test_brain_trace_is_off_by_default_and_an_unknown_value_raises():
    assert load_settings({}).trace == 'off'
    assert load_settings({'BRAIN_TRACE': 'board'}).trace == 'board'
    with pytest.raises(ValueError):
        load_settings({'BRAIN_TRACE': 'yes'})


def app_for(trace: str):
    return create_app(Settings(llm_provider='fake', embedder='fake',
                               chroma_url='memory', board_api_url=BOARD,
                               url_safety='fake', trace=trace))


# This is an integration test.
@respx.mock
def test_the_stream_route_files_a_trace_in_flight_and_again_when_it_settles():
    posted: list[dict] = []
    respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
        return_value=httpx.Response(200, json={'messages': []}))

    def record(request):
        posted.append(httpx.Response(200, json={'ok': True}) and
                      __import__('json').loads(request.content))
        return httpx.Response(200, json={'ok': True})

    respx.post(f'{BOARD}/api/trace').mock(side_effect=record)

    with TestClient(app_for('board')) as client:
        with client.stream('POST', '/agent/chat/stream',
                           json={'messages': [{'role': 'user', 'content': 'hello'}],
                                 'session_id': 'chat-7'}) as res:
            body = ''.join(res.iter_text())

    # The turn itself is untouched: same events, same reply.
    assert 'event: done' in body and 'FAKE: hello' in body
    assert len(posted) == 2, 'one record in flight, one when it settled'
    assert posted[0]['status'] == 'in_flight'
    assert posted[1]['status'] == 'completed'
    # One turn is ONE record — the second write updates the first.
    assert posted[0]['trace_id'] == posted[1]['trace_id']
    assert posted[1]['session_id'] == 'chat-7'
    assert roles(posted[1])[:2] == ['system', 'human']
    assert posted[1]['entries'][-1]['content'] == 'FAKE: hello'
    assert posted[1]['usage']['total_tokens'] > 0


# This is an integration test.
@respx.mock
def test_tracing_off_posts_nothing():
    respx.post(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    route = respx.post(f'{BOARD}/api/trace').mock(
        return_value=httpx.Response(200, json={'ok': True}))

    with TestClient(app_for('off')) as client:
        res = client.post('/agent/chat', json={
            'messages': [{'role': 'user', 'content': 'hello'}]})
    assert res.status_code == 200
    assert not route.called, 'a brain with tracing off must file nothing'
