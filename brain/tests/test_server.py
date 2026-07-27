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


# ---- Chat memory: chunks from assistant chat land in a per-board Chroma ---

def memory_app(chat_dir):
    return create_app(Settings(llm_provider='fake', embedder='hash',
                               board_api_url='http://board.test',
                               chat_memory_dir=str(chat_dir)))


@respx.mock
def test_chat_records_both_sides_and_recall_finds_them(tmp_path):
    client = TestClient(memory_app(tmp_path))
    client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'the wifi password is hunter2'}]})
    res = client.post('/rag/recall', json={'text': 'wifi password', 'k': 4})
    assert res.status_code == 200
    matches = res.json()['matches']
    assert matches, 'chat exchange was not recorded'
    assert any('wifi password' in m['text'] for m in matches)
    roles = {m['metadata']['role'] for m in matches}
    assert {'user', 'assistant'} <= roles  # reply is recorded too (FAKE: echo)


@respx.mock
def test_recall_orders_by_relevance(tmp_path):
    client = TestClient(memory_app(tmp_path))
    for text in ['the wifi password is hunter2',
                 'dentist appointment moved to friday']:
        client.post('/agent/chat', json={'messages': [
            {'role': 'user', 'content': text}]})
    res = client.post('/rag/recall', json={'text': 'dentist appointment', 'k': 1})
    assert 'dentist' in res.json()['matches'][0]['text']


@respx.mock
def test_paired_stores_are_isolated_per_board(tmp_path):
    # Two brains, two persist dirs — the board.db brain must never see chat
    # recorded by the board-3001.db brain, and vice versa.
    main = TestClient(memory_app(tmp_path / 'board-3000'))
    test = TestClient(memory_app(tmp_path / 'board-3001'))
    main.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'production secret rotation is tuesday'}]})
    res = test.post('/rag/recall', json={'text': 'secret rotation', 'k': 5})
    assert all('rotation' not in m['text'] for m in res.json()['matches'])


@respx.mock
def test_recall_with_memory_off_returns_no_matches():
    # Default Settings leave chat_memory_dir empty: no disk writes, no matches.
    client = TestClient(fake_app())
    client.post('/agent/chat', json={'messages': [
        {'role': 'user', 'content': 'hello brain'}]})
    res = client.post('/rag/recall', json={'text': 'hello', 'k': 3})
    assert res.status_code == 200
    assert res.json() == {'matches': []}


def test_recall_requires_a_text_field(tmp_path):
    client = TestClient(memory_app(tmp_path))
    assert client.post('/rag/recall', json={}).status_code == 422


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
