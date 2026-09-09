"""Widget 2's two writing tools — both of which only ever ask.

`create_card` proposes a card and `update_card` suggests a change to one, and
neither may reach the board: what these tests pin is that the request goes to
the waiting-room route, carries exactly the fields the call named, and that a
whole-board PUT is never attempted even though one is mocked and would succeed.

The board's own allowlist against the schema is pinned once, in
`test_edit_suggestions.py`, and is deliberately not repeated here. What is
asserted here is the half that lives in this module: the two schemas the *model*
is shown, and the sentences it reads.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from lodestar_brain.board.client import BoardClient
from lodestar_brain.widget2.tools.fields import (BOARD_KEYS,
                                                 EDITABLE_BOARD_KEYS, CardEdit,
                                                 NewCard)
from lodestar_brain.widget2.tools.write import make_write_tools

BOARD = 'http://board.test'

#: The board the picker chose, as it reaches a tool: through the run config,
#: never through an argument the model could name.
CONFIG = {'configurable': {'board_id': 'b7'}}


def _tools():
    return {tool.name: tool for tool in make_write_tools(BoardClient(BOARD))}


def _proposal(**extra) -> dict:
    """One card as `writeProposal` answers with it — `cleanCard`'s own shape."""
    return {'id': 'p1', 'num': 0, 'title': 'Book the train to Zermatt',
            'notes': '', 'type': 'task', 'category': 'travel',
            'columnId': 'inbox', 'importance': '', 'urgency': '',
            'effort': 'medium', 'control': 'influence', 'deadline': '',
            'plan': '', 'planSrc': 'auto', 'habitFreq': '', 'habitCount': 1,
            'habitTimes': [], 'habitHistory': {}, 'parentId': '', 'tags': [],
            'createdAt': 1, 'updatedAt': 1, 'doneAt': 0, **extra}


# This is a unit test.
@respx.mock
def test_a_new_card_is_proposed_and_the_board_is_never_written():
    """The whole guardrail in one run: the proposal route is called, the
    whole-board PUT is mocked and never touched, and `pending` is the server's
    word — the tool does not send it and could not clear it if it did."""
    put = respx.put(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'version': 1, 'cards': []}))
    proposals = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json=_proposal(
            plan='2027-03', planSrc='ai', category='travel')))

    answer = _tools()['create_card'].run(
        {'title': 'Book the train to Zermatt', 'type': 'task',
         'category': 'travel', 'plan': '2027-03'}, config=CONFIG)

    assert not put.called, 'a proposal must never reach the board itself'
    sent = json.loads(proposals.calls.last.request.content)
    # The board's own spelling, and only what the call named — plus `planSrc`,
    # which the tool sets from the presence of a plan because the board resolves
    # an 'auto' plan straight back to the deadline and the model may not claim a
    # person chose it.
    assert sent == {'title': 'Book the train to Zermatt', 'type': 'task',
                    'category': 'travel', 'plan': '2027-03', 'planSrc': 'ai'}
    # The board the picker chose, from the run config and not from the model.
    assert proposals.calls.last.request.url.params['board'] == 'b7'
    # And the model is told it has only asked, over a card rendered by the one
    # site that renders cards.
    assert answer.startswith('Proposed, and waiting for the owner to accept it')
    assert '(no ledger number yet) “Book the train to Zermatt”' in answer
    assert 'plan: 2027-03' in answer


# This is a unit test.
@respx.mock
def test_a_one_field_suggestion_stays_one_field():
    """What the model named, and nothing else.

    A suggestion carrying every field would overwrite the untouched ones with
    values that were current when the model looked. The framework hands a tool
    all fifteen fields, filled with their default, so this is the assertion that
    fails if `_given` ever stops restoring the set the model actually chose —
    including the difference between clearing a deadline (`''`) and not
    mentioning it (absent).
    """
    put = respx.put(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={'version': 1, 'cards': []}))
    edits = respx.post(f'{BOARD}/api/edits').mock(return_value=httpx.Response(
        200, json={'id': 'e1', 'cardId': 'a', 'fields': {}}))

    answer = _tools()['update_card'].run(
        {'id': 'a', 'notes': 'Rebooked for the 14th.', 'deadline': ''},
        config=CONFIG)

    assert not put.called, 'a suggestion must never reach the board itself'
    sent = json.loads(edits.calls.last.request.content)
    assert sent == {'cardId': 'a',
                    'fields': {'notes': 'Rebooked for the 14th.',
                               'deadline': ''}}
    assert 'waiting for the owner to open it and save' in answer

    # Nothing named is nothing to suggest, answered as a sentence rather than as
    # the board's exception: it is a fact about the call, not about the board.
    assert 'Nothing to suggest' in _tools()['update_card'].run({'id': 'a'})
    assert len(edits.calls) == 1


