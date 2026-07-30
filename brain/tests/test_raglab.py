"""Tests for the RAG Lab (brain/tests/raglab).

Fully offline: Chroma runs in-process (`memory`), embeddings are the lab's
hash embedders, and no test touches an LLM. The integration tests run against
the real one-year fixture rather than a toy corpus, because the properties worth
asserting — that a Farsi question finds its evidence session, that the current
production embedder finds nothing at all — only exist at that scale.
"""
import os
from dataclasses import replace

import numpy as np
import pytest

from lodestar_brain.llm.fake import FakeChat

from .raglab import (chunking, config, corpus, embedding, evaluate, explain,
                     metrics, models, pipeline, query, retrieval, summarize,
                     textnorm)
from .raglab.config import (EMBEDDERS, GenerationConfig, IndexConfig, LabConfig,
                            LabSettings, RetrievalConfig)
from .raglab.index import IndexRegistry, LabIndex

LAB_SETTINGS = LabSettings(chroma_url='memory', chroma_database='raglab-tests',
                           openrouter_api_key='')


# --- fixtures --------------------------------------------------------------

@pytest.fixture(scope='module')
def diary():
    return corpus.load_diary()


@pytest.fixture(scope='module')
def ground_truth():
    return corpus.load_ground_truth()


@pytest.fixture(scope='module')
def registry(diary):
    return IndexRegistry(LAB_SETTINGS, diary)


@pytest.fixture(scope='module')
def index(registry):
    """One shared index: semantic chunks + the whole summary hierarchy, on the
    strongest offline embedder."""
    return registry.get(IndexConfig(chunker='semantic-drift', embedder='char-hash',
                                    contextual=True))


@pytest.fixture(scope='module')
def session(diary):
    return next(s for s in diary['sessions'] if len(s['messages']) >= 6)


# --- text normalisation ----------------------------------------------------

def test_normalize_folds_arabic_letterforms_and_digits():
    assert textnorm.normalize('يك') == textnorm.normalize('یک')
    assert '۱۴۰۵' not in textnorm.normalize('سال ۱۴۰۵')
    assert '1405' in textnorm.normalize('سال ۱۴۰۵')


def test_normalize_is_idempotent():
    once = textnorm.normalize('مي‌خواستم   بلاخره ۳ بار')
    assert textnorm.normalize(once) == once


def test_tokens_match_across_half_space_spelling():
    """«می‌خوام» and «می خوام» are the same word to a reader, so they must be
    the same token to BM25 — the corpus spells it both ways."""
    joined = set(textnorm.tokens('می‌خوام برم باشگاه'))
    spaced = set(textnorm.tokens('می خوام برم باشگاه'))
    assert joined & spaced
    assert 'باشگاه' in joined and 'باشگاه' in spaced


def test_tokens_drop_stopwords_but_keep_content():
    tokens = textnorm.tokens('که از به مهسا دعوا')
    assert 'مهسا' in tokens and 'دعوا' in tokens
    assert 'که' not in tokens


def test_sentences_split_spoken_run_ons():
    text = 'امروز رفتم سر کار و بعدش مهسا زنگ زد. خیلی خسته بودم'
    assert len(textnorm.sentences(text)) >= 2


# --- embedders -------------------------------------------------------------

def test_production_ascii_hash_embedder_is_blind_to_farsi():
    """The finding the lab exists to make measurable: the brain's default
    embedder tokenises [a-z0-9]+, so a Farsi diary embeds to the zero vector and
    retrieval is arbitrary. If this ever fails, the default was fixed."""
    vectors = embedding.make_embedder('ascii-hash').embed(['امروز با مهسا دعوام شد'])
    assert not np.any(vectors)


def test_char_hash_prefers_a_paraphrase_over_an_unrelated_line():
    embedder = embedding.make_embedder('char-hash')
    vectors = embedder.embed(['دعوا با مهسا سر کارهای خونه',
                              'باز با مهسا دعوا کردیم سر خونه',
                              'نامه اداره مالیات رسید'])
    assert float(vectors[0] @ vectors[1]) > float(vectors[0] @ vectors[2])


def test_token_hash_is_normalised_and_nonzero_for_farsi():
    vectors = embedding.make_embedder('token-hash').embed(['خواب بی‌خوابی کمردرد'])
    assert np.any(vectors)
    assert abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-5


# --- chunking --------------------------------------------------------------

@pytest.mark.parametrize('chunker', ('message', 'turn-pair', 'semantic-drift'))
def test_message_preserving_chunkers_cover_every_turn(session, chunker):
    """No message may be dropped. A chunker that loses turns loses evidence, and
    the ground truth cites evidence by message index."""
    cfg = IndexConfig(chunker=chunker, embedder='char-hash', contextual=False)
    chunks = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))
    covered = set()
    for chunk in chunks:
        covered.update(range(chunk.msg_start, chunk.msg_end + 1))
    assert covered == set(range(len(session['messages'])))


def test_every_chunker_produces_unique_ids_and_nonempty_text(session):
    embedder = embedding.make_embedder('char-hash')
    for chunker in ('fixed', 'fixed-overlap', 'message', 'turn-pair', 'session',
                    'semantic-drift'):
        cfg = IndexConfig(chunker=chunker, embedder='char-hash')
        chunks = chunking.chunk_session(session, cfg, embedder, 'خلاصه تستی')
        assert chunks, chunker
        assert len({c.id for c in chunks}) == len(chunks), chunker
        assert all(c.text.strip() for c in chunks), chunker


def test_fixed_chunker_matches_the_production_packing(session):
    """The baseline has to *be* the baseline: same greedy 500-char packing the
    brain ships, or the comparison is against a straw man."""
    from lodestar_brain.rag.chat_memory import chunk_text
    cfg = IndexConfig(chunker='fixed', chunk_chars=500, contextual=False)
    ours = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))
    theirs = chunk_text(corpus.session_text(session), 500)
    assert [c.text for c in ours] == theirs


def test_contextual_prefix_situates_the_chunk(session):
    cfg = IndexConfig(chunker='message', contextual=True)
    chunk = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'),
                                   'خلاصه‌ی نشست برای تست.')[0]
    assert session['date'] in chunk.prefix
    assert session['mood']['label'] in chunk.prefix
    assert chunk.body and not chunk.body.startswith('[')


def test_overlap_chunker_repeats_material_between_windows(session):
    cfg = IndexConfig(chunker='fixed-overlap', chunk_chars=300, overlap=150,
                      contextual=False)
    chunks = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))
    if len(chunks) < 2:
        pytest.skip('session too short to window')
    total = sum(len(c.text) for c in chunks)
    assert total > len(corpus.session_text(session))


def test_semantic_drift_cuts_at_an_explicit_topic_shift():
    fake = {'session_id': 'x-1', 'date': '2026-01-01', 'time': '22:00',
            'source': 'voice', 'mood': {'label': 'خسته', 'valence': 4, 'arousal': 5},
            'topics': [], 'recurring_threads': [],
            'messages': [
                {'role': 'user', 'intent': 'venting',
                 'content': 'امروز کل روز درگیر مالیات بودم و نامه اداره مالیات'},
                {'role': 'assistant', 'content': 'سخت بوده. چی شد آخرش؟'},
                {'role': 'user', 'intent': 'venting',
                 'content': 'حالا اینا رو ولش کن، مهسا سر کارهای خونه دوباره دعوا کرد'},
                {'role': 'assistant', 'content': 'چه حسی داشتی؟'}]}
    cfg = IndexConfig(chunker='semantic-drift', chunk_chars=500, contextual=False)
    chunks = chunking.chunk_session(fake, cfg, embedding.make_embedder('char-hash'))
    assert len(chunks) >= 2
    assert any('مهسا' in c.text and 'مالیات' not in c.text for c in chunks)


def test_chunk_metadata_is_chroma_safe(session):
    cfg = IndexConfig(chunker='message')
    chunk = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))[0]
    for key, value in chunk.metadata().items():
        assert isinstance(value, (str, int, float, bool)), key


def test_importance_rises_with_emotional_intensity():
    calm = {'mood': {'label': 'آروم', 'valence': 6, 'arousal': 2}}
    wrecked = {'mood': {'label': 'داغون', 'valence': 1, 'arousal': 9}}
    assert chunking.importance_of(wrecked) > chunking.importance_of(calm)


# --- summary hierarchy -----------------------------------------------------

def test_extractive_session_summary_is_shorter_and_uses_user_words(session):
    summarizer = summarize.ExtractiveSummarizer(summarize.build_idf([session]))
    summary = summarizer.session(session)
    assert summary
    assert len(summary) < len(corpus.session_text(session))


def test_month_layer_spans_exactly_its_month(diary):
    summaries = {s['session_id']: s['messages'][0]['content'][:120]
                 for s in diary['sessions']}
    months = summarize.month_layer(diary['sessions'], summaries)
    assert len(months) == len({s['date'][:7] for s in diary['sessions']})
    for chunk in months:
        month = chunk.id.removeprefix('month-')
        assert str(chunk.span_from).startswith(month.replace('-', ''))
        assert chunk.span_from <= chunk.span_to


