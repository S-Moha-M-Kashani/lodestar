"""The retrieval module's foundation: what gets embedded, and how.

Group 2 of the LangChain migration. Everything here is offline — no extra, no
download, no socket — because the brain suite has to stay that way. The two
model-backed embedders are exercised through their `factory` seam, so the
prefix behaviour is tested without a 2 GB checkpoint.
"""
import re
from datetime import datetime, timezone

import pytest
from langchain_core.embeddings import Embeddings

from lodestar_brain import retrieval

# A fixed instant, so the expected date int is readable rather than arithmetic.
MADE_ON = int(datetime(2026, 3, 10, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)


def card(id, title, notes='', tags=None, category=''):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': notes,
            'type': 'question', 'category': category, 'importance': '',
            'urgency': '', 'num': 1, 'tags': tags or [],
            'createdAt': MADE_ON, 'updatedAt': MADE_ON}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class StubEncoder:
    """Records exactly what it was asked to encode. The point of the test is
    the text that reaches the model, not the vector that comes back."""

    def __init__(self):
        self.seen = []

    def encode(self, texts, **kwargs):
        self.seen.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    def get_embedding_dimension(self):
        return 2


# This is a unit test.
def test_lexical_hash_embeddings_rank_by_shared_text():
    embed = retrieval.LexicalHashEmbeddings()
    vectors = embed.embed_documents(['kubernetes pod autoscaling',
                                     'kubernetes pod autoscaling',
                                     'structure hiring interviews'])
    assert len(vectors[0]) == embed.dim
    assert abs(dot(vectors[0], vectors[0]) - 1.0) < 1e-5   # unit length
    assert dot(vectors[0], vectors[1]) > 0.999             # deterministic
    query = embed.embed_query('kubernetes pod scaling')
    assert dot(query, vectors[0]) > dot(query, vectors[2])
    # Why this replaces `hash`: its tokeniser was `[a-z0-9]+`, so a Farsi
    # sentence held no tokens and every vector was the zero vector — which made
    # every offline ranking assertion a tie rather than a test.
    farsi = embed.embed_documents(['امروز صبح دویدم و حالم خوب بود'])[0]
    assert any(farsi)
    assert not any(embed.embed_query(''))  # no text, no signal, no crash


# This is a unit test.
def test_the_embedder_seam_names_a_backend_and_resolves_its_model():
    assert isinstance(retrieval.make_embeddings('fake'), Embeddings)
    # 'hash' is retired *by name*, so a stale BRAIN_EMBEDDER=hash raises instead
    # of quietly selecting whatever replaced it.
    for kind in ('hash', 'auto', 'nonsense'):
        with pytest.raises(ValueError):
            retrieval.make_embeddings(kind)
    assert (retrieval.resolve_embed_model('sentence-transformers')
            == retrieval.DEFAULT_LOCAL_MODEL)
    assert (retrieval.resolve_embed_model('fastembed')
            == retrieval.DEFAULT_FASTEMBED_MODEL)
    # An explicitly named model is never replaced by a backend default: a
    # configuration labelled one model and served by another is unfalsifiable.
    pinned = 'intfloat/multilingual-e5-small'
    assert retrieval.resolve_embed_model('fastembed', model=pinned) == pinned
    assert retrieval.resolve_embed_model('fake') == ''


# This is a unit test.
def test_a_prefixed_model_embeds_queries_and_passages_as_it_was_trained_to():
    # The E5 family was trained on 'query: ' / 'passage: '. Dropping the
    # prefixes costs accuracy and raises nothing — the silent loss this seam
    # exists to prevent.
    assert (retrieval.EMBED_PREFIXES['intfloat/multilingual-e5-small']
            == ('query: ', 'passage: '))
    stub = StubEncoder()
    embed = retrieval.SentenceTransformerEmbeddings(
        'intfloat/multilingual-e5-small', query_prefix='query: ',
        passage_prefix='passage: ', factory=lambda name: stub)
    embed.embed_documents(['a diary entry'])
    embed.embed_query('what happened')
    assert stub.seen == ['passage: a diary entry', 'query: what happened']
    # The prefixes belong to the model, not to the backend: the Persian default
    # was trained without them and must stay symmetric.
    assert retrieval.EMBED_PREFIXES.get(retrieval.DEFAULT_LOCAL_MODEL, ('', '')) == ('', '')
    plain_stub = StubEncoder()
    plain = retrieval.SentenceTransformerEmbeddings(
        retrieval.DEFAULT_LOCAL_MODEL, factory=lambda name: plain_stub)
    plain.embed_documents(['a diary entry'])
    plain.embed_query('what happened')
    assert plain_stub.seen == ['a diary entry', 'what happened']


# This is a unit test.
def test_long_chat_text_splits_into_overlapping_chunks():
    text = ' '.join(f'sentence {i:02d} about the tax office and its long tail.'
                    for i in range(60))
    chunks = retrieval.split_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= retrieval.CHUNK_SIZE for chunk in chunks)
    # Overlap is the whole reason for the recursive splitter: a thought cut in
    # half must still be whole in one of the two windows.
    ids = [set(re.findall(r'sentence (\d\d)', chunk)) for chunk in chunks[:2]]
    assert ids[0] & ids[1]
    assert retrieval.split_text('یک جمله کوتاه') == ['یک جمله کوتاه']
    assert retrieval.split_text('   ') == []


# This is a unit test.
def test_a_card_becomes_one_document_every_filter_can_see():
    doc = retrieval.card_document(
        card('c1', 'Renew the visa', notes='book the appointment',
             tags=['admin', 'travel'], category='travel'))
    assert doc.id == 'c1'
    for piece in ('Renew the visa', 'book the appointment', 'admin'):
        assert piece in doc.page_content
    assert doc.metadata['created_day'] == 20260310
    assert doc.metadata['tags'] == 'admin travel'
    # Every key on every document. A field only some rows carry turns a `where`
    # clause into a silent partial scan, which reads as a retrieval bug rather
    # than the schema bug it is.
    sparse = retrieval.card_document({'id': 'c2', 'title': 'Just a title'})
    assert set(sparse.metadata) == set(doc.metadata) == set(retrieval.CARD_META_KEYS)
    assert sparse.metadata['category'] == ''
    assert sparse.metadata['created_day'] == 0   # outside every real date range


# This is a unit test.
def test_metadata_stays_filterable_when_a_value_is_not_a_scalar():
    flat = retrieval.flatten_metadata(
        {'layer': 'chat', 'k': 3, 'tags': ['work', 'money'], 'mood': None,
         'usage': {'tokens': 12}})
    assert flat['tags'] == 'work money'   # joined, not JSON: a filter can read it
    assert 'mood' not in flat             # absent beats null — Chroma rejects null
    assert flat['usage_json'] == '{"tokens": 12}'
    assert flat['layer'] == 'chat' and flat['k'] == 3
    assert retrieval.flatten_metadata({}) == {}
