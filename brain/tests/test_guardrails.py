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
| change a card at all                        | **here** — the client has no write method  |
| apply an edit without the user saving it    | `test_edit_suggestions.py` (a suggestion, not a write) |
| write a habit history the user did not earn | `test_edit_suggestions.py` (no field reaches it) |
| reach SQLite, or the board off its API     | **here** — one file opens one file, and it is not a board |
| put a card on the board unconfirmed        | `test_widget2_write.py` (proposal, not save)|
| record a habit completion                  | **here** — no ticking tool, and the prompt says so |
| widen the confirmation gate                | `test_server.py` (PROPOSING/MUTATING sets)|
| overrun the context window                 | `test_server.py` (413 past MAX_MESSAGES/CHARS)|
| run forever                                | `test_trace.py` (step limit)              |
| have planted text read as instruction      | `test_widget2.py` (the fence)             |
| …and be obeyed anyway                      | `evals/test_injection.py` (rate unmeasured)|
| send the API key to a local model          | `test_llm.py`                             |
| flood the assistant surface                | `tests/server.test.js` (429 + Retry-After)|
| search the web or offer a link             | **here** — no such tool exists            |

A withheld capability the user is never told about is indistinguishable from a
broken one, so one row of this catalogue is about the *telling* rather than the
withholding: **here** — the prompt names the delete limit and what to do instead.

**Still not on this list:** the agent cannot search the web, so there is no
query to screen and no link to offer. The reputation checker (`safety.py`)
remains a named backend for anyone who builds it; a turn never consults it.

The distinction that decides what belongs here: these are **code-enforced**
limits, true whatever the model decides, so they are asserted rather than
measured. The fence is the other kind — it marks a channel and relies on the
model honouring the marking, which is why its row is an eval with a pending
number rather than a test with a bound.

Not a guardrail, deliberately: text the *user* types. They are the principal, so
their input reaches the instruction channel unfenced and unfiltered by design.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import lodestar_brain
from lodestar_brain import server
from lodestar_brain.board.client import BoardClient
from lodestar_brain.config import Settings, load_settings
from lodestar_brain.llm import FakeChat
from lodestar_brain.widget2 import Widget2Agent
from lodestar_brain.widget2.prompt import SYSTEM_PROMPT
from lodestar_brain.widget2.tools import make_tools

BOARD = 'http://board.test'

# What the one agent holds. Read off `/health`, which reads the agent's own
# tools, so a test that rebuilt the list would still pass if the service grew
# another one. Chat memory does not add a tool: the agent cannot recall other
# conversations.
HELD = {'find_cards', 'create_card', 'update_card'}

# Offline and keyless. 'fake' transcriber because the default would import mlx.
SETTINGS = Settings(llm_provider='fake', embedder='fake', transcriber='fake')


# This is a configuration invariant: the agent's capabilities are an allocation,
# and widening it must break a test rather than pass quietly.
def test_the_agents_tool_surface_is_closed():
    """No prompt can invoke a capability that was never registered.

    This is the guardrail every "delete all my cards" phrasing meets, and the
    only one that cannot be argued around: the agent binds a fixed tool list, so
    a tool absent from it is absent from the model's options entirely. Asserted
    as an exact set, with and without the chat index, because a *new* tool is
    exactly the change this has to catch — a subset check would wave it through,
    and the index must not smuggle a recall tool back in.
    """
    for chroma in ('', 'memory'):
        app = server.create_app(Settings(**{**vars(SETTINGS), 'chroma_url': chroma}))
        body = TestClient(app).get('/health').json()
        assert body['agent'] == 'widget2'
        assert set(body['capabilities']) == HELD

    # Stated as intent, not just as an absence: these are the verbs a tool would
    # carry if someone added the capability this design deliberately withholds —
    # destroying a card, searching the web, or writing a completion the user did
    # not perform.
    forbidden = ('delete', 'remove', 'purge', 'destroy', 'drop', 'wipe',
                 'complete', 'tick', 'done', 'search', 'recall', 'remember')
    assert not [name for name in HELD
                if any(verb in name for verb in forbidden)]