def test_thread_layer_is_windowed_and_chronological(diary):
    summaries = {s['session_id']: s['messages'][0]['content'][:120]
                 for s in diary['sessions']}
    threads = summarize.thread_layer(diary['sessions'], summaries, diary['threads'])
    assert threads
    job = [c for c in threads if c.id.startswith('thread-job-search')]
    assert len(job) > 1, 'the busiest thread must be split into windows'
    assert [c.span_from for c in job] == sorted(c.span_from for c in job)
    assert all(c.threads == ('job-search',) for c in job)


def test_commitment_layer_captures_the_recurring_promise(diary):
    chunks = summarize.commitment_layer(diary['sessions'])
    assert chunks
    text = '\n'.join(c.text for c in chunks)
    assert 'باشگاه' in text, 'the gym promise is the corpus\'s signature commitment'


def test_summary_cache_is_keyed_by_content(tmp_path, session):
    cache = summarize.SummaryCache(tmp_path / 'cache.json')
    cache.put('extractive', session, 'خلاصه')
    cache.flush()
    assert summarize.SummaryCache(tmp_path / 'cache.json').get('extractive', session) \
        == 'خلاصه'
    edited = dict(session, messages=[dict(session['messages'][0], content='عوض شد')])
    assert cache.get('extractive', edited) is None


# --- query understanding ---------------------------------------------------

@pytest.mark.parametrize('question,expect_from,expect_to', [
    ('آذر چه خبر بود؟', 20251122, 20251221),
    ('پارسال پاییز حالم چطور بود؟', 20240923, 20241221),
    ('نوروز چی شد؟', 20260318, 20260404),
])
def test_time_scopes_resolve_to_the_right_window(question, expect_from, expect_to):
    scope = query.resolve_time_scope(question, '2026-07-28')
    assert scope is not None, question
    assert (scope.from_int, scope.to_int) == (expect_from, expect_to)


def test_untimed_question_has_no_scope():
    assert query.resolve_time_scope('چرا با مهسا دعوا می‌کنیم؟', '2026-07-28') is None


def test_relative_month_scope_is_the_previous_calendar_month():
    scope = query.resolve_time_scope('ماه پیش چی کار کردم؟', '2026-07-28')
    assert scope and (scope.from_int, scope.to_int) == (20260601, 20260630)


def test_where_clause_overlaps_rather_than_contains():
    """A thread rollup spans a year; requiring containment would filter out
    exactly the layer that answers a scoped question."""
    scope = query.TimeScope(20260101, 20260131, 'دی', 'jalali-month')
    clause = query.where_clause(scope, ('chunk', 'thread'),
                                ('chunk', 'session', 'month', 'thread'))
    assert clause['$and'][0] == {'span_from': {'$lte': 20260131}}
    assert clause['$and'][1] == {'span_to': {'$gte': 20260101}}
    assert {'layer': {'$in': ['chunk', 'thread']}} in clause['$and']


def test_expansion_adds_a_synonym_variant():
    variants = query.expand('دعوا با همسرم سر چی بود؟')
    assert len(variants) >= 2
    assert any('مهسا' in v for v in variants)


def test_keyword_query_strips_interrogatives():
    assert 'چی' not in query.keyword_query('حال مامان چی شد؟')


# --- retrieval primitives --------------------------------------------------

def test_bm25_finds_the_document_with_the_rare_term():
    bm25 = retrieval.BM25(['نامه اداره مالیات رسید و جریمه خوردم',
                           'با مهسا دعوا کردیم', 'رفتم پیاده‌روی'])
    top = bm25.top('مالیات جریمه', 2)
    assert top and top[0][0] == 0


def test_bm25_respects_the_allowed_mask():
    bm25 = retrieval.BM25(['مالیات', 'مالیات'])
    allowed = np.array([False, True])
    assert [i for i, _ in bm25.top('مالیات', 2, allowed)] == [1]


def test_rrf_ranks_a_document_both_retrievers_agree_on_first():
    fused = retrieval.rrf([['a', 'b', 'c'], ['b', 'a', 'd']])
    assert max(fused, key=fused.get) in ('a', 'b')
    assert fused['a'] > fused['c'] and fused['b'] > fused['d']


def test_mmr_breaks_up_near_duplicates():
    vectors = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    relevance = np.array([1.0, 0.99, 0.5], dtype=np.float32)
    assert retrieval.mmr(vectors, relevance, 2, 1.0) == [0, 1]
    assert retrieval.mmr(vectors, relevance, 2, 0.5) == [0, 2]


def test_mmr_falls_back_when_vectors_are_missing():
    relevance = np.array([0.2, 0.9], dtype=np.float32)
    assert retrieval.mmr(np.zeros((0, 2), dtype=np.float32), relevance, 2, 0.5) == [1, 0]


def test_recency_weight_halves_after_one_half_life():
    weight = retrieval.recency_weight(20260101, 20260701, 180.0)
    assert 0.4 < weight < 0.6


def test_llm_grade_parser_defaults_unscored_lines_to_neutral():
    class Reply:
        content = '1: 8\nnonsense\n3: 0'

    class Provider:
        def invoke(self, messages, **kwargs):
            return Reply()

    scores = retrieval.llm_scores(Provider(), 'm', 'q', ['a', 'b', 'c'])
    assert scores[0] == pytest.approx(0.8)
    assert scores[1] == pytest.approx(0.5)   # unparsed = no opinion
    assert scores[2] == pytest.approx(0.0)


# --- metrics ---------------------------------------------------------------

def test_retrieval_metric_arithmetic():
    retrieved, gold = ['a', 'x', 'b'], ['a', 'b', 'c']
    assert metrics.recall_at_k(retrieved, gold, 3) == pytest.approx(2 / 3)
    assert metrics.precision_at_k(retrieved, gold, 3) == pytest.approx(2 / 3)
    assert metrics.mrr(retrieved, gold) == 1.0
    assert metrics.hit_at_k(['x'], gold, 1) == 0.0
    assert metrics.ndcg_at_k(['a', 'b'], gold, 2) > metrics.ndcg_at_k(['x', 'a'], gold, 2)


def test_quote_recall_needs_the_answering_sentence_not_just_the_session():
    question = {'evidence': [{'session_id': 's1', 'message_indices': [0],
                              'quote': 'آذر تموم شد و از هیچ شرکتی هیچ خبری نیس'}]}
    assert metrics.quote_recall('حرف‌های دیگری از همان نشست', question) == 0.0
    assert metrics.quote_recall('گفتم آذر تموم شد و از هیچ شرکتی هیچ خبری نیس بعدش',
                                question) == 1.0


def test_quote_recall_tolerates_whitespace_normalisation():
    question = {'evidence': [{'quote': 'می خوام برم باشگاه', 'session_id': 's',
                              'message_indices': [0]}]}
    assert metrics.quote_recall('گفت می  خوام   برم باشگاه', question) == 1.0


def test_latest_state_session_is_the_newest_evidence():
    question = {'evidence': [{'session_id': '2025-12-01-a'},
                             {'session_id': '2026-05-12-a'}]}
    assert metrics.latest_state_session(question) == '2026-05-12-a'


def test_aggregate_reports_per_type_and_a_headline():
    rows = [
        {'id': 'q1', 'type': 'single-hop', 'difficulty': 'easy', 'answerable': True,
         'recall': 1.0, 'quote_recall': 1.0, 'ndcg': 1.0, 'hit': 1.0,
         'layers': ['chunk'], 'latency_ms': 5},
        {'id': 'q2', 'type': 'abstention', 'difficulty': 'hard', 'answerable': False,
         'abstained_correctly': 1.0, 'layers': [], 'latency_ms': 5},
    ]
    summary = metrics.aggregate(rows)
    assert summary['n_questions'] == 2
    assert summary['by_type']['single-hop']['recall'] == 1.0
    assert 0 < summary['overall']['headline'] <= 1.0
    assert summary['layer_usage'] == {'chunk': 1}


# --- index and pipeline (integration, in-process Chroma) -------------------

def test_index_builds_every_layer(index, diary):
    stats = index.stats
    assert stats.chunks > len(diary['sessions'])
    for layer in ('chunk', 'session', 'month', 'thread', 'commitment'):
        assert stats.by_layer.get(layer), layer
    assert stats.by_layer['month'] == 12
    assert stats.embed_dim == embedding.CHAR_DIM


def test_index_is_reused_for_the_same_fingerprint(registry):
    cfg = IndexConfig(chunker='message', embedder='token-hash', layers=('chunk',))
    first = registry.get(cfg)
    assert registry.get(cfg) is first
    assert first.stats.collection == cfg.collection()


def test_different_configs_get_different_collections():
    a = IndexConfig(chunker='fixed').collection()
    b = IndexConfig(chunker='session').collection()
    assert a != b and a.startswith('raglab-')


def test_retrieval_finds_the_evidence_session_for_a_known_question(index, ground_truth):
    """End-to-end on the real corpus: a hybrid retrieval over semantic chunks
    must surface at least one cited evidence session for most single-hop
    questions. Asserted as a rate, not per question — a single hard question
    should not be able to fail the suite."""
    questions = [q for q in ground_truth['questions']
                 if q['type'] == 'single-hop'][:10]
    cfg = RetrievalConfig(retriever='hybrid-rrf', k=8, reranker='lexical')
    hits = 0
    for question in questions:
        outcome = pipeline.retrieve(index, cfg, question['question_fa'],
                                    question['query_date'])
        gold = corpus.evidence_sessions(question)
        hits += metrics.hit_at_k(outcome.sessions, gold, cfg.k)
    assert hits >= 4, f'only {hits}/10 single-hop questions found any evidence'


