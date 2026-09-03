"""Card-level maintenance of the board index.

Everything here is offline — no extra, no download, no socket, no Chroma —
because the brain suite has to stay that way. The embedder is the lexical hash
one behind a counter, so "only one card was re-embedded" is a call count and
never an inference.

**Measured on a 60-card board** (`CardIndex` over `LexicalHashEmbeddings`,
2026-09-02), documents handed to the embedder:

| after                | before this change | now |
| -------------------- | ------------------ | --- |
| first build          | 60                 | 60  |
| one title edited     | 60                 | 1   |
| metadata-only change | 60                 | 0   |
| one card deleted     | 59                 | 0   |
| that card restored   | 60                 | 1   |
| unchanged board      | 0                  | 0   |

The ranking `search` returns was captured for five queries over the same board
before and after, and is identical; `test_an_incremental_index_ranks_exactly_as
_a_full_rebuild_does` keeps that true rather than trusting the one comparison.
"""
import pytest
from langchain_core.embeddings import Embeddings

from lodestar_brain import retrieval
from lodestar_brain.retrieval import cards as cards_module

MADE_ON = 1773135000000


def card(id, title, notes=''):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': notes,
            'type': 'task', 'category': 'work', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'plan': '', 'createdAt': MADE_ON,
            'updatedAt': MADE_ON}


BOARD = [card('c1', 'renew the visa', notes='appointment at the embassy'),
         card('c2', 'book the dentist'),
         card('c3', 'piano practice every evening'),
         card('c4', 'compare mortgage offers'),
         card('c5', 'plant the balcony herbs')]


class CountingEmbeddings(Embeddings):
    """The lexical embedder, counting the card texts it was asked for — and able
    to fall over on demand, which is the only way to observe a rebuild that
    stopped half way.

    `model_name` is an instance attribute because that is what a real backend
    has (`SentenceTransformerEmbeddings.model_name`) and what the index's
    namespace is derived from. Changing it here is how a test says "a different
    embedding model" without downloading one.
    """

    def __init__(self, model_name: str = 'fake-lexical-v1'):
        self.model_name = model_name
        self.inner = retrieval.LexicalHashEmbeddings()
        self.documents = 0
        self.fail = False

    def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError('the encoder fell over mid-rebuild')
        self.documents += len(texts)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)


def ranked(index, query):
    return [doc.id for doc in index.search(query)]


# This is a unit test.
def test_editing_one_card_re_embeds_only_that_card():
    """The acceptance criterion is a number: one card of five changed must cost
    one card's worth of vectors, not five. The reused records are checked by the
    identity of the vector object they hold — value equality would also pass
    against a re-embed, which is exactly what this test exists to catch."""
    embeddings = CountingEmbeddings()
    index = retrieval.CardIndex(embeddings)
    assert index.build(BOARD) is True
    assert embeddings.documents == 5
    before = {id: dict(record) for id, record in index.store.store.items()}

    edited = [dict(BOARD[0], title='renew the visa urgently')] + BOARD[1:]
    assert index.build(edited) is True
    assert embeddings.documents == 6, 'one edit, one embedding'
    assert index.current and index.generation == 2
    after = index.store.store
    for id in ('c2', 'c3', 'c4', 'c5'):
        assert after[id]['vector'] is before[id]['vector'], \
            f'{id} was re-embedded although nothing about it changed'
        assert index.records[id] == cards_module._card_fingerprint(
            retrieval.card_document(next(c for c in edited if c['id'] == id)))
    assert after['c1']['vector'] != before['c1']['vector']
    assert 'urgently' in after['c1']['text']
    # Recall for the cards nobody touched still answers.
    assert ranked(index, 'dentist')[0] == 'c2'
    assert ranked(index, 'piano practice')[0] == 'c3'

    # A metadata-only move is a changed record and an unchanged vector: the
    # column has to reach the store, and the encoder has no opinion about it.
    moved = [dict(edited[0], columnId='answered')] + edited[1:]
    assert index.build(moved) is True
    assert embeddings.documents == 6, 'metadata does not reach the embedder'
    assert index.store.store['c1']['metadata']['columnId'] == 'answered'

    # An unchanged board is still free, and still says it did nothing.
    assert index.build(list(moved)) is False
    assert embeddings.documents == 6
    # A reorder is a rebuild — insertion order decides how score ties break —
    # but not an embedding.
    assert index.build(list(reversed(moved))) is True
    assert embeddings.documents == 6