# This is a configuration invariant: the durability rule this repo keeps ("the
# brain never touches SQLite — all writes via the Node API") had no test.
def test_the_brain_reaches_the_board_only_through_its_http_api():
    """A prompt cannot get at the database, because no code path leads there.

    The durability promise — a card dies only via Trash → "Delete permanently" —
    holds because that route exists once, in `server.js`. It would stop holding
    the moment anything in this package opened board.db or assistant.db, and no
    behavioural test would notice: a second write path is invisible until it is
    used. So this reads the source.

    The brain does open one sqlite file now — the agent's checkpoints, which
    hold conversation threads and no user record. That makes the claim narrower
    and worth stating precisely rather than as a blanket ban on the word.
    """
    package = Path(lodestar_brain.__file__).parent
    imports_sqlite = [
        path.relative_to(package).as_posix()
        for path in package.rglob('*.py')
        # Import lines only. `retrieval/` discusses sqlite-vec in prose, and an
        # alternatives note weighing a library is not a dependency on it.
        if re.search(r'^\s*(?:import|from)\s+\S*sqlite', path.read_text(),
                     re.MULTILINE)]
    # One file may, and what it opens is not a board. The agent's checkpointer
    # and store are sqlite-backed, so the invariant is no longer "no sqlite" but
    # "no sqlite the user's data lives in" — asserted by naming the only opener
    # and then reading what it opens.
    assert imports_sqlite == ['server.py'], (
        f'only the composition root may open sqlite: {imports_sqlite}')
    source = (package / 'server.py').read_text()
    assert 'path = settings.checkpoint_db' in source
    assert re.findall(r'from_conn_string\((\w+)\)', source) == ['path', 'path'], (
        'the checkpointer and the store open the configured file and nothing else')
    # And that setting can never name the record: board.db and assistant.db are
    # the property of the Node server, in both the code and the env defaults.
    for configured in (Settings().checkpoint_db,
                       load_settings(env={}).checkpoint_db):
        assert 'board.db' not in configured
        assert 'assistant.db' not in configured

    # The board seam offers no way to destroy anything either — rejecting a
    # proposal soft-deletes through the same API, so there is no second path.
    destructive = [name for name in dir(BoardClient)
                   if any(verb in name for verb in ('delete', 'purge', 'remove'))]
    assert not destructive, f'BoardClient must expose no hard delete: {destructive}'


# This is a unit test: the prompt is the only place the user finds out what the
# assistant cannot do, so its wording is a contract like any other.
def test_the_prompt_says_it_cannot_delete_and_what_to_do_instead():
    """The other half of the delete guardrail — the half the user can see.

    Every other row in the catalogue above is enforced in code, which is why it
    is asserted rather than measured. This one is not enforceable: `delete_card`
    does not exist, so the model has no tool to fail against and nothing to
    reason from, and an unprompted model improvises — it claims it deleted the
    card, or refuses flatly and offers nothing. Both are worse than the limit
    itself, because the user is left believing a card is gone, or stuck with no
    idea where the real delete lives.

    So the limit and the way out must travel *together*, in one breath. A prompt
    that says "cannot delete" and never says where the person's own delete lives
    leaves them stuck. Asserted as one clause for exactly that reason.
    """
    prompt = SYSTEM_PROMPT.lower()
    assert 'cannot delete' in prompt, 'the prompt must state the limit outright'

    # The paragraph the limit is stated in, not a character count: a window of
    # n characters would break on an unrelated extra word, which is a test
    # failing about its own arithmetic rather than about the guardrail.
    guidance = prompt[prompt.index('cannot delete'):].split('\n\n')[0]
    # Where their own delete lives, and that the assistant's job is to say so.
    for alternative in ('trash', 'tell them where'):
        assert alternative in guidance, (
            f'the refusal must offer {alternative!r} in the same breath')


def _card(id, title, column='inbox', **extra):
    return {'id': id, 'columnId': column, 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1, **extra}


def _call(name, arguments):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': arguments, 'id': f'{name}-0'}])


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
    agent = Widget2Agent(
        settings=Settings(llm_provider='fake'),
        tools=make_tools(client),
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
    assert [c['id'] for c in asyncio.run(client.list_cards())] == ['a', 'b']


# This is a configuration invariant: the seam to the board is a closed surface,
# and a write reappearing on it must break a test rather than ship.
def test_the_brain_has_no_way_to_write_a_card():
    """The strongest form this guardrail has taken.

    It used to be "every edit saves the whole board", because a partial PUT
    soft-deletes what it omits. Now the client has no PUT at all: cards are
    proposed, edits are suggested, and only the user's own save writes. So the
    old invariant is not enforced here, it is unreachable — and this asserts the
    surface exactly, because a reintroduced `save_cards` would quietly restore
    every failure mode the removal closed.
    """
    surface = {name for name in vars(BoardClient) if not name.startswith('_')}
    # `record_trace` writes, and deliberately not to a card: it files the turn's
    # own developer trace into a table of its own, the way `record_chat` files
    # the conversation. Neither can reach `cards`.
    #
    # `write_vectors` (2026-09-07) is the third write on this surface and the
    # argument for it is the same shape: it writes `card_vectors`, a table of
    # embeddings derived from cards, which the board refuses to fill for a card
    # it does not hold. Nothing in it is the board's record — losing every row
    # costs time and no data, it is outside `rev` and outside the exported JSON —
    # so it can neither create, change nor destroy a card. This set breaking is
    # how a fourth write gets argued for rather than shipped, which is what it
    # did here.
    assert surface == {'list_board', 'list_cards', 'list_chat', 'list_all_chat',
                       'record_chat', 'record_trace', 'create_proposal',
                       'create_edit', 'list_vectors', 'write_vectors'}
    # And the claim the whole test is named for, stated separately from the set
    # so it cannot be satisfied by adding a name to a list: there is no
    # whole-board write here at all, under any spelling.
    # `list_board` reads the whole board document — cards AND the category
    # registry, which the map needs to embed a card as its labels. A read of
    # everything is not a write of anything, and it is the same GET
    # `list_cards` has always made.
    assert not [name for name in surface
                if 'save' in name or 'write' in name.replace('write_vectors', '')
                or name.startswith('put')]