def test_time_filter_narrows_the_candidate_pool(index, ground_truth):
    scoped = 'آذر چه خبر بود؟'
    with_filter = pipeline.retrieve(index, RetrievalConfig(time_filter=True),
                                    scoped, '2026-07-28')
    without = pipeline.retrieve(index, RetrievalConfig(time_filter=False),
                                scoped, '2026-07-28')
    assert with_filter.time_scope is not None
    assert (with_filter.diagnostics['candidates_in_scope']
            < without.diagnostics['candidates_in_scope'])
    dates = [corpus.date_int(c.date) for c in with_filter.contexts
             if c.layer == 'chunk']
    assert dates and all(20251122 <= d <= 20251221 for d in dates), dates


def test_grader_threshold_produces_an_abstention(index):
    """A question about something the diary never mentions must be refusable —
    and only the grader can refuse it."""
    nonsense = 'قرارداد خرید کشتی در بندر عباس چی شد؟'
    ungated = pipeline.retrieve(index, RetrievalConfig(grader='none'), nonsense,
                                '2026-07-28')
    gated = pipeline.retrieve(index, RetrievalConfig(grader='lexical',
                                                    grade_threshold=0.9),
                              nonsense, '2026-07-28')
    assert not ungated.abstained and ungated.contexts
    assert gated.abstained and not gated.contexts


def test_answerer_emits_the_refusal_when_abstaining(index):
    outcome = pipeline.retrieve(index, RetrievalConfig(grader='lexical',
                                                      grade_threshold=0.99),
                                'قرارداد کشتی', '2026-07-28')
    outcome = pipeline.answer(outcome, GenerationConfig(answerer='extractive'))
    assert outcome.answer == pipeline.REFUSAL
    assert outcome.abstained


def test_quoting_the_diarist_saying_i_dont_know_is_not_an_abstention():
    """The diarist writes «نمیدونم» constantly. Counting it as a refusal scored
    6.5% of answerable questions as abstentions on a pipeline with no gate."""
    assert not pipeline.reads_as_refusal('نمیدونم چیکار کنم [2026-01-05-a]',
                                         'extractive')
    assert not pipeline.reads_as_refusal(
        'کارت رو عوض کردی. خودت گفتی نمیدونم درست بود یا نه.', 'llm')
    assert pipeline.reads_as_refusal(pipeline.REFUSAL, 'extractive')
    assert pipeline.reads_as_refusal('چیزی در این مورد ذکر نشده.', 'llm')


def test_parent_expansion_adds_neighbouring_chunks(index):
    question = 'دعوا با مهسا سر کارهای خونه'
    plain = pipeline.retrieve(index, RetrievalConfig(parent_expansion='none'),
                              question, '2026-07-28')
    expanded = pipeline.retrieve(index, RetrievalConfig(parent_expansion='session'),
                                 question, '2026-07-28')
    assert len(expanded.contexts) > len(plain.contexts)
    assert any(c.expanded_from for c in expanded.contexts)


def test_rollup_layers_are_reachable_when_searched_alone(index):
    cfg = RetrievalConfig(search_layers=('thread',), k=5, time_filter=False)
    outcome = pipeline.retrieve(index, cfg, 'چند بار قول دادم برم باشگاه؟',
                                '2026-07-28')
    assert outcome.contexts
    assert {c.layer for c in outcome.contexts} == {'thread'}


def test_rollup_boost_promotes_summary_layers_into_the_context(index):
    """The boost has to act on the fused scores, not on the survivors of the
    candidate cut: leaf chunks outnumber rollups twenty to one, so a summary that
    did not already make the cut can only be promoted before it."""
    question = 'چند بار قول دادم برم باشگاه و نرفتم؟'
    plain = pipeline.retrieve(index, RetrievalConfig(rollup_boost=1.0, k=6),
                              question, '2026-07-28')
    boosted = pipeline.retrieve(index, RetrievalConfig(rollup_boost=3.0, k=6),
                                question, '2026-07-28')
    rollups = lambda o: sum(1 for c in o.contexts if c.layer != 'chunk')
    assert rollups(boosted) > rollups(plain)


def test_ascii_hash_baseline_retrieves_worse_than_char_hash(registry, ground_truth):
    """The lab's headline comparison, asserted: the production embedder cannot
    represent this corpus, so it must lose to a Unicode-aware one."""
    questions = [q for q in ground_truth['questions']
                 if q['type'] == 'single-hop'][:8]
    cfg = RetrievalConfig(retriever='dense', k=8, reranker='none', time_filter=False)

    def rate(embedder_name):
        index = registry.get(IndexConfig(chunker='fixed', embedder=embedder_name,
                                         contextual=False, layers=('chunk',)))
        total = 0.0
        for question in questions:
            outcome = pipeline.retrieve(index, cfg, question['question_fa'],
                                        question['query_date'])
            total += metrics.hit_at_k(outcome.sessions,
                                      corpus.evidence_sessions(question), cfg.k)
        return total / len(questions)

    assert rate('char-hash') > rate('ascii-hash')


# --- evaluation harness ----------------------------------------------------

def test_run_eval_scores_a_slice_end_to_end(registry, ground_truth, tmp_path,
                                            monkeypatch):
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='message', embedder='char-hash',
                                      contextual=True, layers=('chunk', 'session')),
                    retrieval=RetrievalConfig(search_layers=('chunk', 'session'),
                                              k=6, reranker='lexical'),
                    generation=GenerationConfig(answerer='extractive'),
                    label='test-slice')
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=12, ragas_mode='off')
    assert len(result.rows) == 12
    assert result.summary['overall']['headline'] is not None
    assert result.summary['by_type']
    assert (tmp_path / f'{result.run_id}.json').exists()
    assert all('answer' in row for row in result.rows)


def test_select_questions_strides_across_types(ground_truth):
    picked = evaluate.select_questions(ground_truth, limit=10)
    assert len(picked) == 10
    assert len({q['type'] for q in picked}) > 1, 'a limited run must stay diverse'


def test_run_eval_rejects_searching_an_unindexed_layer(registry, ground_truth):
    cfg = LabConfig(index=IndexConfig(layers=('chunk',)),
                    retrieval=RetrievalConfig(search_layers=('chunk', 'thread')))
    with pytest.raises(ValueError, match='never indexed'):
        evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS, limit=2,
                          ragas_mode='off')


def test_config_round_trips_through_the_panel_payload():
    cfg = LabConfig.from_dict({'index': {'chunker': 'session', 'unknown': 1},
                               'retrieval': {'k': 3},
                               'generation': {'answerer': 'none'},
                               'label': 'x'})
    assert cfg.index.chunker == 'session' and cfg.retrieval.k == 3
    assert cfg.validate() == []
    assert LabConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()


def test_lab_refuses_the_production_chroma_database():
    with pytest.raises(ValueError, match='production'):
        LabSettings(chroma_database='lodestar')


# --- RAGAS bridge ----------------------------------------------------------

def test_ragas_telemetry_is_disabled_on_import():
    """RAGAS's usage ping blocks for ~150 seconds per evaluate() call when its
    endpoint is unreachable — longer than the measurement itself by three orders
    of magnitude. Importing the bridge must be enough to prevent that."""
    from .raglab import ragas_eval  # noqa: F401
    assert os.environ.get('RAGAS_DO_NOT_TRACK') == 'true'


def test_ragas_availability_reports_missing_pieces_instead_of_raising():
    from .raglab import ragas_eval
    status = ragas_eval.availability(LAB_SETTINGS)
    assert isinstance(status.installed, bool)
    if status.installed:
        assert not status.llm_ready   # no key in LAB_SETTINGS
    assert 'ragas' in status.as_dict()['install_hint']


def test_evidence_texts_are_the_cited_messages_not_the_short_quotes(diary,
                                                                   ground_truth):
    """String-similarity metrics need comparable units, so RAGAS is given the
    whole cited message — which must still contain the quote."""
    sessions = corpus.sessions_by_id(diary)
    question = next(q for q in ground_truth['questions'] if q['answerable'])
    texts = corpus.evidence_texts(sessions, question)
    assert texts
    quote = question['evidence'][0]['quote']
    assert any(quote in text for text in texts)
    assert sum(map(len, texts)) > len(quote)


def test_evidence_texts_fall_back_to_quotes_for_unknown_sessions():
    question = {'evidence': [{'session_id': 'nope', 'message_indices': [0],
                              'quote': 'یه چیزی'}]}
    assert corpus.evidence_texts({}, question) == ['یه چیزی']


def test_json_safe_replaces_undefined_metrics_with_null():
    assert evaluate.json_safe({'a': float('nan'), 'b': [1.0, float('nan')]}) == \
        {'a': None, 'b': [1.0, None]}


def test_ragas_offline_metrics_score_a_retrieval(index, ground_truth):
    pytest.importorskip('ragas')
    pytest.importorskip('rapidfuzz')
    from .raglab import ragas_eval
    questions = [q for q in ground_truth['questions'] if q['answerable']][:3]
    pairs = [(q, pipeline.retrieve(index, RetrievalConfig(k=5), q['question_fa'],
                                   q['query_date'])) for q in questions]
    report = ragas_eval.run(pairs, LAB_SETTINGS, index.embedder, mode='offline')
    assert report['n_samples'] == 3, report['notes']
    assert 'non_llm_context_recall' in report['metrics']
    assert 0.0 <= report['metrics']['non_llm_context_recall'] <= 1.0


