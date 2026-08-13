"""The retrieval module's foundation: what gets embedded, and how.

Group 2 of the LangChain migration. Everything here is offline — no extra, no
download, no socket — because the brain suite has to stay that way. The two
model-backed embedders are exercised through their `factory` seam, so the
prefix behaviour is tested without a 2 GB checkpoint.
"""
import asyncio
import json
import re
import time
from datetime import date, datetime, timezone
from uuid import uuid4

import httpx
import pytest
import respx
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.retrievers import BaseRetriever

from lodestar_brain import retrieval
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat

# Fixed instants, so the expected date ints are readable rather than arithmetic.
MADE_ON = int(datetime(2026, 3, 10, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
LONG_AGO = int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)


def card(id, title, notes='', tags=None, category=''):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': notes,
            'type': 'question', 'category': category, 'importance': '',
            'urgency': '', 'num': 1, 'tags': tags or [],
            'createdAt': MADE_ON, 'updatedAt': MADE_ON}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


FA_DOCS = [
    Document(id='d1', page_content='رفتم اداره مالیات و جریمه رو پرداخت کردم'),
    Document(id='d2', page_content='با مهسا دعوامون شد سر خرید خونه'),
    Document(id='d3', page_content='صبح دویدم و حالم بهتر شد'),
    Document(id='d4', page_content='می‌خوام برم سفر شمال'),
]
BY_ID = {doc.id: doc for doc in FA_DOCS}


def fixed(ids):
    """A retriever that always answers with these documents, in this order.
    Fusion is what is under test, so the halves being fused are made boring."""

    class Fixed(BaseRetriever):
        def _get_relevant_documents(self, query, *, run_manager=None):
            return [BY_ID[id] for id in ids]

    return Fixed()


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


class ScriptedChat(BaseChatModel):
    """A chat model that answers with `reply`, optionally slowly or not at all,
    and records what it was asked. Enough to test a gate without a provider."""
    reply: str = ''
    delay: float = 0.0
    fail: bool = False
    calls: int = 0
    seen: str = ''

    @property
    def _llm_type(self) -> str:
        return 'scripted'

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.seen = '\n'.join(str(message.content) for message in messages)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError('the provider fell over')
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=self.reply))])


class CountingEmbeddings(Embeddings):
    """The lexical embedder, counting how many documents it was asked to embed —
    which is what the card index's fingerprint exists to keep down."""

    def __init__(self):
        self.inner = retrieval.LexicalHashEmbeddings()
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)


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


# This is a unit test.
def test_bm25_finds_the_rare_literal_a_dense_search_smooths_away():
    bm25 = retrieval.RankBM25Retriever.from_documents(FA_DOCS, k=2)
    # EnsembleRetriever cannot tell ours from langchain-community's, which is
    # the whole reason not to depend on a package that announces its sunset.
    assert isinstance(bm25, BaseRetriever)
    hits = bm25.invoke('مالیات')
    assert [doc.id for doc in hits][0] == 'd1'
    assert len(hits) <= 2
    # Tokenised by ours, not by str.split: «می‌خوام» and «می خوام» are one word,
    # and a whitespace tokeniser makes them two postings that never meet.
    assert bm25.invoke('می خوام')[0].id == 'd4'
    assert retrieval.RankBM25Retriever.from_documents([]).invoke('anything') == []


# This is a unit test.
def test_hybrid_fusion_rewards_agreement_over_one_strong_vote():
    hybrid = retrieval.hybrid_retriever(fixed(['d1', 'd2', 'd3']),
                                        fixed(['d4', 'd2', 'd1']))
    assert hybrid.weights == [0.5, 0.5]   # neither half is worth more
    fused = [doc.id for doc in hybrid.invoke('anything')]
    # d2 is second on both lists, d3 and d4 top only one. RRF sums reciprocal
    # ranks, so two mid votes beat one first place — which is why it needs no
    # score calibration between a cosine and a BM25 score.
    assert set(fused[:2]) == {'d1', 'd2'}
    assert fused.index('d2') < fused.index('d4')
    # And the real BM25 half plugs into it unchanged.
    with_bm25 = retrieval.hybrid_retriever(
        fixed(['d3']), retrieval.RankBM25Retriever.from_documents(FA_DOCS))
    assert 'd1' in {doc.id for doc in with_bm25.invoke('مالیات')}


