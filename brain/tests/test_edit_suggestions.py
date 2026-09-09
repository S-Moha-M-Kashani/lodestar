"""An agent edit is a suggestion the user saves, not a write.

`create_card` has always been a proposal. `update_card` was the exception: it
applied immediately, with no confirmation and no snapshot, which made it the one
path an obeyed prompt injection could use to destroy notes the user wrote.

So it becomes a suggestion too. The tool posts the fields it wants changed to
`/api/edits`; nothing on the board moves. The Assistant shows the suggestion in
the ordinary card editor, the user can change any of it, and *the user's own save*
is what writes — the same whole-board PUT any hand edit goes through. That is the
point of the design: there is no longer an apply path the agent can reach at all,
so there is nothing to gate, back up, or get wrong.

The catalogue of what this closes lives in `test_guardrails.py`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import respx

from lodestar_brain.tools.board import BoardClient, make_board_tools
from lodestar_brain.widget2.tools.fields import EDITABLE_BOARD_KEYS

BOARD = 'http://board.test'
SERVER_JS = Path(__file__).resolve().parents[2] / 'server.js'


def _card(id, title, **extra):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1, **extra}


def _tools(client):
    return {tool.name: tool for tool in make_board_tools(client)}


def _state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards})


# This is a unit test.
@respx.mock
def test_update_card_suggests_an_edit_and_never_writes_the_board():
    """The whole guardrail in one assertion: PUT is never called.

    Both routes are mocked, so a write would succeed if the tool attempted one —
    the test proves a choice, not the absence of a mock.
    """
    respx.get(f'{BOARD}/api/state').mock(
        return_value=_state([_card('a', 'Renew the passport')]))
    put = respx.put(f'{BOARD}/api/state').mock(return_value=_state([]))
    edits = respx.post(f'{BOARD}/api/edits').mock(return_value=httpx.Response(
        200, json={'id': 'e1', 'cardId': 'a', 'fields': {'title': 'Renew it'}}))

    result = _tools(BoardClient(BOARD))['update_card'].run(
        {'id': 'a', 'title': 'Renew it', 'column_id': 'answered'})

    assert not put.called, 'an agent edit must never reach the board'
    assert edits.called
    sent = json.loads(edits.calls.last.request.content)
    assert sent['cardId'] == 'a'
    # Only the fields named in the call: a suggestion that carried every field
    # would overwrite the untouched ones with stale values when the user saved.
    assert sent['fields'] == {'title': 'Renew it', 'columnId': 'answered'}
    # And the model is told plainly, so it reports a suggestion rather than
    # claiming it changed the card — the same contract create_card has.
    assert result['pending'] is True


# This is a unit test.
@respx.mock
def test_a_suggestion_for_an_unknown_card_errors_without_suggesting_anything():
    """Kept from the old behaviour: an invented id is an error, not a write.

    The model is told to look ids up first, and inventing one must not leave a
    suggestion pointing at nothing for the user to puzzle over.
    """
    respx.get(f'{BOARD}/api/state').mock(
        return_value=_state([_card('a', 'Renew the passport')]))
    edits = respx.post(f'{BOARD}/api/edits').mock(return_value=httpx.Response(200))

    result = _tools(BoardClient(BOARD))['update_card'].run(
        {'id': 'nope', 'title': 'x'})

    assert 'error' in result and 'nope' in result['error']
    assert not edits.called


# This is a unit test.
@respx.mock
def test_a_suggested_edit_cannot_carry_a_habits_history():
    """No new way in. The fields a suggestion may carry are the fields
    `update_card` has always accepted, so habit history stays unreachable — and
    now it is unreachable one step earlier, before anything is stored at all."""
    respx.get(f'{BOARD}/api/state').mock(return_value=_state([
        _card('h', 'Morning pages', type='habit', habitFreq='daily',
              habitCount=1, habitHistory={'2026-07-30': 1})]))
    edits = respx.post(f'{BOARD}/api/edits').mock(return_value=httpx.Response(
        200, json={'id': 'e1', 'cardId': 'h', 'fields': {'notes': 'felt good'}}))

    _tools(BoardClient(BOARD))['update_card'].run(
        {'id': 'h', 'notes': 'felt good', 'habitHistory': {'2026-08-03': 99}})

    assert json.loads(edits.calls.last.request.content)['fields'] == {
        'notes': 'felt good'}


# This is a unit test.
def test_no_tool_mutates_the_board_any_more():
    """The consequence for the route's two flags, stated once.

    `update_card` moves out of MUTATING_TOOLS, which leaves it empty: no tool
    changes the board now, so nothing should tell the browser to adopt server
    state mid-conversation. Both tools propose, and the browser refreshes the
    suggestion list instead.
    """
    from lodestar_brain.server import MUTATING_TOOLS, PROPOSING_TOOLS
    assert PROPOSING_TOOLS == {'create_card', 'update_card'}
    assert MUTATING_TOOLS == set(), (
        'a tool that mutates the board is a tool that skipped the user')


def _server_allowlist():
    """`EDITABLE_FIELDS` as `server.js` declares it.

    Parsed, because it is a module-local const in a file with no exports and
    nothing to import it from — and this side is the parsed half deliberately:
    Python can *import* its own list, so exactly one of the two is read as text
    rather than as a value. The failure that matters is the two lists
    disagreeing, and a regex that stopped matching would report that as a
    missing declaration rather than passing on an empty set.
    """
    body = re.search(r'const EDITABLE_FIELDS = \[(.*?)\];', SERVER_JS.read_text(),
                     re.DOTALL)
    assert body, f'no EDITABLE_FIELDS declaration in {SERVER_JS}'
    return set(re.findall(r"'(\w+)'", body.group(1)))


# This is a configuration invariant: two lists in two languages that have to be
# one set, asserted rather than kept in step by hand.
def test_the_boards_allowlist_and_the_tool_schema_are_the_same_set():
    """The cure for the 2026-09-02 drops, applied at the seam that caused them.

    Twice now a tool has advertised a field `server.js` silently dropped, and
    both times the cause was two hand-written lists: `UpdateCardArgs` offered a
    `deadline` the allowlist did not carry, and `writeProposal`'s INSERT named
    no `plan` column while `create_card` offered one. Neither failed anything.

    So the set is declared once, in `widget2/tools/fields.py`, and asserted
    equal here **in both directions**: a field added to the schema and not to
    the server is a suggestion that becomes a 400 nobody sees, and a field added
    to the server and not to the schema is a capability nothing can reach. A
    subset check either way would wave one of those through.
    """
    assert _server_allowlist() == set(EDITABLE_BOARD_KEYS), (
        'server.js EDITABLE_FIELDS and widget2 EDITABLE_BOARD_KEYS have '
        f'drifted: server-only {_server_allowlist() - set(EDITABLE_BOARD_KEYS)}, '
        f'schema-only {set(EDITABLE_BOARD_KEYS) - _server_allowlist()}')

    # And what stays out stays out on *both* sides, stated as intent rather than
    # left as an absence: each has its own reason in fields.py's docstring, and
    # the equality above would be just as satisfied by adding them to both.
    for withheld in ('parentId', 'habitHistory', 'doneAt', 'planSrc',
                     'effortSrc', 'controlSrc', 'num'):
        assert withheld not in EDITABLE_BOARD_KEYS
        assert withheld not in _server_allowlist()