# --- picking a model per task ----------------------------------------------
# Seven stages of the lab can call a language model, and they want different
# things from one: a summariser is run 157 times and should be cheap, a judge
# should be the strongest thing available, and on a Farsi corpus the open-weight
# candidates are worth measuring rather than assuming. So each stage carries its
# own choice, and nothing is hard-coded.

class Recorder:
    """A provider that remembers which model each stage asked for.

    The reply is shaped like an llm_scores answer so the reranking and grading
    stages parse it and carry on; the answer stage just repeats it back."""

    def __init__(self, reply: str = '1: 9\n2: 9\n3: 9\n4: 9'):
        self.reply = reply
        self.calls: list[str] = []

    def invoke(self, messages, model='', **kwargs):
        # '' is the default because lab_chat omits the kwarg entirely for a
        # stage with no model choice — which is what "leave it to the provider"
        # has to look like on the wire.
        self.calls.append(model)
        return type('Turn', (), {'content': self.reply, 'tool_calls': []})()


def test_every_llm_stage_has_a_role_in_the_registry():
    assert {role.key for role in models.ROLES} == {
        'summarize', 'expand', 'rerank', 'grade', 'answer', 'judge', 'ragas'}


def test_every_model_role_points_at_a_real_config_field():
    cfg = LabConfig()
    for role in models.ROLES:
        group, _, field = role.field.partition('.')
        assert field in getattr(cfg, group).__dataclass_fields__, role.key


def test_every_model_in_the_catalogue_declares_where_its_weights_stand():
    entries = models.catalogue(LAB_SETTINGS)
    assert entries[0]['id'] == ''          # the lab default stays the first choice
    assert all(e['source'] in ('default', 'open', 'closed') for e in entries)
    assert any(e['source'] == 'open' for e in entries)
    assert any(e['source'] == 'closed' for e in entries)
    assert all(e['label'] for e in entries)


def test_an_unverified_model_is_offered_as_unavailable_rather_than_dropped():
    """A model this lab has not actually run is still worth trying, so it stays
    in the list marked NA. Silently omitting it would hide the option."""
    entries = models.catalogue(LAB_SETTINGS)     # no API key: nothing to probe
    assert any(not e['available'] for e in entries)
    by_id = {e['id']: e for e in entries}
    assert by_id[LAB_SETTINGS.llm_model]['available']


def test_the_configured_model_is_always_offered_even_if_it_is_not_in_the_registry():
    settings = replace(LAB_SETTINGS, llm_model='someone/custom-7b')
    entries = models.catalogue(settings)
    assert 'someone/custom-7b' in [e['id'] for e in entries]
    assert entries[0]['label'].endswith('someone/custom-7b)')


def test_a_blank_role_falls_back_to_the_lab_default_model():
    settings = replace(LAB_SETTINGS, llm_model='lab/default')
    roles = models.resolve(LabConfig(), settings)
    assert roles.answer == 'lab/default' and roles.summarize == 'lab/default'
    assert roles.ragas == 'lab/default' and roles.judge == 'lab/default'


def test_each_role_round_trips_from_the_panels_json():
    cfg = LabConfig.from_dict({
        'index': {'summarizer': 'llm', 'summarizer_model': 'sum/model'},
        'retrieval': {'reranker_model': 'rerank/model', 'grader_model': 'grade/model',
                      'expansion_model': 'hyde/model'},
        'generation': {'model': 'answer/model', 'judge_model': 'judge/model',
                       'ragas_model': 'ragas/model'}})
    roles = models.resolve(cfg, LAB_SETTINGS)
    assert (roles.summarize, roles.rerank, roles.grade, roles.expand, roles.answer,
            roles.judge, roles.ragas) == (
        'sum/model', 'rerank/model', 'grade/model', 'hyde/model', 'answer/model',
        'judge/model', 'ragas/model')


def test_each_stage_calls_the_model_chosen_for_its_own_role(index):
    """The point of per-task models: a cheap reranker and an expensive answerer
    in the same run. One model for everything makes that impossible to measure."""
    cfg = LabConfig(
        retrieval=RetrievalConfig(k=3, rerank_depth=3, reranker='llm',
                                  reranker_model='rerank/model', grader='llm',
                                  grader_model='grade/model', hyde=True,
                                  expansion_model='hyde/model'),
        generation=GenerationConfig(answerer='llm', model='answer/model'))
    roles = models.resolve(cfg, LAB_SETTINGS)
    provider = Recorder()
    outcome = pipeline.retrieve(index, cfg.retrieval, 'قرار بود چی کار کنم؟',
                                '2026-07-28', llm=provider, models=roles)
    pipeline.answer(outcome, cfg.generation, llm=provider, models=roles)
    assert provider.calls == ['hyde/model', 'rerank/model', 'grade/model',
                             'answer/model']


def test_a_stage_with_no_model_choice_leaves_it_to_the_provider(index):
    """'' rather than a guess: the provider already knows its default model, and
    a lab that hard-codes one here would silently ignore RAGLAB_MODEL."""
    provider = Recorder()
    pipeline.retrieve(index, RetrievalConfig(k=3, rerank_depth=3, reranker='llm'),
                      'قول باشگاه', '2026-07-28', llm=provider)
    assert provider.calls == ['']


def test_the_key_facts_judge_uses_the_judge_model():
    provider = Recorder(reply='1: yes')
    score = evaluate.judge_key_facts(provider, 'judge/model',
                                     {'key_facts': ['he went to the gym']}, 'رفتم')
    assert provider.calls == ['judge/model']
    assert score == pytest.approx(1.0)


def test_the_summary_model_names_the_collection_only_when_llm_summaries_are_on():
    """Switching a model nobody is calling must not invalidate an index — the
    fingerprint has to describe what was actually written, or every unrelated
    change costs a rebuild of 157 sessions."""
    extractive = IndexConfig(summarizer='extractive')
    assert extractive.fingerprint() == \
        replace(extractive, summarizer_model='sum/model').fingerprint()
    llm = IndexConfig(summarizer='llm')
    assert llm.fingerprint() != replace(llm, summarizer_model='sum/model').fingerprint()


def test_two_summary_models_do_not_share_cached_summaries(session, tmp_path):
    """The cache is what makes the hierarchy affordable, and it is keyed by
    summariser. With a per-task model that key has to include the model, or
    comparing two summarisers silently compares one of them twice."""
    fallback = summarize.ExtractiveSummarizer({})
    first = summarize.LLMSummarizer(FakeChat(), 'a/model', fallback)
    second = summarize.LLMSummarizer(FakeChat(), 'b/model', fallback)
    assert first.name != second.name
    cache = summarize.SummaryCache(path=tmp_path / 'summaries.json')
    summarize.session_summaries([session], first, cache)
    assert cache.get(first.name, session) is not None
    assert cache.get(second.name, session) is None


def test_the_index_summarises_with_the_chosen_model(monkeypatch, tmp_path, diary):
    monkeypatch.setattr(summarize, 'RUNS_DIR', tmp_path)
    seen: list[str] = []
    real = summarize.LLMSummarizer

    def spy(llm, model, fallback):
        seen.append(model)
        return real(llm, model, fallback)

    monkeypatch.setattr(summarize, 'LLMSummarizer', spy)
    cfg = IndexConfig(embedder='ascii-hash', layers=('session',), summarizer='llm',
                      summarizer_model='sum/model')
    LabIndex.build(cfg, {'sessions': diary['sessions'][:2], 'threads': {}},
                   LAB_SETTINGS)
    assert seen == ['sum/model']


def test_every_configuration_factor_has_an_explainer():
    """An unexplained knob is a knob nobody can make a real decision about."""
    assert explain.missing() == []


def test_the_explainers_cover_the_model_roles_too():
    topics = explain.topics()
    for role in models.ROLES:
        assert topics[f'model.{role.key}'], role.key
    assert 'model.answer' in topics and len(topics['model.answer']) > 20


# --- picking an embedder that can read the corpus --------------------------
# The embedder decides everything downstream, and on a Farsi diary most of the
# well-known ones cannot represent the text at all: the brain's default tokenises
# [a-z0-9]+, and the fastembed default the brain hardwires (bge-small-**en**) is
# an English model. A dropdown that does not say which languages an option covers
# is how a run ends up measuring nothing — so language coverage is part of every
# entry, and the Farsi-capable models are offered by name.

FARSI_MODELS = ('intfloat/multilingual-e5-large',
                'sentence-transformers/paraphrase-multilingual-mpnet-base-v2',
                'BAAI/bge-m3')


class FakeTextEmbedding:
    """Stands in for fastembed's TextEmbedding: records exactly what it was
    asked to encode, so the E5 prefixes can be asserted with no model download."""

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.seen: list[str] = []

    def embed(self, texts, batch_size=None):
        for text in list(texts):
            self.seen.append(text)
            vector = np.zeros(self.dim, dtype=np.float32)
            vector[len(text) % self.dim] = 1.0
            yield vector


def test_every_embedder_says_which_languages_it_covers():
    """A hint per option, covering the whole registry: an embedder the panel
    offers without saying what it can read is a run nobody can interpret."""
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert set(hints) == set(EMBEDDERS)
    assert all(h['languages'] and h['label'] and h['note'] for h in hints.values())


