import json

import httpx
import respx

from lodestar_brain.tools.board import BoardClient, make_board_tools

BOARD = 'http://board.test'


def card(id, title, column='inbox', **extra):
    base = {'id': id, 'columnId': column, 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1}
    return {**base, **extra}


def tools_by_name(client):
    return {t.name: t for t in make_board_tools(client)}


@respx.mock
def test_list_questions_filters():
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': [card('a', 'RAG evals?', 'inbox'),
                                card('b', 'GPU budget?', 'answered')]}))
    tools = tools_by_name(BoardClient(BOARD))
    rows = tools['list_questions'].run({'column_id': 'inbox'})
    assert [r['id'] for r in rows] == ['a']
    rows = tools['list_questions'].run({'search': 'gpu'})
    assert [r['id'] for r in rows] == ['b']


@respx.mock
def test_create_question_posts_a_proposal_not_a_board_save():
    # The agent may no longer write a card straight onto the board: it proposes
    # one, and the user confirms it. So this posts a single card to
    # /api/proposals and never touches PUT /api/state.
    post = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json=card('new', 'Fresh question', pending=1)))
    put = respx.put(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': []}))
    tools = tools_by_name(BoardClient(BOARD))
    created = tools['create_question'].run({'title': 'Fresh question',
                                            'type': 'idea', 'category': 'work'})
    assert not put.called, 'a proposal must not go through the whole-board save'
    sent = json.loads(post.calls.last.request.content)
    # One card, not a full list — proposals are outside the durability contract
    # that PUT /api/state carries.
    assert sent['title'] == 'Fresh question'
    assert sent['type'] == 'idea'
    assert sent['category'] == 'work'
    assert created['id'] == 'new'
    # The model must be able to tell that nothing is on the board yet, so it
    # reports a proposal instead of claiming it added a card.
    assert created['pending'] is True


@respx.mock
def test_create_question_tool_description_says_it_needs_approval():
    tools = tools_by_name(BoardClient(BOARD))
    described = tools['create_question'].spec()['function']['description'].lower()
    assert 'propos' in described or 'approv' in described or 'confirm' in described


@respx.mock
def test_update_question_changes_only_named_fields():
    existing = [card('a', 'Old', notes='keep me'), card('b', 'Other')]
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing}))
    put = respx.put(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing}))
    tools = tools_by_name(BoardClient(BOARD))
    updated = tools['update_question'].run({'id': 'a', 'column_id': 'in-progress',
                                            'type': 'task', 'category': 'work'})
    assert updated['columnId'] == 'in-progress'
    assert updated['type'] == 'task'
    assert updated['category'] == 'work'
    assert updated['notes'] == 'keep me'
    sent = json.loads(put.calls.last.request.content)
    assert {c['id'] for c in sent['cards']} == {'a', 'b'}


@respx.mock
def test_update_round_trips_effort_and_control_untouched():
    # The agent doesn't set effort/control (LLM estimation is out of scope for
    # now) — but its full-list saves must carry the fields through verbatim.
    existing = [card('a', 'Old', effort='high', control='none',
                     effortSrc='user', controlSrc='user'),
                card('b', 'Other', effort='low', control='act',
                     effortSrc='default', controlSrc='default')]
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing}))
    put = respx.put(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing}))
    tools = tools_by_name(BoardClient(BOARD))
    tools['update_question'].run({'id': 'a', 'column_id': 'in-progress'})
    sent = {c['id']: c for c in json.loads(put.calls.last.request.content)['cards']}
    assert sent['a']['effort'] == 'high' and sent['a']['control'] == 'none'
    assert sent['a']['effortSrc'] == 'user' and sent['a']['controlSrc'] == 'user'
    assert sent['b']['effort'] == 'low' and sent['b']['control'] == 'act'
    assert sent['a']['columnId'] == 'in-progress'


@respx.mock
def test_update_unknown_id_errors_without_writing():
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': []}))
    tools = tools_by_name(BoardClient(BOARD))
    assert 'error' in tools['update_question'].run({'id': 'nope', 'type': 'task'})


def test_tool_specs_are_openai_shaped():
    specs = [t.spec() for t in make_board_tools(BoardClient(BOARD))]
    names = {s['function']['name'] for s in specs}
    assert names == {'list_questions', 'create_question', 'update_question'}
    assert all(s['type'] == 'function' and 'parameters' in s['function'] for s in specs)


# ---- Habits ---------------------------------------------------------------
# The Assistant may propose a habit like any other card. It deliberately cannot
# tick one: logging a repetition is the user's act, and a history an agent could
# write into is not a record of anything.


def test_habit_is_an_offered_card_type():
    tools = tools_by_name(BoardClient(BOARD))
    for name in ('create_question', 'update_question'):
        enum = tools[name].spec()['function']['parameters']['properties']['type']['enum']
        assert 'habit' in enum, f'{name} does not offer the habit type'


@respx.mock
def test_create_question_proposes_a_habit_with_its_cadence():
    post = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json=card(
            'new', 'Meditate', type='habit', habitFreq='daily', habitCount=2, pending=1)))
    tools = tools_by_name(BoardClient(BOARD))
    created = tools['create_question'].run({
        'title': 'Meditate', 'type': 'habit',
        'frequency': 'daily', 'times_per_period': 2, 'category': 'health'})
    sent = json.loads(post.calls.last.request.content)
    assert sent['type'] == 'habit'
    assert sent['habitFreq'] == 'daily'
    assert sent['habitCount'] == 2
    assert created['pending'] is True


@respx.mock
def test_a_non_habit_proposal_carries_no_cadence():
    post = respx.post(f'{BOARD}/api/proposals').mock(
        return_value=httpx.Response(200, json=card('new', 'A thought')))
    tools = tools_by_name(BoardClient(BOARD))
    tools['create_question'].run({'title': 'A thought', 'type': 'idea'})
    sent = json.loads(post.calls.last.request.content)
    assert sent.get('habitFreq', '') == ''


@respx.mock
def test_the_agent_has_no_tool_for_ticking_a_habit():
    names = {t.name for t in make_board_tools(BoardClient(BOARD))}
    assert not any('habit' in n or 'tick' in n or 'complete' in n for n in names), (
        'the history must record what the user did, not what the model claimed')
