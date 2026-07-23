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
def test_create_question_puts_full_list():
    existing = [card('a', 'Old question')]
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing}))
    put = respx.put(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': existing + [card('new', 'Fresh question')]}))
    tools = tools_by_name(BoardClient(BOARD))
    created = tools['create_question'].run({'title': 'Fresh question'})
    sent = json.loads(put.calls.last.request.content)
    # durability guarantee: the pre-existing card MUST be in the PUT payload
    assert {c.get('id') for c in sent['cards']} >= {'a'}
    assert any(c['title'] == 'Fresh question' for c in sent['cards'])
    assert created['id'] == 'new'


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