def test_the_production_default_is_labelled_as_latin_only():
    """ascii-hash scores 0.014 on this corpus for one reason, and the dropdown
    has to say it out loud rather than leave it to be discovered by a run."""
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert hints['ascii-hash']['farsi'] is False
    assert 'latin' in hints['ascii-hash']['languages'].lower()
    for kind in ('token-hash', 'char-hash', 'fastembed'):
        assert hints[kind]['farsi'] is True, kind


def test_the_embedding_model_catalogue_offers_models_that_speak_farsi():
    entries = embedding.embed_model_catalogue(LAB_SETTINGS)
    assert entries[0]['id'] == ''            # the lab default stays first
    by_id = {entry['id']: entry for entry in entries}
    for model in FARSI_MODELS:
        assert by_id[model]['farsi'] is True, model
        assert 'farsi' in by_id[model]['languages'].lower(), model
    assert LAB_SETTINGS.fastembed_model in by_id
    assert all(e['languages'] and e['label'] and e['note'] for e in entries)
    assert all(e['source'] in ('default', 'open', 'closed', 'unknown')
               for e in entries)


def _fastembed_serving(monkeypatch, ids):
    """Pretend fastembed is installed and serves exactly `ids`.

    Both halves have to be stubbed. Availability is an import check, the served
    list is a separate lookup, and the catalogue honours both — so patching only
    the list leaves these tests asserting on whether the `semantic` extra
    happens to be installed here. The brain suite is offline by contract."""
    monkeypatch.setattr(embedding, 'fastembed_available', lambda: True)
    monkeypatch.setattr(embedding, 'fastembed_models', lambda: frozenset(ids))


def test_an_english_only_model_is_offered_but_says_so(monkeypatch):
    """The brain hardwires bge-small-en today. The lab must be able to measure
    that choice, and must never let it be picked by accident."""
    _fastembed_serving(monkeypatch, embedding.MODEL_IDS)
    by_id = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    english = by_id['BAAI/bge-small-en-v1.5']
    assert english['farsi'] is False
    assert 'english' in english['languages'].lower()
    assert english['available'] is True      # installable, just wrong for Farsi


def test_a_model_fastembed_cannot_serve_stays_in_the_list_as_unavailable(monkeypatch):
    """Same rule as the chat models: NA says "worth trying, nobody measured it
    here", while dropping it hides the option altogether."""
    _fastembed_serving(monkeypatch, {'intfloat/multilingual-e5-large'})
    entries = embedding.embed_model_catalogue(LAB_SETTINGS)
    by_id = {entry['id']: entry for entry in entries}
    assert by_id['intfloat/multilingual-e5-large']['available'] is True
    assert by_id['BAAI/bge-m3']['available'] is False
    flags = [entry['available'] for entry in entries]
    assert flags == sorted(flags, reverse=True), 'usable models come first'


def test_fastembed_models_are_NA_until_the_semantic_extra_is_installed(monkeypatch):
    """The mirror of the sentence-transformers case, and the reason the catalogue
    checks the import on top of the served list: with the extra missing, every
    fastembed model must read NA rather than promise a wheel that is not there.
    The served list is left generous on purpose — the import check alone decides."""
    monkeypatch.setattr(embedding, 'fastembed_models',
                        lambda: frozenset(embedding.MODEL_IDS))
    monkeypatch.setattr(embedding, 'fastembed_available', lambda: False)
    absent = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert absent['BAAI/bge-m3']['available'] is False
    assert absent['BAAI/bge-small-en-v1.5']['available'] is False
    monkeypatch.setattr(embedding, 'fastembed_available', lambda: True)
    present = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert present['BAAI/bge-m3']['available'] is True


def test_e5_models_carry_the_prefixes_they_were_trained_with():
    """E5 was trained with "query: " / "passage: ". Dropping the prefixes is a
    silent quality loss, so they belong to the model entry, not to a caller."""
    by_id = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    e5 = by_id['intfloat/multilingual-e5-large']
    assert (e5['query_prefix'], e5['passage_prefix']) == ('query: ', 'passage: ')
    mpnet = by_id['sentence-transformers/paraphrase-multilingual-mpnet-base-v2']
    assert (mpnet['query_prefix'], mpnet['passage_prefix']) == ('', '')


def test_a_prefixed_embedder_marks_queries_and_passages_apart():
    fake = FakeTextEmbedding()
    embedder = embedding.FastEmbedMultilingual(
        'intfloat/multilingual-e5-large', query_prefix='query: ',
        passage_prefix='passage: ', factory=lambda name: fake)
    embedder.embed(['دعوا با مهسا سر خونه'])
    embedder.embed_queries(['دعوا با مهسا'])
    assert 'passage: دعوا با مهسا سر خونه' in fake.seen
    assert 'query: دعوا با مهسا' in fake.seen


def test_a_query_is_embedded_as_a_query_when_the_model_distinguishes_them():
    class Asymmetric:
        dim = 2
        name = 'asymmetric'

        def __init__(self):
            self.as_query: list[str] = []

        def embed(self, texts):
            return np.zeros((len(list(texts)), 2), dtype=np.float32)

        def embed_queries(self, texts):
            self.as_query.extend(texts)
            return np.ones((len(list(texts)), 2), dtype=np.float32)

    embedder = Asymmetric()
    vectors = embedding.query_vectors(embedder, ['سلام'])
    assert embedder.as_query == ['سلام']
    assert vectors.shape == (1, 2) and vectors.any()


def test_a_symmetric_embedder_needs_no_query_method():
    """Every hash embedder embeds both sides the same way, and must keep
    working without knowing this distinction exists."""
    vectors = embedding.query_vectors(embedding.make_embedder('char-hash'),
                                      ['سلام'])
    assert vectors.shape[0] == 1 and np.any(vectors)


def test_dense_retrieval_embeds_the_question_as_a_query(index, monkeypatch):
    """The prefix is worthless if retrieval bypasses it, so the pipeline must go
    through the query seam rather than calling embed() itself."""
    real = embedding.query_vectors
    seen: list[list[str]] = []

    def spy(embedder, texts):
        seen.append(list(texts))
        return real(embedder, texts)

    monkeypatch.setattr(pipeline.embedding, 'query_vectors', spy)
    pipeline.retrieve(index, RetrievalConfig(retriever='dense', k=3,
                                            multi_query=False, time_filter=False),
                      'قول باشگاه', '2026-07-28')
    assert seen and seen[0] == ['قول باشگاه']


def test_the_embedding_model_names_the_collection_only_when_it_is_used():
    """Same rule as the summary model: a model nobody loads must not invalidate
    an index and cost a 157-session rebuild."""
    hashed = IndexConfig(embedder='char-hash')
    assert hashed.fingerprint() == \
        replace(hashed, embed_model='BAAI/bge-m3').fingerprint()
    real = IndexConfig(embedder='fastembed')
    assert real.fingerprint() != \
        replace(real, embed_model='BAAI/bge-m3').fingerprint()


def test_the_chosen_embedding_model_is_the_one_that_gets_loaded(monkeypatch):
    seen: dict = {}

    def spy(model_name, **kwargs):
        seen.update({'model': model_name} | kwargs)
        return object()

    monkeypatch.setattr(embedding, 'FastEmbedMultilingual', spy)
    embedding.make_embedder('fastembed', LAB_SETTINGS,
                            model='intfloat/multilingual-e5-large')
    assert seen['model'] == 'intfloat/multilingual-e5-large'
    assert seen['query_prefix'] == 'query: '
    assert seen['passage_prefix'] == 'passage: '


def test_a_blank_embedding_model_keeps_following_the_lab_default(monkeypatch):
    """'' means RAGLAB_FASTEMBED_MODEL, exactly as '' means RAGLAB_MODEL for the
    chat roles — the lab never hard-codes a model of its own."""
    seen: dict = {}
    monkeypatch.setattr(embedding, 'FastEmbedMultilingual',
                        lambda model_name, **kw: seen.update(model=model_name))
    embedding.make_embedder('fastembed', LAB_SETTINGS)
    assert seen['model'] == LAB_SETTINGS.fastembed_model


def test_the_index_builds_with_the_embedding_model_from_its_config(monkeypatch,
                                                                  diary):
    from .raglab import index as index_module
    seen: list[tuple] = []

    def spy(kind, settings=None, model=''):
        seen.append((kind, model))
        return embedding.CharHashEmbedder()   # anything that embeds, offline

    monkeypatch.setattr(index_module.embedding, 'make_embedder', spy)
    cfg = IndexConfig(chunker='session', embedder='fastembed',
                      embed_model='BAAI/bge-m3', layers=('session',))
    LabIndex.build(cfg, {'sessions': diary['sessions'][:2], 'threads': {}},
                   LAB_SETTINGS)
    assert seen == [('fastembed', 'BAAI/bge-m3')]


def test_the_language_note_names_the_model_that_was_actually_used():
    note = embedding.language_note('fastembed', 'BAAI/bge-small-en-v1.5')
    assert 'bge-small-en' in note and 'english' in note.lower()
    assert 'ascii-hash' in embedding.language_note('ascii-hash', '')


