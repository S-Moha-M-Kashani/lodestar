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

    def search(self, text, k=5):
        self.asked.append((text, k))
        return [{'text': 'the wifi password is hunter2', 'score': 0.9,
                 'metadata': {'role': 'user'}}]


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


# This is a unit test.
def test_recall_chat_hands_back_what_the_store_found():
    store = FakeStore()
    tool = make_recall_tool(store)
    assert tool.name == 'recall_chat'
    assert tool.args_schema.model_json_schema()['required'] == ['text']
    matches = tool.run({'text': 'wifi password', 'k': 3})
    assert 'hunter2' in matches[0]['text']
    assert store.asked == [('wifi password', 3)]