# This is a unit test.
def test_the_lexical_rerank_promotes_the_document_that_covers_the_question():
    idf = retrieval.RankBM25Retriever.from_documents(FA_DOCS).idf
    question = 'جریمه مالیات چقدر شد؟'
    order = ['d3', 'd4', 'd1', 'd2']
    ranked = retrieval.lexical_rerank(question, [BY_ID[id] for id in order], idf,
                                      k=4)
    assert [doc.id for doc in ranked][0] == 'd1'   # the only one that covers it
    # A question of nothing but interrogatives has no informative words to
    # cover, so coverage is zero rather than an accident of stopword overlap.
    assert retrieval.coverage('چی شد؟', FA_DOCS[0].page_content, idf) == 0.0
    # Only the first RERANK_DEPTH candidates are read — the reranker is the
    # expensive stage, and what it never sees keeps no place in the result.
    padding = [Document(id=f'x{i}', page_content='حالم خوب بود')
               for i in range(retrieval.RERANK_DEPTH)]
    deep = retrieval.lexical_rerank(question, padding + [BY_ID['d1']], idf, k=3)
    assert 'd1' not in {doc.id for doc in deep}


# This is a unit test.
def test_the_reranker_seam_names_a_backend_and_an_unknown_one_raises():
    """No 'auto' here either: BRAIN_RERANKER names one, and anything else is a
    boot error rather than a silent downgrade to the free one. The hosted
    backend refuses to build keyless for the reason `make_url_safety` does — a
    keyless reranker would fail open on every question while the configuration
    claimed a cross-encoder."""
    # The shipped backend is the function itself: the seam wraps nothing.
    assert retrieval.make_reranker('lexical') is retrieval.lexical_rerank
    assert isinstance(retrieval.make_reranker('fake'), retrieval.FakeReranker)
    with pytest.raises(ValueError, match='unknown reranker'):
        retrieval.make_reranker('auto')
    with pytest.raises(ValueError, match='OPENROUTER_API_KEY'):
        retrieval.make_reranker('openrouter', Settings())
    # The roadmap's model is the hosted backend's own default, not the pipeline's
    # default backend; an explicitly pinned one is never replaced.
    keyed = Settings(openrouter_api_key='sk-test')
    assert retrieval.make_reranker('openrouter', keyed).model \
        == 'cohere/rerank-4-fast'
    assert retrieval.make_reranker(
        'openrouter', Settings(openrouter_api_key='sk-test',
                               rerank_model='cohere/rerank-4-pro')).model \
        == 'cohere/rerank-4-pro'


# This is a unit test.
def test_the_fake_reranker_reorders_by_relevance_rather_than_by_position():
    """The offline backend has to move documents, or every test written against
    it asserts nothing.

    It also has to move them the way a *cross-encoder* would, which is the one
    thing `lexical_rerank` cannot do: at the shipped depth its 50/50 blend of
    position and coverage cannot promote a last-placed candidate past a
    first-placed one. The same input is run through both to pin that difference,
    since a fake that merely re-derived the lexical order would hide it.
    """
    fake = retrieval.FakeReranker()
    question = 'جریمه مالیات چقدر شد؟'
    idf = retrieval.RankBM25Retriever.from_documents(FA_DOCS).idf
    # Not a pass-through: the answer differs from the order it was handed.
    order = ['d3', 'd4', 'd2', 'd1']
    ranked = [doc.id for doc in fake(question, [BY_ID[id] for id in order],
                                     idf={}, k=4)]
    assert ranked != order
    assert ranked[0] == 'd1'      # the only card the question is about
    # `idf={}` above is the point of the empty dict: corpus statistics are on the
    # interface for the lexical backend's sake, and a model-scored one ignores
    # them. This backend must never need them to rank.
    buried = [Document(id=f'x{i}', page_content='حالم خوب بود')
              for i in range(retrieval.RERANK_DEPTH - 1)] + [BY_ID['d1']]
    assert fake(question, buried, idf={}, k=3)[0].id == 'd1'
    assert retrieval.lexical_rerank(question, buried, idf, k=3)[0].id != 'd1'


