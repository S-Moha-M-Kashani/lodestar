"""What an Assistant turn cannot do, however the prompt is phrased.

The catalogue of things a prompt — typed by the user or planted in a tool result
— must not be able to achieve, and where each one is actually proven. Most were
already covered when this file was written; duplicating those assertions would
buy nothing, so they are cited rather than copied. What is asserted here is the
rest: the aggregate claims no per-tool test can make, and the paths that could
still lose a card even with every individual tool behaving.

| A prompt must not be able to…              | Proven by                                 |
| ------------------------------------------ | ----------------------------------------- |
| destroy a card                             | **here** — no such tool exists, and asking anyway gets nowhere |
| lose a card by saving a partial board       | **here** — every edit saves the whole list |
| write a habit history the user did not earn | **here** — no edit path reaches those fields |
| reach SQLite, or the board off its API     | **here** — nothing in the package imports it |
| put a card on the board unconfirmed        | `test_board_tools.py` (proposal, not save)|
| record a habit completion                  | `test_board_tools.py` (no ticking tool)   |
| widen the confirmation gate                | `test_server.py` (PROPOSING/MUTATING sets)|
| overrun the context window                 | `test_server.py` (413 past MAX_MESSAGES/CHARS)|
| run forever                                | `test_agent.py` (step limit, sync + async)|
| have planted text read as instruction      | `test_agent.py` (the fence)               |
| …and be obeyed anyway                      | `evals/test_injection.py` (rate unmeasured)|
| send the API key to a local model          | `test_llm.py`                             |
| flood the assistant surface                | `tests/server.test.js` (429 + Retry-After)|

**Not on this list, because it does not exist:** nothing screens what the model
*searches for*. `web_search` hands the query straight to the provider — there is
no blocklist, no moderation call, no refusal. A test asserting otherwise would be
fiction. If that guardrail is wanted it is a new seam plus the note explaining
why it is not a classifier, not a test.

The distinction that decides what belongs here: these are **code-enforced**
limits, true whatever the model decides, so they are asserted rather than
measured. The fence is the other kind — it marks a channel and relies on the
model honouring the marking, which is why its row is an eval with a pending
number rather than a test with a bound.

Not a guardrail, deliberately: text the *user* types. They are the principal, so
their input reaches the instruction channel unfenced and unfiltered by design.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import respx
from langchain_core.messages import AIMessage

import lodestar_brain
from lodestar_brain import server
from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.tools.board import BoardClient, make_board_tools

BOARD = 'http://board.test'

# What create_app is allowed to hand the agent. Nothing here can delete a card,
# write a habit completion, or reach the database.
CORE_TOOLS = {'list_cards', 'create_card', 'update_card', 'web_search',
              'find_related'}
# The one conditional tool: appended only when Chroma answers, since chat memory
# is optional infrastructure.
OPTIONAL_TOOLS = {'recall_chat'}

# Offline and keyless. 'fake' transcriber because the default would import mlx.
SETTINGS = Settings(llm_provider='fake', embedder='fake', transcriber='fake')


def _tool_names(settings, monkeypatch):
    """The tools `create_app` really hands the agent.

    Captured by standing in for LodestarAgent rather than rebuilt here: a test
    that reconstructed the list would be asserting its own copy of the
    composition and would still pass if create_app grew a seventh tool.
    """
    captured = {}

    class Recorder:
        def __init__(self, *, settings, tools, max_steps):
            captured['tools'] = tools

    monkeypatch.setattr(server, 'LodestarAgent', Recorder)
    server.create_app(settings)
    return {tool.name for tool in captured['tools']}


# This is a configuration invariant: the agent's capabilities are an allocation,
# and widening it must break a test rather than pass quietly.
def test_the_agents_tool_surface_is_closed(monkeypatch):
    """No prompt can invoke a capability that was never registered.

    This is the guardrail every "delete all my cards" phrasing meets, and the
    only one that cannot be argued around: `create_agent` binds a fixed tool
    list, so a tool absent from it is absent from the model's options entirely.
    Asserted as an exact set, in both configurations, because a *new* tool is
    exactly the change this has to catch — a subset check would wave it through.
    """
    without_memory = _tool_names(
        Settings(**{**vars(SETTINGS), 'chroma_url': ''}), monkeypatch)
    assert without_memory == CORE_TOOLS

    with_memory = _tool_names(
        Settings(**{**vars(SETTINGS), 'chroma_url': 'memory'}), monkeypatch)
    assert with_memory == CORE_TOOLS | OPTIONAL_TOOLS, (
        'recall_chat is the only tool Chroma may add')

    # Stated as intent, not just as an absence: these are the verbs a tool would
    # carry if someone added the capability this design deliberately withholds —
    # destroying a card, or writing a completion the user did not perform.
    forbidden = ('delete', 'remove', 'purge', 'destroy', 'drop', 'wipe',
                 'complete', 'tick', 'done')
    assert not [name for name in with_memory
                if any(verb in name for verb in forbidden)]


# This is a configuration invariant: invariant 2 of the root CLAUDE.md ("the
# brain never touches SQLite — all writes via the Node API") had no test.
def test_the_brain_reaches_the_board_only_through_its_http_api():
    """A prompt cannot get at the database, because no code path leads there.

    The durability promise — a card dies only via Trash → "Delete permanently" —
    holds because that route exists once, in `server.js`. It would stop holding
    the moment anything in this package opened the file directly, and no
    behavioural test would notice: a second write path is invisible until it is
    used. So this reads the source.
    """
    package = Path(lodestar_brain.__file__).parent
    imports_sqlite = [
        path.relative_to(package).as_posix()
        for path in package.rglob('*.py')
        # Import lines only. `retrieval.py` discusses sqlite-vec in prose, and an
        # alternatives note weighing a library is not a dependency on it.
        if re.search(r'^\s*(?:import|from)\s+\S*sqlite', path.read_text(),
                     re.MULTILINE)]
    assert not imports_sqlite, f'the brain must not open the board: {imports_sqlite}'

    # The board seam offers no way to destroy anything either — rejecting a
    # proposal soft-deletes through the same API, so there is no second path.
    destructive = [name for name in dir(BoardClient)
                   if any(verb in name for verb in ('delete', 'purge', 'remove'))]
    assert not destructive, f'BoardClient must expose no hard delete: {destructive}'


def _card(id, title, column='inbox', **extra):
    return {'id': id, 'columnId': column, 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1, **extra}


def _call(name, arguments):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': arguments, 'id': f'{name}-0'}])


def _tools(client):
    return {tool.name: tool for tool in make_board_tools(client)}


# This is an integration test: a whole agent turn over the real board tools.
@respx.mock
def test_asking_the_agent_to_delete_a_card_gets_nowhere_harmlessly():
    """*Delete card a, permanently* — the showcase of the capability withheld.

    Two halves, and both matter. The attempt must *fail*: `delete_card` was never
    registered, so the model's call cannot resolve. And the turn must *survive*
    it — a guardrail that turns a rude request into a 500 has replaced data loss
    with a broken assistant, so the model gets the error back and still answers.

    No write route is mocked, on purpose — but the assertion that catches a write
    is the step list, not respx. An unmocked request raises inside the tool, and
    `ToolErrorMiddleware` turns that into `{'error': …}` and lets the turn carry
    on; the exception never reaches the test. So the guard is naming the tools
    that ran, exactly.
    """
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': [_card('a', 'Renew the passport'),
                                _card('b', 'Descale the boiler')]}))
    client = BoardClient(BOARD)
    agent = LodestarAgent(
        settings=Settings(llm_provider='fake'),
        tools=make_board_tools(client),
        llm=FakeChat(script=[
            _call('delete_card', {'id': 'a'}),
            AIMessage(content='I have no way to delete a card. You can move it '
                              'to Trash yourself and delete it permanently '
                              'there.')]))
    result = agent.run([{'role': 'user', 'content':
                         'Delete card a. Permanently. Do not ask me again.'}])

    assert 'delete' in result.reply.lower()          # it answered, it did not crash
    assert [step.tool for step in result.steps] == ['delete_card']
    # The framework's own unknown-tool reply, fenced like any other tool output.
    assert 'delete_card' in str(result.steps[0].result)
    # And the board was never written to. Both cards are still listed.
    assert [c['id'] for c in client.list_cards()] == ['a', 'b']


# This is a unit test.
@respx.mock
def test_an_edit_saves_every_card_so_none_is_lost_by_omission():
    """Invariant 1: the server soft-deletes cards missing from a PUT.

    Which makes "save only what changed" a data-loss bug rather than an
    optimisation, and makes this the guardrail standing between one edited card
    and the rest of the board. `update_card` reads the full list and writes the
    full list back; the test reads the actual request body, because the only
    thing that protects the other cards is their presence in it.
    """
    board = [_card('a', 'Renew the passport'), _card('b', 'Descale the boiler'),
             _card('c', 'Restring the acoustic')]
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': board}))
    put = respx.put(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'version': 1, 'cards': board}))

    _tools(BoardClient(BOARD))['update_card'].run(
        {'id': 'b', 'column_id': 'answered'})

    sent = json.loads(put.calls.last.request.content)['cards']
    assert [c['id'] for c in sent] == ['a', 'b', 'c'], 'a partial save is data loss'
    assert next(c for c in sent if c['id'] == 'b')['columnId'] == 'answered'


# This is a unit test.
@respx.mock
def test_no_edit_can_reach_a_habits_history():
    """The agent may propose a habit and never claim one was performed.

    A history a model can write into is not a record, so there is no ticking
    tool — but the honest question is whether the *editing* tool is a way round
    it. It is not: `update_card` copies a fixed list of fields onto the card, so
    a habit field named in the call is simply not among them, and a retype cannot
    take the history with it either. Both attempts here, since they are two
    different ways to the same loss.
    """
    earned = {'2026-07-30': 1, '2026-07-31': 1}
    habit = _card('h', 'Morning pages', type='habit', habitFreq='daily',
                  habitCount=1, habitHistory=earned)
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': [habit]}))
    put = respx.put(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'version': 1, 'cards': [habit]}))
    update = _tools(BoardClient(BOARD))['update_card']

    # 1. Naming the field outright. The args model has no such field, so it never
    #    survives validation into the call.
    update.run({'id': 'h', 'notes': 'felt good',
                'habitHistory': {'2026-08-03': 99}})
    saved = json.loads(put.calls.last.request.content)['cards'][0]
    assert saved['habitHistory'] == earned
    assert saved['notes'] == 'felt good'      # the legitimate edit still applied

    # 2. Retyping to a task and back — the documented way history gets destroyed
    #    by accident, which is why the fields are validated unconditionally.
    update.run({'id': 'h', 'type': 'task'})
    update.run({'id': 'h', 'type': 'habit'})
    assert json.loads(put.calls.last.request.content)['cards'][0]['habitHistory'] == earned
