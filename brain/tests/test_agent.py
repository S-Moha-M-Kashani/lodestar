from lodestar_brain.agent.loop import Agent
from lodestar_brain.llm.base import AssistantTurn, ToolCall
from lodestar_brain.llm.fake import FakeProvider
from lodestar_brain.tools.base import Tool


def echo_tool(recorder):
    def echo(text: str) -> dict:
        recorder.append(text)
        return {'echoed': text}
    return Tool('echo', 'echo back', {'type': 'object', 'properties': {
        'text': {'type': 'string'}}, 'required': ['text']}, echo)


def test_agent_executes_tool_calls_then_replies():
    calls = []
    llm = FakeProvider(script=[
        AssistantTurn(tool_calls=[ToolCall(id='1', name='echo',
                                           arguments={'text': 'ping'})]),
        AssistantTurn(content='done'),
    ])
    agent = Agent(llm, [echo_tool(calls)])
    result = agent.run([{'role': 'user', 'content': 'echo ping'}])
    assert calls == ['ping']
    assert result.reply == 'done'
    assert [s.tool for s in result.steps] == ['echo']
    assert result.steps[0].result == {'echoed': 'ping'}


def test_agent_reports_unknown_tool_and_tool_errors():
    def boom(text: str) -> dict:
        raise RuntimeError('kaput')
    llm = FakeProvider(script=[
        AssistantTurn(tool_calls=[
            ToolCall(id='1', name='missing', arguments={}),
            ToolCall(id='2', name='boom', arguments={'text': 'x'})]),
        AssistantTurn(content='recovered'),
    ])
    agent = Agent(llm, [Tool('boom', 'explode', {'type': 'object', 'properties': {
        'text': {'type': 'string'}}, 'required': ['text']}, boom)])
    result = agent.run([{'role': 'user', 'content': 'go'}])
    assert result.reply == 'recovered'
    assert 'error' in result.steps[0].result
    assert 'kaput' in result.steps[1].result['error']


def test_agent_stops_at_max_steps():
    looping = FakeProvider(script=[
        AssistantTurn(tool_calls=[ToolCall(id=str(i), name='echo',
                                           arguments={'text': 'x'})])
        for i in range(5)])
    agent = Agent(looping, [echo_tool([])], max_steps=2)
    result = agent.run([{'role': 'user', 'content': 'loop'}])
    assert 'step limit' in result.reply
    assert len(result.steps) == 2