def test_a_run_records_which_languages_its_embedder_can_represent(
        registry, ground_truth, tmp_path, monkeypatch):
    """A leaderboard row whose embedder could not read the corpus is not a
    result, and three days later nothing on the row says so."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='fixed', embedder='ascii-hash',
                                      contextual=False, layers=('chunk',)),
                    retrieval=RetrievalConfig(search_layers=('chunk',), k=4),
                    generation=GenerationConfig(answerer='none'))
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=2, ragas_mode='off')
    notes = ' '.join(result.notes).lower()
    assert 'ascii-hash' in notes and 'latin' in notes


def test_the_embedding_model_knob_explains_itself():
    topics = explain.topics()
    assert 'farsi' in topics['index.embed_model'].lower()
    assert 'farsi' in topics['index.embedder'].lower()


# --- models fastembed cannot serve -----------------------------------------
#
# The strongest candidates for Persian are not in fastembed's list: Qwen3 and
# heydariAI/persian-embeddings are HuggingFace checkpoints, and OpenAI's are an
# API call. Listing them as permanently NA would be honest and useless, so each
# model names the backend that serves it and the lab grows two more of them.
# Everything here runs offline: the local backend is exercised through an injected
# factory and the API backend through an injected transport, because a test that
# needs a 16 GB download is a test nobody runs.

REQUESTED_MODELS = {
    'Qwen/Qwen3-Embedding-8B': ('sentence-transformers', 4096, 'open'),
    'heydariAI/persian-embeddings': ('sentence-transformers', 1024, 'open'),
    'openai/text-embedding-3-small': ('openai', 1536, 'closed'),
    'openai/text-embedding-3-large': ('openai', 3072, 'closed'),
}


class FakeSentenceTransformer:
    """Stands in for sentence_transformers.SentenceTransformer, recording every
    text it was asked to encode so the prefix behaviour can be asserted."""

    def __init__(self, name: str, dim: int = 8):
        self.name = name
        self.dim = dim
        self.seen: list[str] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts, **kwargs):
        self.seen.extend(texts)
        return np.ones((len(list(texts)), self.dim), dtype=np.float32)


def test_the_catalogue_offers_every_requested_model_with_its_backend():
    by_id = {model.id: model for model in embedding.EMBED_MODELS}
    for model_id, (backend, dim, source) in REQUESTED_MODELS.items():
        entry = by_id.get(model_id)
        assert entry is not None, model_id
        assert (entry.backend, entry.dim, entry.source) == (backend, dim, source)
        assert entry.farsi and entry.note, model_id


def test_every_model_names_a_backend_the_lab_actually_has():
    assert all(model.backend in embedding.BACKENDS
               for model in embedding.EMBED_MODELS)
    assert set(embedding.BACKENDS) <= set(EMBEDDERS)


def test_the_persian_tuned_model_is_the_default_and_qwen3_the_ceiling():
    """The lab defaults to a Persian-tuned encoder — a Farsi corpus deserves one,
    and at ~2.2 GB it is the cheapest real encoder here. Qwen3 stays the
    recommended ceiling rather than the default: 16 GB is not a default."""
    assert IndexConfig().embedder == 'sentence-transformers'
    assert IndexConfig().embed_model == ''      # '' = the backend's default
    assert embedding.BACKEND_DEFAULTS['sentence-transformers'] == \
        'heydariAI/persian-embeddings'
    assert embedding.resolve_model('sentence-transformers', LAB_SETTINGS, '') == \
        'heydariAI/persian-embeddings'
    by_id = {m.id: m for m in embedding.EMBED_MODELS}
    qwen = by_id['Qwen/Qwen3-Embedding-8B']
    assert 'recommend' in qwen.note.lower()
    # Visible in the option itself, not only behind the explainer: the standing is
    # what you are looking for while the dropdown is open.
    assert qwen.tag == 'recommended'
    assert by_id['heydariAI/persian-embeddings'].tag == 'lab default'
    # RAGLAB_FASTEMBED_MODEL still drives the fastembed backend, untouched.
    assert embedding.resolve_model('fastembed', LAB_SETTINGS, '') == \
        LabSettings().fastembed_model


def test_the_persian_tuned_model_says_which_language_it_was_tuned_for():
    entry = {m.id: m for m in embedding.EMBED_MODELS}['heydariAI/persian-embeddings']
    assert 'persian' in entry.languages.lower() or 'farsi' in entry.languages.lower()


def test_both_new_backends_are_offered_as_embedders_with_their_coverage():
    assert {'sentence-transformers', 'openai'} <= set(EMBEDDERS)
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert set(hints) == set(EMBEDDERS)
    for kind in ('sentence-transformers', 'openai'):
        assert hints[kind]['farsi'] is True
        assert hints[kind]['languages'] and hints[kind]['note']


def test_a_local_model_is_offered_as_NA_until_its_library_is_installed(monkeypatch):
    monkeypatch.setattr(embedding, 'sentence_transformers_available', lambda: False)
    absent = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert absent['Qwen/Qwen3-Embedding-8B']['available'] is False
    assert absent['heydariAI/persian-embeddings']['available'] is False
    monkeypatch.setattr(embedding, 'sentence_transformers_available', lambda: True)
    present = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert present['Qwen/Qwen3-Embedding-8B']['available'] is True


def test_an_api_model_is_offered_as_NA_until_there_is_a_key():
    """Availability stays verified rather than guessed: without a key the call
    would fail at the first chunk of a 40-minute sweep."""
    without = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert without['openai/text-embedding-3-small']['available'] is False
    keyed = replace(LAB_SETTINGS, openai_api_key='sk-test')
    with_key = {e['id']: e for e in embedding.embed_model_catalogue(keyed)}
    assert with_key['openai/text-embedding-3-small']['available'] is True
    assert with_key['openai/text-embedding-3-large']['available'] is True


def test_the_lab_reads_an_openai_key_of_its_own():
    """Separate from OPENROUTER_API_KEY on purpose: OpenRouter serves no
    embeddings endpoint, so the chat key cannot stand in for this one."""
    settings = config.load_lab_settings({'OPENAI_API_KEY': 'sk-lab',
                                         'OPENAI_BASE_URL': 'http://proxy/v1'})
    assert settings.openai_api_key == 'sk-lab'
    assert settings.openai_base_url == 'http://proxy/v1'
    assert LabSettings().openai_api_key == ''
    assert LabSettings().openai_base_url.endswith('/v1')


def test_the_local_embedder_asks_qwen3_the_way_qwen3_expects():
    """Qwen3 is instruction-tuned: the query side carries an instruction and the
    document side does not. Getting that backwards is a silent accuracy loss of
    exactly the kind the E5 prefixes taught us to test for."""
    fake = FakeSentenceTransformer('Qwen/Qwen3-Embedding-8B')
    embedder = embedding.SentenceTransformerEmbedder(
        'Qwen/Qwen3-Embedding-8B', query_prefix='Instruct: find it\nQuery: ',
        factory=lambda name: fake)
    fake.seen.clear()                      # drop anything the probe encoded
    passages = embedder.embed(['امروز جلسه داشتم'])
    queries = embedder.embed_queries(['جلسه کی بود؟'])
    assert fake.seen == ['امروز جلسه داشتم',
                         'Instruct: find it\nQuery: جلسه کی بود؟']
    assert embedder.dim == fake.dim == passages.shape[1] == queries.shape[1]
    assert 'Qwen/Qwen3-Embedding-8B' in embedder.name


def test_the_api_embedder_sends_the_model_and_normalises_what_comes_back():
    calls = []

    def post(url, payload, headers):
        calls.append((url, payload, headers))
        return {'data': [{'embedding': [3.0, 4.0]} for _ in payload['input']]}

    keyed = replace(LAB_SETTINGS, openai_api_key='sk-test')
    embedder = embedding.OpenAIEmbedder('openai/text-embedding-3-small', keyed,
                                        post=post)
    vectors = embedder.embed(['یک', 'دو'])
    assert vectors.shape == (2, 2)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    url, payload, headers = calls[-1]
    assert url.endswith('/embeddings')
    # The panel shows an OpenRouter-shaped slug; OpenAI's own API wants the bare
    # model name, so the prefix is stripped on the wire.
    assert payload['model'] == 'text-embedding-3-small'
    assert 'sk-test' in headers['Authorization']
    # Declared, not probed: a network call in the constructor would make building
    # an index config cost money.
    assert embedder.dim == 1536


def test_the_api_embedder_batches_so_a_whole_corpus_fits():
    calls = []

    def post(url, payload, headers):
        calls.append(payload['input'])
        return {'data': [{'embedding': [1.0, 0.0]} for _ in payload['input']]}

    keyed = replace(LAB_SETTINGS, openai_api_key='sk-test')
    embedder = embedding.OpenAIEmbedder('openai/text-embedding-3-small', keyed,
                                        batch_size=2, post=post)
    embedder.embed(['a', 'b', 'c'])
    assert [len(batch) for batch in calls] == [2, 1]


def test_the_api_embedder_says_what_is_missing_instead_of_failing_mid_sweep():
    with pytest.raises(ValueError) as raised:
        embedding.OpenAIEmbedder('openai/text-embedding-3-small', LAB_SETTINGS)
    assert 'OPENAI_API_KEY' in str(raised.value)


def test_make_embedder_builds_both_new_backends(monkeypatch):
    monkeypatch.setattr(embedding, '_sentence_transformer',
                        lambda name: FakeSentenceTransformer(name))
    local = embedding.make_embedder('sentence-transformers', LAB_SETTINGS,
                                    'Qwen/Qwen3-Embedding-8B')
    assert 'Qwen/Qwen3-Embedding-8B' in local.name
    # Blank means "the default model for the backend you chose", the same rule as
    # '' meaning RAGLAB_FASTEMBED_MODEL for fastembed.
    default = embedding.make_embedder('sentence-transformers', LAB_SETTINGS, '')
    assert 'heydariAI/persian-embeddings' in default.name
    keyed = replace(LAB_SETTINGS, openai_api_key='sk-test')
    api = embedding.make_embedder('openai', keyed, 'openai/text-embedding-3-large')
    assert 'text-embedding-3-large' in api.name and api.dim == 3072


def test_the_chosen_model_survives_the_fingerprint_for_every_model_backend():
    """The model is part of what got stored, so it has to reach the collection
    name — for all three backends, not just the first one the lab had."""
    for kind in ('fastembed', 'sentence-transformers', 'openai'):
        kept = IndexConfig(embedder=kind, embed_model='some/model').normalized()
        assert kept.embed_model == 'some/model', kind
    dropped = IndexConfig(embedder='char-hash', embed_model='some/model').normalized()
    assert dropped.embed_model == ''
    a = IndexConfig(embedder='openai', embed_model='openai/text-embedding-3-small')
    b = IndexConfig(embedder='openai', embed_model='openai/text-embedding-3-large')
    assert a.fingerprint() != b.fingerprint()


def test_a_model_from_the_wrong_backend_is_refused_before_the_run():
    """Picking Qwen3 while the embedder is fastembed used to mean "load the
    default instead" — a run labelled Qwen3 that measured something else."""
    problems = LabConfig(index=IndexConfig(
        embedder='fastembed',
        embed_model='Qwen/Qwen3-Embedding-8B')).validate()
    assert any('sentence-transformers' in problem for problem in problems)
    assert LabConfig(index=IndexConfig(
        embedder='sentence-transformers',
        embed_model='Qwen/Qwen3-Embedding-8B')).validate() == []


