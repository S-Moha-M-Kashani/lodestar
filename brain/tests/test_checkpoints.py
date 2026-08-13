"""Durable threads: what survives a turn, and what must not be said twice.

The browser sends the *entire* transcript on every turn — that is the wire
contract and it does not move. A checkpointed thread already holds those
messages, so feeding the whole list back into it appends a second copy of every
one: `add_messages` keys on message id, and the browser's messages carry none.
The turn after that would carry three copies. These tests are about that hazard
and about the two ways of getting it wrong in the other direction — losing the
thread's own memory, and letting an unnamed turn write into a named chat.
"""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite import AsyncSqliteStore

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.server import create_app

SETTINGS = Settings(llm_provider='fake')


@tool
def lookup(text: str) -> dict:
    """Look something up."""
    return {'answer': f'the {text} is 42'}


def call(name, args, i=0):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


class RecordingChat(FakeChat):
    """Keeps every transcript it was handed, so a test can assert on what the
    *model* saw rather than on what came back."""

    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **kwargs)


@asynccontextmanager
async def durable(agent, path):
    """The lifespan, as create_app opens it: one sqlite file, both backends."""
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver, \
            AsyncSqliteStore.from_conn_string(str(path)) as store:
        await saver.setup()
        await store.setup()
        agent.attach(checkpointer=saver, store=store)
        try:
            yield saver
        finally:
            agent.attach(checkpointer=None, store=None)


def said(messages):
    return [m.content for m in messages if isinstance(m, HumanMessage)]


# This is an integration test (a real sqlite checkpointer on a temp file).
def test_a_resent_transcript_is_not_appended_twice(tmp_path):
    """The duplication hazard, on the exact shape the browser produces.

    Turn two re-sends turn one's messages because that is all the browser knows
    how to do. Only the suffix may reach the thread, and the turn's own
    `steps`/`usage` must describe this turn and not the thread's whole history —
    the checkpoint holds the previous turn's tool calls and token counts, and
    reporting them again would bill the user twice for one call.
    """
    chat = RecordingChat(script=[AIMessage(content='answer one'),
                                 AIMessage(content='answer two')])
    agent = LodestarAgent(settings=SETTINGS, tools=[lookup],
                          system_prompt='sys', llm=chat)

    async def run():
        async with durable(agent, tmp_path / 'cp.db') as saver:
            first = await agent.arun([{'role': 'user', 'content': 'hello'}],
                                     session_id='s1')
            second = await agent.arun(
                [{'role': 'user', 'content': 'hello'},
                 {'role': 'assistant', 'content': 'answer one'},
                 {'role': 'user', 'content': 'again'}], session_id='s1')
            state = await saver.aget({'configurable': {'thread_id': 's1'}})
            return first, second, state['channel_values']['messages']

    first, second, thread = asyncio.run(run())
    assert first.reply == 'answer one' and second.reply == 'answer two'
    assert said(thread) == ['hello', 'again'], 'the thread heard "hello" twice'
    assert [m.content for m in thread
            if isinstance(m, AIMessage)] == ['answer one', 'answer two']
    # The turn reports itself, not the thread: one model call each, and the
    # replies are the same length — a turn that summed the thread's usage would
    # bill the first answer again.
    assert first.usage['output_tokens'] > 0
    assert second.usage['output_tokens'] == first.usage['output_tokens']


