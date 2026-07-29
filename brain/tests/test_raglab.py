"""Tests for the RAG Lab (brain/tests/raglab).

Fully offline: Chroma runs in-process (`memory`), embeddings are the lab's
hash embedders, and no test touches an LLM. The integration tests run against
the real one-year fixture rather than a toy corpus, because the properties worth
asserting — that a Farsi question finds its evidence session, that the current
production embedder finds nothing at all — only exist at that scale.
"""
import os

import numpy as np
import pytest

from .raglab import (chunking, corpus, embedding, evaluate, metrics, pipeline,
                     query, retrieval, summarize, textnorm)
from .raglab.config import (GenerationConfig, IndexConfig, LabConfig, LabSettings,
                            RetrievalConfig)
from .raglab.index import IndexRegistry

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
        def chat(self, messages, tools=None, model=None):
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