def test_the_embedder_explainer_says_how_to_reach_a_model_it_cannot_download():
    """Three backends is a choice nobody can make from the kind names alone."""
    text = explain.topics()['index.embedder'].lower()
    assert 'sentence-transformers' in text and 'openai' in text


# --- what each number on the dashboard actually means -----------------------
#
# Every score in the panel is a claim about quality, and a claim nobody can check
# is worse than no claim: "faithfulness 0.74" means nothing without knowing whose
# definition, which formula, and which library produced it. So each metric carries
# the same four facts, from the same registry, shown through the same `!` the knobs
# use — and a metric a run can report without an explainer fails a test.

def test_every_reported_metric_has_a_definition():
    """The gate: `aggregate()` can report these keys, so the panel can show them,
    so every one of them has to be explainable."""
    defined = {measure.key for measure in metrics.MEASURES}
    reported = set(metrics.AGGREGATED) | {'headline'}
    assert reported <= defined, reported - defined
    for measure in metrics.MEASURES:
        assert measure.label and measure.short, measure.key
        assert measure.formula and measure.library and measure.help, measure.key


def test_a_metric_states_the_exact_formula_it_computes():
    """Not prose about the idea — the arithmetic, matching the code above it."""
    by_key = {measure.key: measure for measure in metrics.MEASURES}
    assert '|gold ∩ top-k| / |gold|' in by_key['recall'].formula
    assert '1 / rank' in by_key['mrr'].formula
    assert 'log2' in by_key['ndcg'].formula
    # The headline is a weighted sum invented here, so its weights are the formula.
    headline = by_key['headline'].formula
    for weight in ('0.4', '0.3', '0.2', '0.1'):
        assert weight in headline, weight
    assert '0.9' in by_key['quote_recall'].formula      # the fuzzy fallback


def test_every_metric_names_the_library_that_computes_it():
    by_key = {measure.key: measure for measure in metrics.MEASURES}
    assert 'metrics.recall_at_k' in by_key['recall'].library
    assert 'difflib' in by_key['quote_recall'].library
    assert 'difflib' in by_key['answer_similarity'].library
    # A deterministic metric must not claim to be a model, and vice versa.
    assert 'llm' not in by_key['recall'].library.lower()
    assert 'llm' in by_key['key_fact_coverage'].library.lower()


def test_every_metric_says_which_step_it_grades():
    """Same three inks as the panels: a number about retrieval is green wherever
    it appears, so the dashboard means one thing by a colour."""
    steps = {step.key for step in config.STEPS} | {''}
    assert all(measure.step in steps for measure in metrics.MEASURES)
    by_key = {measure.key: measure for measure in metrics.MEASURES}
    assert by_key['recall'].step == 'retrieval'
    assert by_key['ndcg'].step == 'retrieval'
    assert by_key['answer_similarity'].step == 'generation'
    assert by_key['latency_ms'].step == ''      # whole pipeline, no single step


def test_the_ragas_definitions_cover_every_metric_ragas_can_report():
    from .raglab import ragas_eval
    defined = {measure.key for measure in ragas_eval.RAGAS_MEASURES}
    reported = set(ragas_eval.OFFLINE_METRICS) | set(ragas_eval.LLM_METRICS)
    assert reported <= defined, reported - defined


def test_a_ragas_metric_carries_ragas_own_class_definition_and_formula():
    """"Faithfulness" is RAGAS's word, not ours, so the panel says whose
    definition it is showing and which class computed it."""
    from .raglab import ragas_eval
    by_key = {m.key: m for m in ragas_eval.RAGAS_MEASURES}
    faith = by_key['faithfulness']
    assert 'Faithfulness' in faith.library and 'ragas' in faith.library.lower()
    assert 'claims' in faith.help.lower()
    assert 'supported claims' in faith.formula and '/' in faith.formula
    relevancy = by_key['answer_relevancy']
    assert 'ResponseRelevancy' in relevancy.library
    assert 'cosine' in relevancy.formula.lower()
    f1 = by_key['factual_correctness(mode=f1)']
    assert 'FactualCorrectness' in f1.library and 'F1' in f1.formula
    offline = by_key['non_llm_context_recall']
    assert 'NonLLMContextRecall' in offline.library
    # The offline pair is string distance, not a model — and says so.
    assert 'rapidfuzz' in offline.library and 'llm' not in offline.formula.lower()


def test_a_judged_metric_says_which_model_judged_it():
    """A number produced by a model is a number with variance, and the reader has
    to know which model — the same reason every stage carries its own dropdown."""
    from .raglab import ragas_eval
    for measure in ragas_eval.RAGAS_MEASURES:
        if measure.key in ragas_eval.LLM_METRICS:
            assert 'RAGAS judge' in measure.library, measure.key
        else:
            assert 'no model' in measure.library.lower(), measure.key


def test_no_metric_ships_without_an_explainer():
    """The counterpart of explain.missing() for the knobs: a metric added to
    AGGREGATED or to the RAGAS list without a definition fails here."""
    assert explain.missing_metrics() == []


def test_metric_definitions_join_the_one_help_registry():
    """Homogeneous by construction: the panel has one explainer mechanism, so a
    metric's text lives with the knobs' text under 'metric.<key>'."""
    topics = explain.topics()
    for key in ('metric.recall', 'metric.quote_recall', 'metric.headline',
                'metric.faithfulness', 'metric.non_llm_context_recall'):
        assert topics.get(key), key


# --- the three pipeline steps ----------------------------------------------
#
# The panel groups and colours every control by the step it belongs to — index,
# retrieval, generation — so the step list is a registry the lab owns, not a
# palette the frontend invents. The colours themselves stay in CSS; what has to
# be single-sourced here is which step each knob and each model serves, because a
# dropdown coloured for the wrong step is worse than an uncoloured one.

def test_the_pipeline_steps_are_named_once_in_pipeline_order():
    assert [step.key for step in config.STEPS] == ['index', 'retrieval',
                                                   'generation']
    # Two names on purpose: the long one titles a panel, the short one tags a
    # group of models inside another panel, where a whole sentence would not fit.
    assert all(step.label and step.short and step.note for step in config.STEPS)
    assert [step.short for step in config.STEPS] == ['Index', 'Retrieval',
                                                     'Generation']


def test_the_steps_are_exactly_the_config_groups():
    """A step is a config group with a colour, so the two lists cannot drift:
    a fourth group would otherwise render in a panel nobody colours."""
    assert {step.key for step in config.STEPS} == {group for group, _
                                                   in explain.GROUPS}


def test_every_model_role_says_which_step_it_serves():
    steps = {step.key for step in config.STEPS}
    assert all(role.step in steps for role in models.ROLES)
    # The colour cannot disagree with where the value is stored: the step is the
    # group of the field the dropdown writes to.
    assert all(role.step == role.field.split('.')[0] for role in models.ROLES)


def test_every_step_owns_at_least_one_model():
    """Each colour has to mean something in the models panel — a step with no
    model in it is a legend entry pointing at nothing."""
    served = {role.step for role in models.ROLES}
    assert served == {step.key for step in config.STEPS}


def test_a_model_role_is_serialised_with_its_step():
    role = next(r for r in models.ROLES if r.key == 'grade')
    assert role.as_dict()['step'] == 'retrieval'


# --- the service -----------------------------------------------------------

