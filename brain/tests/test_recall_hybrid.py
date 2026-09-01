"""Recall becomes hybrid and searches the board as well as the chat record.

"Search past conversations" (POST /rag/recall) is today a dense-only query
over chat memory. The desired contract under test:

- `ChatStore.search` is hybrid: real BM25 over the stored chunks fused with
  the dense half by weighted RRF, and **BM25 carries much more weight** —
  `retrieval.RECALL_WEIGHTS = (dense, bm25)` with the bm25 weight at least
  3x the dense one. Observable: when the two halves disagree about the top
  hit, BM25's choice wins; a dense-only hit (no shared term) still surfaces,
  so the dense half is fused in rather than replaced.
- POST /rag/recall searches the board's cards too (through the app's
  configured board API — never SQLite, never any other host; respx raises on
  any unmocked request, which is what pins that here). Every match carries a
  `source` label: 'chat' or 'card'.
- Cards do not depend on Chroma: with memory off (chroma_url=''), recall
  still returns card matches and reports memory: False.

Which databases the paired test brain (:3001 board) reaches is already
fenced by the pairing rules in config.py and their tests in test_config.py —
board reads go to the brain's own `board_api_url`, chat memory to the
'lodestar-test' Chroma database.
"""
import math
from datetime import datetime, timezone

import httpx
import respx
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

from lodestar_brain.config import Settings
from lodestar_brain.retrieval import ChatStore, LexicalHashEmbeddings, MEMORY_URL
from lodestar_brain.server import create_app

BOARD = 'http://board.test'

PASSWORD = 'the wifi password is hunter2'
ROUTER = 'we changed the wifi router last week'
DENTIST = 'dentist appointment moved to friday'


def ms(year, month, day):
    return int(datetime(year, month, day, 12, 0,
                        tzinfo=timezone.utc).timestamp() * 1000)


def row(id, content, role='user', created=ms(2026, 7, 1)):
    return {'id': id, 'role': role, 'content': content, 'createdAt': created}


class RiggedDense(Embeddings):
    """The dense half under test control: cosine similarity to any query is
    exactly the score set here, so the test decides the dense ranking while
    BM25 stays real. [s, sqrt(1-s^2)] against a query of [1, 0] has cosine s."""
    SCORES = {PASSWORD: 0.1, ROUTER: 0.9, DENTIST: 0.5}

    def _vector(self, text):
        s = self.SCORES.get(text, 0.0)
        return [s, math.sqrt(1.0 - s * s)]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


# This is an integration test (in-process Chroma, no server, no disk).
def test_chat_search_is_hybrid_and_bm25_outweighs_dense():
    from lodestar_brain.retrieval import RECALL_WEIGHTS
    assert RECALL_WEIGHTS[1] >= 3 * RECALL_WEIGHTS[0], (
        'BM25 must carry much more weight than the dense half')

    store = ChatStore(MEMORY_URL, RiggedDense(), collection='chat-hybrid-weights')
    store.index_messages([row(1, PASSWORD), row(2, ROUTER), row(3, DENTIST)])

    # Dense ranks ROUTER (0.9) far above PASSWORD (0.1); BM25 ranks PASSWORD
    # first on the rare literal 'hunter2'. With BM25 weighted much higher the
    # literal match must win — under equal weights or dense-only it loses.
    hits = store.search('hunter2 wifi')
    assert hits and 'hunter2' in hits[0]['text'], (
        "BM25's top pick must beat the dense top pick")
    assert all(isinstance(hit['score'], float) for hit in hits)

    # DENTIST shares no term with the query: it is dense-floor noise, and
    # noise presented as a match is what makes a search feel broken. Dense
    # only orders the lexical survivors, it never introduces rows.
    assert not any('dentist' in hit['text'] for hit in hits), (
        'a no-shared-term document must not be presented as a match')


# This is an integration test.
@respx.mock
def test_recall_searches_cards_and_chat_with_source_labels():
    # respx raises on any request this test does not mock, so every card hit
    # below provably came from the app's own board_api_url — the same seam
    # that points the paired test brain at :3001 and its test databases.
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'cards': [{'id': 'c1', 'num': 1,
                   'title': 'buy a fig tree for the balcony',
                   'columnId': 'inbox', 'type': 'task', 'category': 'home',
                   'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)}]}))
    # /all, because that is what the boot sync reads: the index spans every
    # board, so seeding it from one board's messages would prune the rest.
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
        return_value=httpx.Response(200, json={'messages': [row(1, PASSWORD)]}))
    # `with`, so the app's lifespan runs: the boot sync is what seeds the index
    # from the record, and it belongs to the running service rather than to the
    # object graph — see `sync_chat_index` for why it cannot live anywhere else.
    with TestClient(create_app(Settings(
            llm_provider='fake', embedder='fake', board_api_url=BOARD,
            chroma_url=MEMORY_URL,
            chat_collection='chat-recall-cards'))) as client:
        res = client.post('/rag/recall',
                          json={'text': 'wifi password fig tree', 'k': 5})
    assert res.status_code == 200
    body = res.json()
    assert body['memory'] is True
    sources = {match['source'] for match in body['matches']}
    assert sources == {'chat', 'card'}, (
        'recall must answer from both the chat record and the board')
    assert any(match['source'] == 'chat' and 'hunter2' in match['text']
               for match in body['matches'])
    assert any(match['source'] == 'card' and 'fig tree' in match['text']
               for match in body['matches'])


# --- cross-script search ------------------------------------------------
# A user who types "mahsa" means «مهسا» (and the other way round), so every
# recall query is expanded across scripts: Latin tokens grow Persian
# transliteration variants and Persian tokens grow Latin ones, feeding the
# same BM25-heavy fusion. Cards are searchable by every word the user gave
# them: title, notes, tags, type and category.


# This is a unit test.
def test_expand_queries_bridges_scripts_both_ways():
    from lodestar_brain.retrieval import expand_queries
    assert any('مهسا' in v for v in expand_queries('mahsa')), (
        'a Latin query must grow a Persian-script variant')
    assert any('mahsa' in v for v in expand_queries('مهسا')), (
        'a Persian query must grow a Latin-script variant')


# This is a unit test.
def test_card_text_covers_type_category_and_tags():
    from lodestar_brain.retrieval import card_text
    card = {'title': 'دعوا با مهسا', 'notes': 'سر برنامه‌ریزی',
            'tags': ['family'], 'type': 'problem', 'category': 'love'}
    text = card_text(card)
    for term in ('family', 'problem', 'love'):
        assert term in text, f'cards must be findable by their {term!r} field'


# This is an integration test (in-process Chroma, no server, no disk).
def test_latin_query_recalls_the_persian_chat_message_first():
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(),
                      collection='chat-cross-script')
    store.index_messages([row(1, 'امروز با مهسا دعوا کردم'),
                          row(2, PASSWORD), row(3, DENTIST)])
    hits = store.search('mahsa')
    assert hits and 'مهسا' in hits[0]['text'], (
        'the message naming مهسا must outrank script-blind dense noise')

