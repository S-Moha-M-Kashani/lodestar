import httpx
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app


def test_health():
    client = TestClient(create_app(Settings(llm_provider='fake', embedder='hash')))
    res = client.get('/health')
    assert res.status_code == 200
    assert res.json() == {'ok': True, 'service': 'lodestar-brain'}


def fake_app():
    return create_app(Settings(llm_provider='fake', embedder='hash',
                               board_api_url='http://board.test'))


def board_state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards})


def card(id, title):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '', 'num': 1,
            'tags': [], 'createdAt': 1, 'updatedAt': 1}


@respx.mock
def test_chat_echo_roundtrip():
    client = TestClient(fake_app())
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'hello brain'}]})
    assert res.status_code == 200
    body = res.json()
    assert body['reply'] == 'FAKE: hello brain'
    assert body['mutated'] is False
    assert body['steps'] == []


@respx.mock
def test_chat_add_creates_card_and_flags_mutation():
    respx.get('http://board.test/api/state').mock(return_value=board_state([]))
    respx.put('http://board.test/api/state').mock(
        return_value=board_state([card('n1', 'What is Leiden clustering?')]))
    client = TestClient(fake_app())
    res = client.post('/agent/chat', json={
        'messages': [{'role': 'user', 'content': 'add: What is Leiden clustering?'}]})
    body = res.json()
    assert body['mutated'] is True
    assert body['steps'] == [{'tool': 'create_question',
                              'arguments': {'title': 'What is Leiden clustering?'}}]
    assert 'created' in body['reply']


@respx.mock
def test_rag_reindex_and_communities():
    respx.get('http://board.test/api/state').mock(return_value=board_state(
        [card('a', 'kubernetes pods scaling'), card('b', 'kubernetes pod limits')]))
    client = TestClient(fake_app())
    res = client.post('/rag/reindex')
    assert res.status_code == 200
    assert res.json()['cards'] == 2
    res = client.get('/rag/communities')
    assert res.status_code == 200
    assert isinstance(res.json()['communities'], list)
