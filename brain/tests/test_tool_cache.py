"""One turn, one answer per question — except where an answer must not be reused.

The cache exists because a turn asks the same thing twice: three tools that each
fetch `/api/state`, or a model that runs the same search again after the first
answer did not settle it. What it must never do is reuse the result of a call
that was a *request* — a proposal or a suggested edit — because then a user who
asked for two cards would be shown one.
"""
import httpx
import pytest
import respx
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from lodestar_brain import server
from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.middleware.cache import NEVER_CACHED
from lodestar_brain.tools.board import BoardClient, make_board_tools

BOARD = 'http://board.test'
SETTINGS = Settings(llm_provider='fake')


def call(name, args, i=0):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': f'c{i}'}])


# This is an integration test: a whole turn over a tool that counts its callers.
def test_the_same_question_asked_twice_in_a_turn_is_answered_once():
    """A repeat is served from the cache, and a different argument is not.

    Both halves matter. Without the first, the cache is not there; without the
    second, it is worse than not being there — a key that ignored the arguments
    would answer "what about the boiler" with what it found about the passport.
    The model must still see an answer for *every* call it made: a cached hit is
    re-addressed to the call that is asking, so the transcript has no tool
    request left hanging.

    And the scope is one turn, which the last assertion is about. The board can
    change between turns — the user is looking at it — so an answer that outlived
    the question it was given to would be the cache handing back yesterday's
    board with nothing to say it had.
    """
    asked: list[str] = []

    @tool
    def lookup(text: str) -> dict:
        """Look something up."""
        asked.append(text)
        return {'answer': f'the {text} is 42'}

    agent = LodestarAgent(
        settings=SETTINGS, tools=[lookup], system_prompt='sys',
        llm=FakeChat(script=[call('lookup', {'text': 'wifi'}, 0),
                             call('lookup', {'text': 'wifi'}, 1),
                             call('lookup', {'text': 'boiler'}, 2),
                             AIMessage(content='both are 42'),
                             call('lookup', {'text': 'wifi'}, 3),
                             AIMessage(content='still 42')]))
    result = agent.run([{'role': 'user', 'content': 'twice, then something else'}])

    assert asked == ['wifi', 'boiler'], 'the repeated call ran again'
    assert [s.tool for s in result.steps] == ['lookup'] * 3
    assert result.steps[0].result == result.steps[1].result
    assert result.steps[2].result != result.steps[1].result

    agent.run([{'role': 'user', 'content': 'and the wifi again?'}])
    assert asked == ['wifi', 'boiler', 'wifi'], (
        'an answer from the previous turn was reused in this one')


# This is an integration test: the two tools whose result is a request.
@respx.mock
def test_a_proposal_is_never_served_from_the_cache():
    """Ask for the same card twice and two proposals are filed.

    A cached proposal would return the first one's id without posting anything,
    so the second request would vanish and the user would be shown one card
    having asked for two — a cache silently discarding a request, which is worse
    than the HTTP call it saved.

    The exclusion is the confirmation gate restated, so it is asserted to *be*
    the confirmation gate: a tool that starts proposing must not have to be
    remembered here as well.
    """
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(
        200, json={'version': 1, 'cards': []}))
    proposals = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json={
            'id': 'p1', 'title': 'Book the movers', 'columnId': 'inbox',
            'type': 'task', 'category': '', 'importance': '', 'urgency': '',
            'tags': [], 'notes': ''}))
    agent = LodestarAgent(
        settings=SETTINGS, tools=make_board_tools(BoardClient(BOARD)),
        system_prompt='sys',
        llm=FakeChat(script=[call('create_card', {'title': 'Book the movers'}, 0),
                             call('create_card', {'title': 'Book the movers'}, 1),
                             AIMessage(content='I proposed it twice')]))
    result = agent.run([{'role': 'user', 'content': 'propose it, and again'}])

    assert proposals.call_count == 2
    assert [s.tool for s in result.steps] == ['create_card', 'create_card']
    assert NEVER_CACHED == server.PROPOSING_TOOLS, (
        'the never-cached set and the confirmation gate are the same tools')


if __name__ == '__main__':
    pytest.main([__file__])
