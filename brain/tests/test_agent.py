"""The agent's contract: it runs the tools the model asks for, recovers from
bad tool calls, stops at its step limit, and reports every step it took.

LangChain owns the loop now, so these tests assert behaviour and never the
framework's wording — an unknown-tool message belongs to create_agent and may
change under us.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from lodestar_brain.agent import (STEP_LIMIT_REPLY, AgentStep, LodestarAgent,
                                  _steps_from)
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat

SETTINGS = Settings(llm_provider='fake')


def echo_tool(recorder):
    @tool
    def echo(text: str) -> dict:
        """Echo back."""
        recorder.append(text)
        return {'echoed': text}
    return echo


@tool
def boom(text: str) -> dict:
    """Explode."""
    raise RuntimeError('kaput')


def call(name, args, i=0):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


def build(script, tools, **kw):
    return LodestarAgent(settings=SETTINGS, tools=tools, system_prompt='sys',
                         llm=FakeChat(script=script), **kw)


def test_agent_executes_tool_calls_then_replies():
    calls = []
    agent = build([call('echo', {'text': 'ping'}), AIMessage(content='done')],
                  [echo_tool(calls)])
    result = agent.run([{'role': 'user', 'content': 'echo ping'}])
    assert calls == ['ping']
    assert result.reply == 'done'
    assert [s.tool for s in result.steps] == ['echo']
    assert result.steps[0].arguments == {'text': 'ping'}
    assert result.steps[0].result == {'echoed': 'ping'}


def test_agent_recovers_from_an_unknown_tool_and_from_a_raising_tool():
    # create_agent owns the unknown-tool message, so assert recovery and the
    # reported step — never the wording. A raising tool is ours to handle: it
    # escapes the graph by default, and one unreachable board must not 500 the
    # whole chat turn.
    agent = build([call('missing', {}, 1), call('boom', {'text': 'x'}, 2),
                   AIMessage(content='recovered')],
                  [echo_tool([]), boom])
    result = agent.run([{'role': 'user', 'content': 'go'}])
    assert result.reply == 'recovered'
    assert [s.tool for s in result.steps] == ['missing', 'boom']
    assert 'not a valid tool' in str(result.steps[0].result)
    assert result.steps[1].result == {'error': 'kaput'}


def test_agent_stops_at_max_steps_and_still_reports_them():
    agent = build([call('echo', {'text': 'x'}, i) for i in range(8)],
                  [echo_tool([])], max_steps=2)
    result = agent.run([{'role': 'user', 'content': 'loop'}])
    assert result.reply == STEP_LIMIT_REPLY
    assert 'step limit' in result.reply
    assert len(result.steps) == 2


def test_an_unknown_provider_fails_when_the_agent_is_built():
    # create_app builds the agent at import time, so this is the boot-time
    # failure the no-auto-modes rule asks for — not a 500 on the first chat.
    with pytest.raises(ValueError, match='typo'):
        LodestarAgent(settings=Settings(llm_provider='typo'), tools=[])


def test_arun_matches_run():
    # The route is async; this is the path production actually takes.
    calls = []
    agent = build([call('echo', {'text': 'ping'}), AIMessage(content='done')],
                  [echo_tool(calls)])
    result = asyncio.run(agent.arun([{'role': 'user', 'content': 'echo ping'}]))
    assert calls == ['ping']
    assert result.reply == 'done'
    assert [s.tool for s in result.steps] == ['echo']
    assert result.steps[0].result == {'echoed': 'ping'}


def test_arun_survives_a_raising_tool():
    # Tool-error handling that is only wired up synchronously raises
    # NotImplementedError under ainvoke/astream — which is this test's point.
    agent = build([call('boom', {'text': 'x'}), AIMessage(content='recovered')],
                  [boom])
    result = asyncio.run(agent.arun([{'role': 'user', 'content': 'go'}]))
    assert result.reply == 'recovered'
    assert result.steps[0].result == {'error': 'kaput'}


def test_arun_stops_at_max_steps_and_still_reports_them():
    agent = build([call('echo', {'text': 'x'}, i) for i in range(8)],
                  [echo_tool([])], max_steps=2)
    result = asyncio.run(agent.arun([{'role': 'user', 'content': 'loop'}]))
    assert result.reply == STEP_LIMIT_REPLY
    assert len(result.steps) == 2


def test_steps_from_pairs_calls_with_results_and_decodes_json():
    messages = [HumanMessage(content='go'),
                AIMessage(content='', tool_calls=[
                    {'name': 'echo', 'args': {'text': 'a'}, 'id': 'x1'}]),
                ToolMessage(content='{"echoed": "a"}', tool_call_id='x1'),
                AIMessage(content='', tool_calls=[
                    {'name': 'echo', 'args': {'text': 'b'}, 'id': 'x2'}]),
                ToolMessage(content='not json', tool_call_id='x2'),
                AIMessage(content='done')]
    assert _steps_from(messages) == [
        AgentStep(tool='echo', arguments={'text': 'a'}, result={'echoed': 'a'}),
        AgentStep(tool='echo', arguments={'text': 'b'}, result='not json')]


def test_steps_from_ignores_a_call_with_no_result_yet():
    # The old loop appended a step only once the tool had run; a transcript cut
    # off at the step limit must not report a step that never happened.
    messages = [AIMessage(content='', tool_calls=[
        {'name': 'echo', 'args': {}, 'id': 'pending'}])]
    assert _steps_from(messages) == []


def test_the_model_picker_gets_one_compiled_graph_per_model():
    # create_agent binds its model at build time, but the Assistant view sends
    # a model per request — so there is a graph per pick, cached per process.
    # The key is (provider, model), not the slug alone: the picker can move
    # between the local daemon and the paid API, so a model name on its own is
    # not a whole destination. The provider falls back to the configured one,
    # which is why an unspecified pick keys on 'fake' here.
    agent = build([AIMessage(content='a'), AIMessage(content='b'),
                   AIMessage(content='c')], [echo_tool([])])
    agent.run([{'role': 'user', 'content': 'x'}])
    agent.run([{'role': 'user', 'content': 'x'}], model='anthropic/claude-sonnet-4.5')
    agent.run([{'role': 'user', 'content': 'x'}], model='anthropic/claude-sonnet-4.5')
    assert sorted(agent._graphs) == [('fake', ''),
                                     ('fake', 'anthropic/claude-sonnet-4.5')]


def test_the_same_slug_under_two_providers_gets_two_graphs():
    """A model name means something only to the backend serving it. Keyed on the
    slug alone, picking OpenRouter after Ollama would replay the graph compiled
    for the local daemon — and the reply would come from a model other than the
    one the picker names, with nothing on screen to contradict it."""
    agent = build([AIMessage(content='a'), AIMessage(content='b')],
                  [echo_tool([])])
    agent.run([{'role': 'user', 'content': 'x'}], model='shared-slug',
              provider='ollama')
    agent.run([{'role': 'user', 'content': 'x'}], model='shared-slug',
              provider='openrouter')
    # ('fake', '') is the graph the constructor builds so an unknown BRAIN_LLM
    # fails at boot rather than on the first chat; the two picks are the point.
    assert sorted(agent._graphs) == [('fake', ''),
                                     ('ollama', 'shared-slug'),
                                     ('openrouter', 'shared-slug')]