# This is a unit test.
def test_an_incremental_index_ranks_exactly_as_a_full_rebuild_does():
    """This slice is about how the index is maintained, never about recall. An
    index walked through an edit, a delete, a restore and a reorder must answer
    identically to one built from the final board in one go — including the ties,
    which is why the queries include ones that match nothing in particular."""
    grown = retrieval.CardIndex(retrieval.LexicalHashEmbeddings())
    grown.build(BOARD)
    grown.build([dict(BOARD[0], title='renew the visa urgently')] + BOARD[1:])
    grown.build(BOARD[1:])
    final = [dict(BOARD[0], title='renew the visa urgently'), BOARD[2], BOARD[1],
             BOARD[3], BOARD[4]]
    grown.build(final)

    fresh = retrieval.CardIndex(retrieval.LexicalHashEmbeddings())
    fresh.build(final)
    for query in ('visa embassy', 'dentist', 'piano', 'mortgage offers',
                  'herbs on the balcony', 'work task', 'nothing like this'):
        assert ranked(grown, query) == ranked(fresh, query), query
    assert grown.fingerprint == fresh.fingerprint


# This is a unit test.
def test_a_different_embedding_model_rebuilds_the_whole_board():
    """The namespace is what a model change moves, and it has to be a *different*
    question from "did this card change?": the documents are identical across the
    swap, so a card-level comparison alone would reuse vectors from the old
    model's space and rank noise without raising."""
    embeddings = CountingEmbeddings()
    index = retrieval.CardIndex(embeddings)
    index.build(BOARD)
    assert embeddings.documents == 5
    # The whole-index fingerprint, which is over the namespace as well as the
    # cards: the documents are byte-identical across the swap, so an identity
    # derived from them alone would call two vector spaces the same index.
    fingerprint = index.fingerprint

    embeddings.model_name = 'fake-lexical-v2'
    assert index.build(BOARD) is True, 'a new model went unnoticed'
    assert embeddings.documents == 10, 'a new model re-embeds every card'
    assert index.fingerprint != fingerprint
    assert index.current and index.generation == 2
    assert ranked(index, 'dentist')[0] == 'c2'

    # And the escape hatch does the same thing without a model change, because
    # incremental maintenance is only ever as good as its invalidation.
    assert index.rebuild(BOARD) is True
    assert embeddings.documents == 15
    assert index.current


# This is a unit test.
def test_a_deleted_card_leaves_the_index_and_comes_back_for_one_embedding():
    embeddings = CountingEmbeddings()
    index = retrieval.CardIndex(embeddings)
    index.build(BOARD)
    assert embeddings.documents == 5

    assert index.build([c for c in BOARD if c['id'] != 'c2']) is True
    assert embeddings.documents == 5, 'a deletion embeds nothing'
    assert 'c2' not in index.records and 'c2' not in index.store.store
    assert 'c2' not in ranked(index, 'dentist'), 'a deleted card was recalled'
    assert ranked(index, 'piano practice')[0] == 'c3'

    assert index.build(BOARD) is True
    assert embeddings.documents == 6, 'a restore costs the one card, not five'
    assert ranked(index, 'dentist')[0] == 'c2'


# This is a unit test.
def test_an_interrupted_rebuild_never_claims_to_be_current():
    """Two failures, and they must not be handled the same way.

    A failed *content* rebuild leaves the previous generation whole — its vectors
    are still the ones the query embedder produces, so serving them is right and
    the only lie would be calling the index current. A failed rebuild after a
    *model* change cannot serve anything: the old vectors belong to another
    space, so they are dropped before the work starts and the index answers with
    nothing rather than with noise.
    """
    embeddings = CountingEmbeddings()
    index = retrieval.CardIndex(embeddings)
    index.build(BOARD)
    assert index.current and index.generation == 1

    embeddings.fail = True
    edited = [dict(BOARD[0], title='renew the visa urgently')] + BOARD[1:]
    with pytest.raises(RuntimeError):
        index.build(edited)
    assert index.current is False, 'a half-done rebuild reported itself current'
    assert index.generation == 1
    assert ranked(index, 'dentist')[0] == 'c2', 'the last good index was lost'

    # The next attempt must actually do the work — the manifest still describes
    # the board before the edit, so a short-circuit here would strand the index.
    embeddings.fail = False
    assert index.build(edited) is True
    assert index.current and index.generation == 2
    assert ranked(index, 'visa urgently')[0] == 'c1'

    # A model change that fails takes the old vector space with it.
    embeddings.model_name = 'fake-lexical-v3'
    embeddings.fail = True
    with pytest.raises(RuntimeError):
        index.build(edited)
    assert index.current is False
    assert index.search('dentist') == [], \
        'vectors from the retired model were still being searched'
    embeddings.fail = False
    assert index.build(edited) is True
    assert index.current and ranked(index, 'dentist')[0] == 'c2'
