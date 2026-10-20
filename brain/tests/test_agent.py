"""How a finished turn is read back out of a transcript.

The loop that runs the tools belongs to the one agent (`widget2/graph.py`).
What stays here is the shared reader: a LangChain transcript becomes the
steps and the spend the route reports, and that reader has no opinion about
which agent produced the messages.
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lodestar_brain.result import AgentStep, _steps_from, _usage_from


# This is a unit test.
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


# This is a unit test.
def test_usage_sums_the_turns_model_calls_and_is_absent_when_unreported():
    # A turn with tools is several model calls, and each one re-sends the
    # transcript grown by the last tool's answer — so those input tokens really
    # are paid again and summing them is the bill, not double counting.
    spent = lambda i, o: {'input_tokens': i, 'output_tokens': o,
                          'total_tokens': i + o}
    messages = [HumanMessage(content='go'),
                AIMessage(content='', usage_metadata=spent(10, 2)),
                ToolMessage(content='{}', tool_call_id='x'),
                AIMessage(content='done', usage_metadata=spent(20, 5))]
    assert _usage_from(messages) == {'input_tokens': 30, 'output_tokens': 7,
                                     'total_tokens': 37}
    # None rather than zeros: a model that reports nothing and a turn that spent
    # nothing are different facts, and "0 tokens" is a measurement nobody made.
    assert _usage_from([AIMessage(content='done')]) is None


# This is a unit test.
def test_steps_from_ignores_a_call_with_no_result_yet():
    # A transcript cut off at the step limit must not report a step that never
    # happened: a step is appended only once the tool has run.
    messages = [AIMessage(content='', tool_calls=[
        {'name': 'echo', 'args': {}, 'id': 'pending'}])]
    assert _steps_from(messages) == []
