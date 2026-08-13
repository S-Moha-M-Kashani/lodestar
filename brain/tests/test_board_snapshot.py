"""One turn, one board fetch — for every tool that reads the board.

`list_cards` fetched `/api/state`, `find_related` fetched it again to rebuild its
index, and `daily_recap` fetched it a third time. Nothing changed between them
and the user waited for all three. `middleware/cache.py` cannot help: it collapses
the *same* tool asked the same question twice, and these are three different keys.

The tools under test are the ones `create_app` really built, captured by standing
in for the agent — a test that assembled its own three would still pass with the
composition handing each tool a client of its own, which is the bug.
"""
import asyncio

import httpx
import respx
from langchain_core.messages import AIMessage

from lodestar_brain import server
from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat

BOARD = 'http://board.test'


def card(id, title):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1}


def call(name, args, i):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


def composed_tools(monkeypatch):
    """The tool list `create_app` hands the agent, with its wiring intact."""
    captured = {}

    class Recorder:
        def __init__(self, *, settings, tools, max_steps):
            captured['tools'] = tools

    monkeypatch.setattr(server, 'LodestarAgent', Recorder)
    server.create_app(Settings(llm_provider='fake', embedder='fake',
                               transcriber='fake', board_api_url=BOARD,
                               chroma_url=''))
    return captured['tools']


def turn(tools, asked):
    """One agent turn that reaches for all three board readers."""
    agent = LodestarAgent(
        settings=Settings(llm_provider='fake'), tools=tools,
        llm=FakeChat(script=[call('list_cards', {}, 0),
                             call('find_related', {'text': 'passport'}, 1),
                             call('daily_recap', {'day': 'today'}, 2),
                             AIMessage(content='here is what I found')]))
    return asyncio.run(agent.arun([{'role': 'user', 'content': asked}]))


# This is an integration test: a whole turn over the tools create_app composed.
@respx.mock
def test_three_tools_in_one_turn_fetch_the_board_once(monkeypatch):
    """Three board readers, one `/api/state`.

    And the scope is the turn, which the last assertion is about: the user is
    looking at this board and may move a card between two questions, so an
    answer that outlived the question it was fetched for would be the snapshot
    handing back a board the user can see is no longer there. Only the agent is
    unable to race it — neither of its writing tools writes to `cards`.
    """
    state = respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(
        200, json={'version': 1, 'cards': [card('a', 'Renew the passport')]}))
    respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    tools = composed_tools(monkeypatch)

    result = turn(tools, 'what is on the board about the passport?')

    # Not a vacuous pass: all three ran, and the first one really answered from
    # the board rather than erroring into a fenced {'error': …}.
    assert [step.tool for step in result.steps] == [
        'list_cards', 'find_related', 'daily_recap']
    assert 'Renew the passport' in str(result.steps[0].result)
    assert state.call_count == 1, (
        'three tools reading one board must share one fetch')

    turn(tools, 'and now?')
    assert state.call_count == 2, (
        'a new turn re-reads the board — the snapshot bounds staleness at one '
        'turn, and the user edits their own board between them')
