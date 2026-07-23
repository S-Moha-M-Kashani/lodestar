import json

import httpx
import respx

from lodestar_brain.llm.base import AssistantTurn, ToolCall
from lodestar_brain.llm.fake import FakeProvider
from lodestar_brain.llm.openrouter import OpenRouterProvider


@respx.mock
def test_openrouter_parses_content_and_tool_calls():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={
            'choices': [{'message': {
                'content': None,
                'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
                    'name': 'web_search', 'arguments': '{"query": "leiden algorithm"}'}}],
            }}],
        }))
    llm = OpenRouterProvider(api_key='sk-test',
                             base_url='https://openrouter.ai/api/v1',
                             default_model='openai/gpt-4o-mini')
    turn = llm.chat([{'role': 'user', 'content': 'find leiden'}],
                    tools=[{'type': 'function', 'function': {'name': 'web_search'}}])
    sent = route.calls.last.request
    assert sent.headers['authorization'] == 'Bearer sk-test'
    assert turn.tool_calls == [ToolCall(id='call_1', name='web_search',
                                        arguments={'query': 'leiden algorithm'})]
    assert turn.content is None


@respx.mock
def test_openrouter_plain_reply_and_model_override():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(200, json={
            'choices': [{'message': {'content': 'hello'}}]}))
    llm = OpenRouterProvider('k', 'https://openrouter.ai/api/v1', 'openai/gpt-4o-mini')
    turn = llm.chat([{'role': 'user', 'content': 'hi'}], model='anthropic/claude-sonnet-4.5')
    assert json.loads(route.calls.last.request.content)['model'] == 'anthropic/claude-sonnet-4.5'
    assert turn.content == 'hello'
    assert turn.tool_calls == []


def test_fake_provider_scripted():
    llm = FakeProvider(script=[AssistantTurn(content='first'), AssistantTurn(content='second')])
    assert llm.chat([{'role': 'user', 'content': 'x'}]).content == 'first'
    assert llm.chat([{'role': 'user', 'content': 'x'}]).content == 'second'


def test_fake_provider_add_heuristic():
    llm = FakeProvider()
    msgs = [{'role': 'user', 'content': 'add: What is Leiden clustering?'}]
    turn = llm.chat(msgs)
    assert turn.tool_calls and turn.tool_calls[0].name == 'create_question'
    assert turn.tool_calls[0].arguments == {'title': 'What is Leiden clustering?'}
    # after the tool ran, it must produce a final text reply
    msgs += [{'role': 'assistant', 'content': None},
             {'role': 'tool', 'tool_call_id': 'fake-1', 'content': '{}'}]
    assert 'created' in llm.chat(msgs).content


def test_fake_provider_echoes():
    llm = FakeProvider()
    assert llm.chat([{'role': 'user', 'content': 'hello brain'}]).content == 'FAKE: hello brain'
