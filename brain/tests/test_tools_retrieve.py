"""The two retrieval tools, as the agent sees them.

`tools/retrieve.py` is deliberately thin: the pipeline lives in `retrieval.py`
and these wrappers decide only what the model is told. That contract is what
these tests hold — the shape of a result, not the quality of a ranking.
"""
import httpx
import respx

from lodestar_brain.retrieval import CardIndex, LexicalHashEmbeddings
from lodestar_brain.tools.board import BoardClient
from lodestar_brain.tools.retrieve import make_recall_tool, make_retrieve_tool


def card(id, title, tags=None):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': tags or [], 'createdAt': 1, 'updatedAt': 1}


# Two lexically distinct subjects, so a ranking has something to get wrong.
CARDS = [
    card('k1', 'How to scale kubernetes pods under load?', ['infra']),
    card('k2', 'Best kubernetes pod autoscaling strategy?', ['infra']),
    card('k3', 'Debug kubernetes pod restarts and scale limits', ['infra']),
    card('h1', 'Structure hiring interviews for senior candidates?', ['team']),
    card('h2', 'What hiring interview questions reveal candidates?', ['team']),
    card('h3', 'How to calibrate hiring interview feedback?', ['team']),
]


def board_state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards})


def index_and_board():
    return CardIndex(LexicalHashEmbeddings()), BoardClient('http://board.test')


class FakeStore:
    def __init__(self):
        self.asked = []

    def search(self, text, k=5, exclude_session=None):
        self.asked.append((text, k, exclude_session))
        return [{'text': 'the wifi password is hunter2', 'score': 0.9,
                 'metadata': {'role': 'user', 'created_day': 20260712,
                              'session_id': 's-old'}}]


# This is a unit test.
@respx.mock
def test_find_related_ranks_the_board_and_claims_no_score():
    route = respx.get('http://board.test/api/state').mock(
        return_value=board_state(CARDS))
    tool = make_retrieve_tool(*index_and_board())
    hits = tool.run({'text': 'kubernetes pod scaling', 'k': 2})
    assert len(hits) <= 2
    assert {hit['card']['id'] for hit in hits} <= {'k1', 'k2', 'k3'}
    assert [hit['rank'] for hit in hits] == list(range(1, len(hits) + 1))
    # RRF exposes no fused score, so a number invented from a rank would mean
    # nothing. `community` is asserted absent because it used to be here: a
    # reader still expecting it fails loudly instead of reading None as a theme.
    assert 'score' not in hits[0] and 'community' not in hits[0]
    # The brief is built from the board rows the tool just read, not from index
    # metadata: tags are a list there, and space-joining them so a store can
    # filter on them must not come back to the model as a lossy string.
    assert hits[0]['card']['tags'] == ['infra']
    assert set(hits[0]['card']) == {'id', 'title', 'columnId', 'tags'}
    # The board is re-read on every call, so a card added since the last one is
    # findable without anything having to invalidate a cache.
    route.mock(return_value=board_state(CARDS + [card('n1', 'kubernetes notes')]))
    assert 'n1' in {hit['card']['id']
                    for hit in tool.run({'text': 'kubernetes notes', 'k': 3})}


# This is an integration test (in-process Chroma, no server, no disk).
@respx.mock
def test_find_related_also_answers_from_the_chat_record():
    """find_related is the agent's one search over everything the user has:
    the board (board.db via the paired board API) AND the chat record
    (assistant.db, through its Chroma chunk index) — which board and which
    record follows from the brain's own wiring, so the :3001 test brain reads
    only test data. The chat side is *semantic*: «دعوا» must reach a chunk
    saying «دعوامون» even though they share no BM25 token — the recall box's
    lexical-evidence floor is for humans reading a result list, not for a
    model that judges relevance itself."""
    from lodestar_brain.retrieval import ChatStore, MEMORY_URL
    respx.get('http://board.test/api/state').mock(return_value=board_state(CARDS))
    index, client = index_and_board()
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(),
                      collection='find-related-chat')
    store.index_messages([{'id': 1, 'role': 'user', 'createdAt': 1753995600000,
                           'content': 'دیشب دعوامون شد سر برنامه آخر هفته'}])

    tool = make_retrieve_tool(index, client, memory=store)
    hits = tool.run({'text': 'دعوا', 'k': 3})
    chat_rows = [hit for hit in hits if 'chat' in hit]
    assert chat_rows, 'find_related must also answer from the chat record'
    assert any('دعوامون' in hit['chat']['text'] for hit in chat_rows), (
        'a chunk related only semantically (no shared token) must reach '
        'the agent')
    assert {'text', 'role'} <= set(chat_rows[0]['chat']), (
        'the model needs the words and who said them')
    assert [hit['rank'] for hit in chat_rows] == list(
        range(1, len(chat_rows) + 1))
    # Cards keep their shape exactly — the Assistant's source list reads
    # row.card to build a clickable citation.
    assert all(set(hit) == {'card', 'rank'} or set(hit) == {'chat', 'rank'}
               for hit in hits)


# This is a unit test.
def test_recall_chat_hands_back_what_the_store_found():
    store = FakeStore()
    tool = make_recall_tool(store)
    assert tool.name == 'recall_chat'
    assert tool.args_schema.model_json_schema()['required'] == ['text']
    matches = tool.run({'text': 'wifi password', 'k': 3})
    assert 'hunter2' in matches[0]['text']
    assert store.asked == [('wifi password', 3, None)]


# This is a unit test.
def test_recall_skips_the_chat_it_is_already_reading():
    """The current conversation is already in the context window.

    Returning it back spends the model's attention re-reading what it can
    already see, and — worse — makes a fresh chat look like it has history the
    moment the model reaches for the tool. Which chat that is arrives through
    the run config, not as a tool argument: the model must not be able to name
    (or spoof) it, and the args schema below is the assertion of that.
    """
    store = FakeStore()
    tool = make_recall_tool(store)
    config = {'configurable': {'session_id': 's-current'}}

    matches = tool.run({'text': 'wifi password'}, config=config)
    assert store.asked[-1] == ('wifi password', 5, 's-current')
    assert 'session_id' not in tool.args_schema.model_json_schema()['properties'], (
        'the session is injected, never offered to the model as an argument')

    # Dated, so the model can say "you mentioned this on the 12th" instead of
    # quoting the past as though it were said now. The date is already in the
    # chunk metadata; the chat's *title* is deliberately not carried — see the
    # spec's Recall section for why a copied title goes stale on rename.
    assert matches[0]['day'] == 20260712

    # Missing config is not an error: an eval or a curl runs without one, and a
    # recall that excluded nothing is strictly better than a 500.
    assert tool.run({'text': 'wifi password'})
    assert store.asked[-1][2] is None