# This is an integration test (in-process Chroma, no server, no disk).
def test_chat_search_evidence_floor_is_optional_for_the_agent():
    """The floor protects a human reading a result list; the agent's
    find_related turns it off to keep semantic chunk matches reachable."""
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(),
                      collection='chat-evidence-flag')
    store.index_messages([row(1, 'دیشب دعوامون شد سر برنامه آخر هفته')])
    # «دعوا» and «دعوامون» are different BM25 tokens: no lexical evidence.
    assert store.search('دعوا') == [], 'the search box keeps the floor'
    hits = store.search('دعوا', evidence=False)
    assert hits and 'دعوامون' in hits[0]['text'], (
        'without the floor, the semantically nearest chunk must surface')


# This is an integration test (in-process Chroma, no server, no disk).
def test_chat_search_returns_nothing_when_nothing_matches():
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(),
                      collection='chat-no-noise')
    store.index_messages([row(1, PASSWORD), row(2, DENTIST)])
    assert store.search('mahsa') == [], (
        'a query matching no recorded term must return nothing, not the '
        'k nearest pieces of dense-floor noise')


# This is an integration test.
@respx.mock
def test_recall_says_nothing_rather_than_noise():
    """The reproduction for the cross-script recall screenshot on the
    :3001 sandbox: a board naming nobody by that name returned ten
    irrelevant rows. A query that matches nothing must say so — the UI
    already renders the empty list as 'Nothing recorded about that yet.'"""
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'cards': [
            {'id': 'c1', 'num': 1, 'title': 'Book the August ferry crossing',
             'columnId': 'inbox', 'type': 'task', 'category': 'travel',
             'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)},
            {'id': 'c2', 'num': 2, 'title': 'Which Stoic should I read?',
             'columnId': 'inbox', 'type': 'question', 'category': 'mind',
             'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)}]}))
    # /all, because that is what the boot sync reads: the index spans every
    # board, so seeding it from one board's messages would prune the rest.
    respx.get(f'{BOARD}/api/chat/messages/all').mock(
        return_value=httpx.Response(200, json={'messages': [row(1, PASSWORD)]}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url=MEMORY_URL, chat_collection='chat-recall-no-noise')))

    body = client.post('/rag/recall', json={'text': 'mahsa', 'k': 10}).json()
    assert body['memory'] is True
    assert body['matches'] == [], (
        'no card or message names mahsa in any script — showing anything '
        'here is presenting noise as an answer')


# This is an integration test.
@respx.mock
def test_latin_query_ranks_the_persian_card_first_with_a_real_score():
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'cards': [
            {'id': 'c1', 'num': 1, 'title': 'دعوا با مهسا سر برنامه‌ریزی',
             'columnId': 'inbox', 'type': 'problem', 'category': 'love',
             'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)},
            {'id': 'c2', 'num': 2, 'title': 'Book the August ferry crossing',
             'columnId': 'inbox', 'type': 'task', 'category': 'travel',
             'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)},
            {'id': 'c3', 'num': 3, 'title': 'Learn the Spanish subjunctive',
             'columnId': 'inbox', 'type': 'task', 'category': 'mind',
             'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)}]}))
    respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))

    body = client.post('/rag/recall', json={'text': 'mahsa'}).json()
    assert body['matches'], 'the Persian card must be found from a Latin query'
    top = body['matches'][0]
    assert 'مهسا' in top['text'], 'the card about مهسا must come first'
    assert top['score'] > 0, (
        "a lexically matched card must not display a coverage of 0 — the "
        "score must see the cross-script expansion the ranking saw")


# This is an integration test.
@respx.mock
def test_recall_returns_card_matches_even_with_memory_off():
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'cards': [{'id': 'c1', 'num': 1,
                   'title': 'buy a fig tree for the balcony',
                   'columnId': 'inbox', 'type': 'task', 'category': 'home',
                   'createdAt': ms(2026, 7, 1), 'updatedAt': ms(2026, 7, 1)}]}))
    respx.get(f'{BOARD}/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': []}))
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url=BOARD,
        chroma_url='')))

    body = client.post('/rag/recall', json={'text': 'fig tree'}).json()
    # memory: False keeps meaning what it says — Chroma is off — while the
    # board, which never depended on Chroma, still answers.
    assert body['memory'] is False
    assert body['matches'], 'cards must be searchable with chat memory off'
    assert all(match['source'] == 'card' for match in body['matches'])