# This is a unit test.
@respx.mock
def test_the_hosted_reranker_orders_by_the_score_it_is_given_and_fails_open():
    """One POST to OpenRouter's own /rerank — no SDK, no second credential.

    Two properties beyond the wire format. The ranking is rebuilt from
    `relevance_score` here rather than trusted from the response order, because
    the order handed to the answerer is this pipeline's to guarantee. And a
    reranker that cannot be reached returns the *fused* order: never a 500 in the
    middle of a search, and never a quiet fallback to `lexical_rerank`, which
    would be one backend serving another's configuration.
    """
    reranker = retrieval.make_reranker(
        'openrouter', Settings(openrouter_api_key='sk-test'))
    # Deliberately worst-score-first, so passing the response through unchanged
    # fails this test.
    route = respx.post('https://openrouter.ai/api/v1/rerank').mock(
        return_value=httpx.Response(200, json={'results': [
            {'index': 0, 'relevance_score': 0.11},
            {'index': 3, 'relevance_score': 0.98}]}))
    ranked = reranker('مالیات', FA_DOCS, idf={}, k=2)
    assert [doc.id for doc in ranked] == ['d4', 'd1']
    sent = json.loads(route.calls.last.request.content)
    assert sent['model'] == 'cohere/rerank-4-fast'
    assert sent['query'] == 'مالیات'
    assert sent['documents'] == [doc.page_content for doc in FA_DOCS]
    assert sent['top_n'] == 2
    assert route.calls.last.request.headers['authorization'] == 'Bearer sk-test'

    respx.post('https://openrouter.ai/api/v1/rerank').mock(
        side_effect=httpx.ConnectError('rerank is down'))
    assert [doc.id for doc in reranker('مالیات', FA_DOCS, idf={}, k=2)] \
        == ['d1', 'd2']


# This is a unit test.
def test_the_card_index_reranks_with_the_reranker_it_was_given():
    """The seam has to reach the pipeline, not just exist beside it: create_app
    chooses a reranker and the index is where it has to be used. The spy also
    pins what the stage is handed — the *expanded* query, and the idf of the very
    retrieval being reranked, so BM25 and the reranker cannot disagree about
    what a rare word is."""
    seen: dict = {}

    def spy(query, documents, idf, k=retrieval.TOP_K,
            depth=retrieval.RERANK_DEPTH):
        seen.update(query=query, idf=idf, count=len(documents))
        return list(documents)[:1]

    index = retrieval.CardIndex(retrieval.LexicalHashEmbeddings(), rerank=spy)
    index.build([card('c1', 'رفتم اداره مالیات', notes='جریمه رو دادم'),
                 card('c2', 'صبح دویدم')])
    hits = index.search('مالیات', k=3)
    assert len(hits) == 1                  # the seam decided the cut, not search
    assert 'مالیات' in seen['query'] and seen['count'] >= 1
    assert seen['idf'], 'the reranker got no corpus statistics to weigh terms by'
    # And an index built without one stays on the measured pipeline.
    assert retrieval.CardIndex(retrieval.LexicalHashEmbeddings()).rerank \
        is retrieval.lexical_rerank


# This is a unit test.
def test_time_language_resolves_to_a_date_range():
    today = date(2026, 3, 10)

    def scope(question):
        return retrieval.resolve_time_scope(question, today)

    # Farsi: «آذر» means the آذر that has already
    # happened, and the Jalali month is mapped to its Gregorian window.
    assert (scope('آذر چی شد؟').from_int, scope('آذر چی شد؟').to_int) \
        == (20251122, 20251221)
    # پارسال is Nowruz-to-Nowruz, so on 10 March 2026 it is still Persian year
    # 1404 and «پارسال تابستون» is the summer of 2024, not 2025.
    assert (scope('پارسال تابستون چطور بود').from_int,
            scope('پارسال تابستون چطور بود').to_int) == (20240622, 20240922)
    assert scope('دیروز').from_int == scope('دیروز').to_int == 20260309

    # English is deliberately not a mirror: "last summer" means the most recent
    # summer, where «پارسال تابستون» shifts a further year because the Persian
    # year has not turned yet. Coverage of the English half is measured in
    # brain/tests/evals/test_timescope_english.py.
    assert (scope('how was last summer').from_int,
            scope('how was last summer').to_int) == (20250622, 20250922)
    # The same two questions in August, when the summer is the one in progress
    # — the anchor at which "last" has to be read rather than inferred from
    # which window has started. Here the Persian year *has* turned, so both
    # halves name the same summer by two different routes.
    august = date(2026, 8, 13)
    for question in ('how was last summer', 'پارسال تابستون چطور بود'):
        window = retrieval.resolve_time_scope(question, august)
        assert (window.from_int, window.to_int) == (20250622, 20250922), question
    assert (scope('what did I do last month').from_int,
            scope('what did I do last month').to_int) == (20260201, 20260228)
    assert (scope('anything from 2024').from_int,
            scope('anything from 2024').to_int) == (20240101, 20241231)
    assert scope('what should I work on') is None


