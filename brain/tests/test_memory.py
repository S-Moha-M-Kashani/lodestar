"""The agent's own notes: written where the user can see it, read as data.

Two rules travel together here and one test covers both, because separating them
would let each pass while the pair failed. A memory write must land in the turn's
`steps` — this board already refuses the agent a habit-completion tool for the
same reason, and a note it could file invisibly is that mistake one layer down.
And what comes back must reach the model *fenced*: the agent writes its notes
after reading web pages, so an instruction can be laundered through a memory and
reappear in the system prompt, which is where instructions live.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.store.memory import InMemoryStore

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.middleware.memory import facts_namespace
from lodestar_brain.middleware.untrusted import BEGIN, END
from lodestar_brain.tools.memory import make_memory_tool

FACT = 'She practises the santur on Sunday mornings.'


class RecordingChat(FakeChat):
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **kwargs)


def call(name, args, i=0):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


# This is an integration test: two turns over a real store, no checkpointer.
def test_a_remembered_fact_is_a_visible_step_and_comes_back_fenced():
    """The write is a chip; the read is data.

    The first turn's assertions are the visibility rule: `remember_fact` is an
    ordinary tool call, so it appears in `steps` exactly like a board read and
    the Assistant renders it without knowing anything about memory. The second
    turn's are the injection: a later conversation — a different turn, nothing
    shared but the store — is handed the note, and handed it inside the untrusted
    markers rather than as one more line of system prompt.
    """
    store = InMemoryStore()
    chat = RecordingChat(script=[call('remember_fact', {'fact': FACT}),
                                 AIMessage(content='noted'),
                                 AIMessage(content='I have a note about that')])
    agent = LodestarAgent(settings=Settings(llm_provider='fake'),
                          tools=[make_memory_tool()], system_prompt='sys',
                          llm=chat)
    agent.attach(store=store)

    async def two_turns():
        first = await agent.arun([{'role': 'user', 'content': 'remember that'}])
        second = await agent.arun([{'role': 'user', 'content': 'what do you know?'}])
        return first, second

    first, second = asyncio.run(two_turns())

    assert [s.tool for s in first.steps] == ['remember_fact']
    assert first.steps[0].result == {'remembered': FACT}
    # And it really is in the store, under this board's namespace.
    assert [item.value['fact']
            for item in store.search(facts_namespace(''))] == [FACT]

    system = chat.seen[-1][0]
    assert isinstance(system, SystemMessage)
    assert FACT in system.content, 'the note never reached the second turn'
    fenced = system.content[system.content.index(BEGIN):]
    assert FACT in fenced[:fenced.index(END)], (
        'a note is the agent quoting the world back to itself — it belongs in '
        'the data channel, not in the instructions')
    assert second.reply == 'I have a note about that'


if __name__ == '__main__':
    pytest.main([__file__])