@pytest.fixture(scope='module')
def client():
    os.environ['BRAIN_CHROMA_URL'] = 'memory'
    os.environ['RAGLAB_CHROMA_DATABASE'] = 'raglab-tests'
    from fastapi.testclient import TestClient

    from .raglab.server import create_app
    return TestClient(create_app())


def test_options_describes_the_corpus_and_capabilities(client):
    body = client.get('/api/options').json()
    assert body['corpus']['sessions'] == 157
    assert body['corpus']['questions'] == 100
    assert 'semantic-drift' in body['chunkers']
    assert body['capabilities']['chroma_database'] == 'raglab-tests'
    assert 'ragas' in body['capabilities']


def test_panel_is_served(client):
    page = client.get('/')
    assert page.status_code == 200
    assert 'RAG Lab' in page.text


def test_ad_hoc_query_returns_stages_and_contexts(client):
    body = client.post('/api/query', json={
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash',
                  'layers': ['chunk']},
        'retrieval': {'search_layers': ['chunk'], 'k': 4},
        'generation': {'answerer': 'extractive'}}).json()
    assert body['contexts'] and body['answer']
    assert body['time_scope']['label'] == 'آذر'
    assert 'retrieve_ms' in body['timings']


def test_query_rejects_an_unknown_strategy(client):
    res = client.post('/api/query', json={'question': 'x',
                                          'index': {'chunker': 'nope'}})
    assert res.status_code == 400
    assert 'unknown chunker' in res.json()['detail']


def test_questions_endpoint_hides_the_answers(client):
    body = client.get('/api/questions?limit=5').json()
    assert len(body['questions']) == 5
    assert 'answer_fa' not in body['questions'][0]
    assert body['questions'][0]['evidence_sessions']


def test_options_offers_a_model_choice_for_every_llm_task(client):
    body = client.get('/api/options').json()
    roles = {role['key']: role for role in body['model_roles']}
    assert set(roles) == {'summarize', 'expand', 'rerank', 'grade', 'answer',
                          'judge', 'ragas'}
    assert all(role['help'] and role['label'] and role['field']
               for role in roles.values())
    ids = [m['id'] for m in body['models']]
    assert ids[0] == '' and 'openai/gpt-5-nano' in ids
    assert {m['source'] for m in body['models']} >= {'open', 'closed'}


def test_options_explains_every_knob(client):
    body = client.get('/api/options').json()
    for key in ('index.chunker', 'index.summarizer', 'retrieval.reranker',
                'retrieval.grade_threshold', 'generation.answerer', 'model.answer',
                'model.summarize'):
        assert body['help'].get(key), key


def test_defaults_carry_the_per_task_model_fields(client):
    """The panel merges saved settings over these, so a field missing here is a
    dropdown that renders as undefined on an old browser tab."""
    defaults = client.get('/api/options').json()['defaults']
    assert defaults['index']['summarizer_model'] == ''
    assert defaults['retrieval']['reranker_model'] == ''
    assert defaults['retrieval']['grader_model'] == ''
    assert defaults['retrieval']['expansion_model'] == ''
    assert defaults['generation']['judge_model'] == ''
    assert defaults['generation']['ragas_model'] == ''


def test_a_per_task_model_is_accepted_by_the_query_endpoint(client):
    res = client.post('/api/query', json={
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash', 'layers': ['chunk']},
        'retrieval': {'search_layers': ['chunk'], 'k': 4,
                      'grader_model': 'meta-llama/llama-3.3-70b-instruct'},
        'generation': {'answerer': 'extractive', 'judge_model': 'openai/gpt-5'}})
    assert res.status_code == 200
    assert res.json()['contexts']


def test_the_standalone_panel_offers_the_model_pickers_too():
    """The lab still runs without a board, and that panel must not be the one
    place where a model is hard-coded."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'model_roles' in html and 'rag-model' in html


def test_ragas_takes_its_own_judge_model(index, ground_truth):
    pytest.importorskip('ragas')
    pytest.importorskip('rapidfuzz')
    from .raglab import ragas_eval
    question = next(q for q in ground_truth['questions'] if q['answerable'])
    pairs = [(question, pipeline.retrieve(index, RetrievalConfig(k=5),
                                          question['question_fa'],
                                          question['query_date']))]
    report = ragas_eval.run(pairs, LAB_SETTINGS, index.embedder, mode='offline',
                            judge_model='judge/model')
    assert report['n_samples'] == 1, report['notes']


def test_options_say_which_languages_each_embedder_covers(client):
    body = client.get('/api/options').json()
    hints = {hint['kind']: hint for hint in body['embedder_hints']}
    assert set(hints) == set(body['embedders'])
    assert all(hint['languages'] for hint in hints.values())
    assert hints['ascii-hash']['farsi'] is False


def test_options_offer_farsi_capable_embedding_models(client):
    body = client.get('/api/options').json()
    assert body['embed_models'][0]['id'] == ''
    by_id = {entry['id']: entry for entry in body['embed_models']}
    assert by_id['intfloat/multilingual-e5-large']['farsi'] is True
    assert by_id['BAAI/bge-small-en-v1.5']['farsi'] is False
    assert body['defaults']['index']['embed_model'] == ''
    assert body['help']['index.embed_model']


def test_an_embedding_model_is_accepted_by_the_query_endpoint(client):
    """The field has to survive the panel round trip even when the running
    embedder ignores it, or a stale tab breaks a query."""
    res = client.post('/api/query', json={
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash',
                  'embed_model': 'intfloat/multilingual-e5-large',
                  'layers': ['chunk']},
        'retrieval': {'search_layers': ['chunk'], 'k': 4},
        'generation': {'answerer': 'extractive'}})
    assert res.status_code == 200
    assert res.json()['contexts']


def test_the_standalone_panel_offers_the_embedding_models_too():
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'embed_models' in html and 'embedder_hints' in html


def test_options_define_every_metric_the_panel_can_show(client):
    body = client.get('/api/options').json()
    by_key = {measure['key']: measure for measure in body['metrics']}
    # Deterministic and judged metrics arrive through the same shape, so the
    # dashboard renders one concept rather than two.
    for key in ('recall', 'quote_recall', 'headline', 'faithfulness',
                'non_llm_context_recall'):
        measure = by_key.get(key)
        assert measure, key
        assert measure['label'] and measure['short'], key
        assert measure['formula'] and measure['library'] and measure['help'], key
        assert 'step' in measure, key
    assert body['help']['metric.recall']
    assert 'ragas' in by_key['faithfulness']['library'].lower()


def test_options_colour_code_the_pipeline_steps(client):
    """The panel cannot invent the grouping: which step a control belongs to is
    a fact about the pipeline, served with everything else."""
    body = client.get('/api/options').json()
    assert [step['key'] for step in body['steps']] == ['index', 'retrieval',
                                                       'generation']
    assert all(step['label'] and step['short'] and step['note']
               for step in body['steps'])
    steps = {step['key'] for step in body['steps']}
    assert all(role['step'] in steps for role in body['model_roles'])
    by_key = {role['key']: role['step'] for role in body['model_roles']}
    assert by_key['summarize'] == 'index'
    assert by_key['rerank'] == 'retrieval'
    assert by_key['answer'] == 'generation'


def test_options_offer_the_two_new_backends_and_their_models(client):
    body = client.get('/api/options').json()
    assert {'sentence-transformers', 'openai'} <= set(body['embedders'])
    by_id = {entry['id']: entry for entry in body['embed_models']}
    for model_id, (backend, dim, _) in REQUESTED_MODELS.items():
        assert model_id in by_id, model_id
        assert by_id[model_id]['backend'] == backend
        assert by_id[model_id]['dim'] == dim
    # The panel reports what is installed, so a dropdown never promises a
    # download or an API call that cannot happen.
    caps = body['capabilities']
    assert isinstance(caps['sentence_transformers'], bool)
    assert isinstance(caps['openai_embeddings'], bool)


def test_the_standalone_panel_colour_codes_the_steps_too():
    """One ink per step, defined once as a token and applied by data-step, so the
    two panels cannot end up disagreeing about what orange means."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    for token in ('--step-index', '--step-retrieval', '--step-generation'):
        assert token in html, token
    assert 'data-step="index"' in html
    assert 'data-step="retrieval"' in html
    assert 'data-step="generation"' in html


def test_the_standalone_panel_takes_its_metric_definitions_from_the_service():
    """No second list of score labels: the panel that runs without a board has to
    explain a metric the same way the board's page does, or the same number ends
    up with two names and one definition."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'OPTIONS.metrics' in html
    assert 'metric.${key}' in html or "metric.' + key" in html
    assert 'SCORE_CARDS' not in html, 'the hard-coded score list is back'


def test_the_standalone_panel_says_which_backends_consult_the_model():
    """It said "fastembed only", which stopped being true the moment a second
    backend could load a model."""
    import re

    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    label = re.search(r'<label>Embedding model.*?</label>', html, re.S)
    assert label, 'the standalone panel lost its embedding-model label'
    assert 'sentence-transformers' in label.group(0)
    assert 'openai' in label.group(0)


def test_the_standalone_panel_keeps_every_model_in_one_place():
    """The embedder is a language model too, so it belongs in the model column
    with the other seven rather than buried among the chunking knobs."""
    import re

    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    card = re.search(r'<section[^>]*id="modelCard".*?</section>', html, re.S)
    assert card, 'the standalone panel has no model column'
    assert 'id="embedder"' in card.group(0)
    assert 'id="embed_model"' in card.group(0)