# This is an integration test (a real sqlite checkpointer on a temp file).
def test_the_second_turn_sees_what_the_browser_no_longer_carries(tmp_path):
    """Resume is worth having only if the thread remembers more than the wire.

    A tool's answer never reaches the browser's transcript — it is a chip in the
    Assistant, not a message — so on turn two the model would be re-reading a
    reply whose evidence it can no longer see. The checkpoint is what puts the
    ToolMessage back in front of it, and this turn's `steps` must still name only
    this turn's calls.
    """
    chat = RecordingChat(script=[call('lookup', {'text': 'wifi password'}),
                                 AIMessage(content='the wifi password is 42'),
                                 AIMessage(content='no, it has not changed')])
    agent = LodestarAgent(settings=SETTINGS, tools=[lookup],
                          system_prompt='sys', llm=chat)

    async def run():
        async with durable(agent, tmp_path / 'cp.db'):
            await agent.arun([{'role': 'user', 'content': 'what is the wifi password'}],
                             session_id='s1')
            return await agent.arun(
                [{'role': 'user', 'content': 'what is the wifi password'},
                 {'role': 'assistant', 'content': 'the wifi password is 42'},
                 {'role': 'user', 'content': 'has it changed?'}], session_id='s1')

    second = asyncio.run(run())
    last = chat.seen[-1]
    assert [m.content for m in last if isinstance(m, ToolMessage)], (
        'the second turn lost the first turn\'s tool result')
    assert said(last).count('what is the wifi password') == 1
    # The step already reported on turn one is not reported again.
    assert second.steps == []
    assert second.reply == 'no, it has not changed'


# This is an integration test (a real sqlite checkpointer on a temp file).
def test_an_unsessioned_turn_never_lands_in_a_named_chat(tmp_path):
    """`session_id` is optional on the wire, and that must not mean "shared".

    A batch with no session is filed under Node's reserved `adhoc` chat, which is
    one *record*, not one conversation. Threading them together would let a curl
    read the last one's context — and, worse, an unnamed turn would grow the
    thread of whichever named chat it was mistaken for.
    """
    chat = RecordingChat(script=[AIMessage(content=str(i)) for i in range(4)])
    agent = LodestarAgent(settings=SETTINGS, tools=[lookup],
                          system_prompt='sys', llm=chat)

    async def run():
        async with durable(agent, tmp_path / 'cp.db') as saver:
            await agent.arun([{'role': 'user', 'content': 'in the chat'}],
                             session_id='s1')
            await agent.arun([{'role': 'user', 'content': 'anonymous one'}])
            await agent.arun([{'role': 'user', 'content': 'anonymous two'}])
            named = await saver.aget({'configurable': {'thread_id': 's1'}})
            return named['channel_values']['messages']

    thread = asyncio.run(run())
    assert said(thread) == ['in the chat']
    # And the two unnamed turns did not thread with each other either: the
    # second one was handed no trace of the first.
    assert said(chat.seen[-1]) == ['anonymous two']


# This is an integration test (the real app and its lifespan, over a temp file).
@respx.mock
def test_the_app_opens_the_file_and_a_chat_becomes_a_thread(tmp_path):
    """Everything above builds the durable state by hand; this is the wiring.

    The connections belong to the running service, so they are opened in the
    lifespan — which means a `TestClient` used as a context manager is the only
    thing here that exercises them at all. What the route contributes is the last
    link: `ChatBody.session_id` is the thread, so the chat the user reopened is
    the state the next turn resumes.
    """
    respx.post('http://board.test/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    path = tmp_path / 'brain-checkpoints.db'
    app = create_app(Settings(llm_provider='fake', embedder='fake',
                              transcriber='fake',
                              board_api_url='http://board.test',
                              checkpoint_db=str(path)))
    with TestClient(app) as client:
        first = client.post('/agent/chat', json={
            'messages': [{'role': 'user', 'content': 'hello brain'}],
            'session_id': 's-berlin'})
        second = client.post('/agent/chat', json={
            'messages': [{'role': 'user', 'content': 'hello brain'},
                         {'role': 'assistant', 'content': 'FAKE: hello brain'},
                         {'role': 'user', 'content': 'and again'}],
            'session_id': 's-berlin'})
    assert first.json()['reply'] == 'FAKE: hello brain'
    assert second.json()['reply'] == 'FAKE: and again'
    # The envelope is unchanged by any of this: one turn, its own spend.
    assert second.json()['steps'] == []
    assert second.json()['usage']['output_tokens'] > 0

    async def thread():
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            saved = await saver.aget({'configurable': {'thread_id': 's-berlin'}})
            return saved['channel_values']['messages']

    assert said(asyncio.run(thread())) == ['hello brain', 'and again']


if __name__ == '__main__':
    pytest.main([__file__])