# This is a unit test.
def test_a_time_scope_filters_both_stores_by_the_same_rule():
    scope = retrieval.resolve_time_scope('anything from 2025', date(2026, 3, 10))
    # BM25 and the in-memory card index filter in Python; Chroma filters with a
    # where dict. Both come from the one scope: if they could disagree, hybrid
    # fusion would silently compare two different candidate pools.
    inside = {'created_day': 20250701, 'updated_day': 20250701}
    touched = {'created_day': 20241231, 'updated_day': 20250101}
    outside = {'created_day': 20240101, 'updated_day': 20240102}
    assert scope.matches(inside) and scope.matches(touched)
    assert not scope.matches(outside)
    assert retrieval.where_clause(scope) == {'$or': [
        {'$and': [{'created_day': {'$gte': 20250101}},
                  {'created_day': {'$lte': 20251231}}]},
        {'$and': [{'updated_day': {'$gte': 20250101}},
                  {'updated_day': {'$lte': 20251231}}]}]}
    # One date field needs no $or — the chat store records a single date.
    assert retrieval.where_clause(scope, fields=('created_day',)) == {
        '$and': [{'created_day': {'$gte': 20250101}},
                 {'created_day': {'$lte': 20251231}}]}
    assert retrieval.where_clause(None) is None


# This is a unit test.
def test_expansion_reaches_the_words_the_corpus_actually_uses():
    question = 'دعوا با همسرم سر مالیات چی شد؟'
    variants = retrieval.expand_queries(question)
    assert variants[0] == question          # the question itself always leads
    assert any('مهسا' in variant for variant in variants)
    assert len(variants) == len(set(variants))
    # Cross-script expansion is always on: an English query grows a variant
    # with Persian spellings, so «ویسا» on the board is reachable from "visa".
    english = retrieval.expand_queries('renew the visa')
    assert english[0] == 'renew the visa'   # the question itself still leads
    assert any('ویسا' in variant for variant in english)
    # The LLM-backed alternative is LangChain's, wired to the chat model rather
    # than reimplemented. It is a seam, so it only has to answer.
    llm = FakeChat(script=[AIMessage(content='dispute with wife\ntax fine')])
    expanded = retrieval.multi_query(fixed(['d1']), llm)
    assert [doc.id for doc in expanded.invoke('anything')] == ['d1']


# This is a unit test.
def test_the_relevance_gate_asks_once_and_drops_what_the_model_rejects():
    # The gate is the one retrieval stage that calls a model, so it is a
    # coroutine: awaited, the loop keeps serving while the grader thinks.
    def gate(*args, **kwargs):
        return asyncio.run(retrieval.relevance_gate(*args, **kwargs))

    docs = [BY_ID['d1'], BY_ID['d2'], BY_ID['d3']]
    llm = ScriptedChat(reply='1: 9\n2: 0\n3: 8')
    assert [doc.id for doc in gate(llm, 'مالیات', docs)] == ['d1', 'd3']
    # One call for the whole batch, not one per candidate. Every candidate is in
    # that one prompt — a gate that costs k calls per question is the row an
    # OpenRouter credit never reaches.
    assert llm.calls == 1
    for doc in docs:
        assert doc.page_content[:15] in llm.seen
    # Every failure mode is a no-op rather than an empty context: an unparsed
    # line means "no opinion" (0.5), which clears the 0.4 threshold. A malformed
    # reply must not be able to silently delete the evidence.
    assert len(gate(ScriptedChat(reply='I cannot help'), 'مالیات', docs)) == 3
    assert len(gate(ScriptedChat(fail=True), 'مالیات', docs)) == 3
    idle = ScriptedChat(reply='1: 9')
    assert gate(idle, 'مالیات', []) == []
    assert idle.calls == 0
    # The gate is a named seam like every other in the brain: 'none' turns it
    # off, and an unknown value raises rather than silently disabling it.
    assert retrieval.gate_llm('llm', llm) is llm
    assert retrieval.gate_llm('none', llm) is None
    with pytest.raises(ValueError):
        retrieval.gate_llm('lexical', llm)


# This is a unit test.
def test_the_gate_abandons_a_call_that_blows_its_latency_budget():
    docs = [BY_ID['d1'], BY_ID['d2']]
    slow = ScriptedChat(reply='1: 0\n2: 0', delay=2.0)

    async def timed():
        """Timed inside the loop, which is where the guarantee is.

        `asyncio.timeout` cancels the *await*, so the caller is released on the
        budget while the abandoned call finishes wherever it was running — for a
        model with no async of its own, a worker thread, exactly as with the
        daemon thread this replaced. Timing `asyncio.run` instead would measure
        the loop joining that thread on the way out, which no route ever does.
        """
        start = time.perf_counter()
        kept = await retrieval.relevance_gate(slow, 'مالیات', docs,
                                              budget_s=0.05)
        return kept, time.perf_counter() - start

    kept, waited = asyncio.run(timed())
    # The gate is an optimisation that measured a *tie* with no gate at all, so
    # a slow model costs the gate, never the answer.
    assert waited < 1.0
    assert [doc.id for doc in kept] == ['d1', 'd2']