# This is a unit test.
@respx.mock
def test_finishing_a_card_is_one_suggested_column_and_no_date():
    """Putting a card in Done is `update_card` with `column_id: 'answered'`.

    No second tool and no `doneAt` argument: `placeCard` stamps the finished
    moment when the owner's own save files the card, so the suggestion carries
    the column and nothing else — a tool that could send the date could claim a
    day nobody finished anything on.
    """
    edits = respx.post(f'{BOARD}/api/edits').mock(return_value=httpx.Response(
        200, json={'id': 'e1', 'cardId': 'a', 'fields': {}}))

    tools = _tools()
    tools['update_card'].run({'id': 'a', 'column_id': 'answered'},
                             config=CONFIG)

    sent = json.loads(edits.calls.last.request.content)
    assert sent['fields'] == {'columnId': 'answered'}
    # Not merely absent from this call — unreachable, in either spelling.
    assert 'doneAt' not in json.dumps(sent)
    assert not {'done_at', 'doneAt'} & set(tools['update_card'].args)
    # And the model is told where the date comes from, in the place it reads.
    assert 'stamps the finished' in tools['update_card'].description


# This is a configuration invariant: one field set in two doors, and widening it
# must break a test rather than pass quietly.
def test_the_two_schemas_are_one_field_set_and_it_is_the_boards_own():
    """§5.1 and §5.5, asserted in both directions.

    The two argument models are `fields.py`'s, so this is a test that the
    derivation held: the same fields on both doors apart from the one each
    requires, and that set is exactly what the board can change. A subset check
    either way would wave a new field through, which is precisely how Widget 1
    came to advertise fields the write path did not carry.
    """
    tools = _tools()
    create, update = tools['create_card'], tools['update_card']

    # The same fields on both doors: `create_card` is the fifteen, `update_card`
    # is the fifteen plus the id of the card it changes. What differs between
    # them is which one is required, never which ones exist.
    assert set(create.args) == set(BOARD_KEYS)
    assert set(update.args) == set(BOARD_KEYS) | {'id'}
    assert {BOARD_KEYS[name] for name in create.args} == set(EDITABLE_BOARD_KEYS)

    # One required field each, and `id` is not one of the fifteen: a proposal
    # cannot name the card it wants to be, and an edit must name the card it
    # changes.
    assert create.args_schema.model_json_schema()['required'] == ['title']
    assert update.args_schema.model_json_schema()['required'] == ['id']
    assert 'id' not in create.args and 'id' not in BOARD_KEYS

    # What stays out, stated as intent rather than left as an absence — each
    # reason is in `fields.py`'s docstring, and the equality above would be just
    # as satisfied by adding one of these to both sides.
    for withheld in ('parent_id', 'habit_history', 'done_at', 'plan_src',
                     'effort_src', 'control_src', 'num'):
        assert withheld not in create.args
        assert withheld not in update.args

    # **The one pair the shared model refuses, on both doors.** A plan whose
    # window opens after the card was already due cannot be saved — the card
    # dialog refuses it — so naming that pair produces something the owner
    # cannot apply. It matters most on the proposal, which is the case that
    # escapes: a proposal is confirmed by its own route and never passes
    # through the dialog, so without this a proposed card could carry a
    # conflict a hand-typed one is refused for.
    for door, extra in ((NewCard, {'title': 'x'}), (CardEdit, {'id': 'c1'})):
        with pytest.raises(ValidationError, match='starts after the deadline'):
            door(**extra, plan='2027-03', deadline='2027-01-15')
        # A year is placed at its first day, not its last, or a plan for 2028
        # would read as compatible with a deadline in 2027.
        with pytest.raises(ValidationError, match='starts after the deadline'):
            door(**extra, plan='2028', deadline='2027-12-31')
        # And the rule is about the pair: either field alone says nothing,
        # because the other one is on the board and this model may not read it.
        assert door(**extra, plan='2027-03').plan == '2027-03'
        assert door(**extra, deadline='2027-01-15').deadline == '2027-01-15'
        assert door(**extra, plan='2027-03', deadline='2027-06-01').plan


# This is a configuration invariant: the board's integration is keyed on these
# two names, and the honesty is in their descriptions.
def test_two_tools_named_what_the_board_expects_and_no_third_door():
    """Exactly two, called what `server.js` and the cache middleware assert.

    The names cannot say that a tool only asks, so each description does — and
    there is no third tool here at all: no delete, no purge, no habit
    completion. Absent rather than gated, which is how the durability promise
    is kept.
    """
    from lodestar_brain.middleware.cache import NEVER_CACHED
    from lodestar_brain.server import PROPOSING_TOOLS

    tools = _tools()
    assert set(tools) == {'create_card', 'update_card'} == PROPOSING_TOOLS \
        == set(NEVER_CACHED)

    assert 'propose' in tools['create_card'].description
    assert 'never that you added it' in tools['create_card'].description
    assert 'suggest' in tools['update_card'].description
    assert 'never that you made one' in tools['update_card'].description