# This is a unit test.
def test_the_card_index_re_embeds_only_when_the_board_changed():
    cards = [card('c1', 'Renew the visa'), card('c2', 'Book the dentist')]
    embeddings = CountingEmbeddings()
    index = retrieval.CardIndex(embeddings)
    index.build(cards)
    assert embeddings.documents == 2
    index.build(list(cards))          # the same board, a different list object
    assert embeddings.documents == 2  # a per-tool-call rebuild costs nothing
    # An edit is a different board even at the same length: the fingerprint
    # covers what gets indexed, not how many rows there are.
    index.build([dict(cards[0], title='Renew the visa urgently'), cards[1]])
    assert embeddings.documents == 4
    index.build([cards[1]])
    assert embeddings.documents == 5
    assert [doc.id for doc in index.search('dentist')] == ['c2']


# This is a unit test.
def test_the_card_index_search_runs_the_whole_pipeline():
    cards = [card('c1', 'Renew the visa', notes='appointment at the embassy'),
             card('c2', 'Book the dentist'),
             dict(card('c3', 'Old thing'), createdAt=LONG_AGO, updatedAt=LONG_AGO)]
    index = retrieval.CardIndex(retrieval.LexicalHashEmbeddings())
    index.build(cards)
    assert [doc.id for doc in index.search('visa embassy')][0] == 'c1'
    # The question's own time language filters before ranking, so a card outside
    # the window cannot win on relevance.
    scoped = index.search('what did I do in 2024', today=date(2026, 3, 10))
    assert {doc.id for doc in scoped} == {'c3'}
    # The gate only runs when a model is handed over, and when it rejects
    # everything the caller gets nothing — which is how an honest "no" happens.
    # It lives on `asearch`: `search` is local and CPU-bound and stays
    # synchronous, and the gate is the one stage that waits on a model.
    assert asyncio.run(index.asearch(
        'visa', llm=ScriptedChat(reply='1: 0\n2: 0\n3: 0'))) == []


# This is an integration test: it runs a real Chroma client, in process.
def test_the_chat_store_records_a_snippet_and_recalls_it():
    store = retrieval.ChatStore(retrieval.MEMORY_URL,
                                retrieval.LexicalHashEmbeddings(),
                                collection=f'chat-test-{uuid4().hex}')
    assert store.search('مالیات') == []   # an empty store is not an error
    store.record(['رفتم اداره مالیات و جریمه رو دادم', 'صبح دویدم و حالم خوب بود'],
                 metadata={'source': 'chat', 'tags': ['money', 'admin']})
    hits = store.search('مالیات', k=1)
    assert len(hits) == 1
    assert 'مالیات' in hits[0]['text']
    assert hits[0]['metadata']['tags'] == 'money admin'   # flattened on the way in
    assert 0.0 <= hits[0]['score'] <= 1.0
    store.record([f'یادداشت شماره {i} دربارهٔ مالیات' for i in range(5)])
    assert len(store.search('مالیات', k=3)) == 3   # k is a cap, not a hint
    before = store.count()
    store.record(['   ', ''])
    assert store.count() == before   # blank text is not a memory
    store.drop()


# This is an integration test: a real Chroma client, in process, and no disk.
def test_the_memory_url_never_touches_disk_or_a_server(tmp_path, monkeypatch):
    # Regression: a path-based client accepts 'memory' as a *relative
    # directory* and silently persists there, so every "offline" assertion
    # would pass while writing a chroma.sqlite3 into the repo root.
    monkeypatch.chdir(tmp_path)
    store = retrieval.ChatStore(retrieval.MEMORY_URL,
                                retrieval.LexicalHashEmbeddings(),
                                collection='chat-nodisk')
    store.record(['this must live in process only'])
    assert store.search('in process', k=1)
    assert list(tmp_path.iterdir()) == [], \
        f"'memory' was written to disk as {[p.name for p in tmp_path.iterdir()]}"
    # Construction must not need a live server for the resolved target to be
    # inspectable, or a misconfigured url is invisible until the first record.
    assert retrieval.parse_chroma_url('http://localhost:8001') \
        == ('localhost', 8001, False)
    assert retrieval.parse_chroma_url('https://chroma.internal') \
        == ('chroma.internal', 443, True)
