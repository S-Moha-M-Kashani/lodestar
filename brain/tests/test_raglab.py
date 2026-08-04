"""Tests for the RAG Lab (brain/tests/raglab).

Fully offline by construction: the lab's index is process memory, embeddings are
its hash embedders, and no test touches an LLM. The integration tests run against
the real one-year fixture rather than a toy corpus, because the properties worth
asserting — that a Farsi question finds its evidence session, that the current
production embedder finds nothing at all — only exist at that scale.
"""
import json
import os
import re
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest


from lodestar_brain import textnorm

from .raglab import (chunking, config, corpus, embedding, evaluate, explain,
                     leaderboard, metrics, models, pipeline, query, ragas_eval,
                     retrieval, store, sweep)
from .raglab.config import (EMBEDDERS, RERANKERS, GenerationConfig, IndexConfig,
                            LabConfig, LabSettings, RetrievalConfig)
from .raglab.index import IndexRegistry, LabIndex

LAB_SETTINGS = LabSettings(openrouter_api_key='', llm_provider='fake')
RAGLAB_DIR = Path(__file__).resolve().parent / 'raglab'
REPO_ROOT = RAGLAB_DIR.parents[2]


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

# This is a unit test.
def test_normalize_folds_arabic_letterforms_and_digits():
    assert textnorm.normalize('يك') == textnorm.normalize('یک')
    assert '۱۴۰۵' not in textnorm.normalize('سال ۱۴۰۵')
    assert '1405' in textnorm.normalize('سال ۱۴۰۵')


# This is a unit test.
def test_normalize_is_idempotent():
    once = textnorm.normalize('مي‌خواستم   بلاخره ۳ بار')
    assert textnorm.normalize(once) == once


# This is a unit test.
def test_tokens_match_across_half_space_spelling():
    """«می‌خوام» and «می خوام» are the same word to a reader, so they must be
    the same token to BM25 — the corpus spells it both ways."""
    joined = set(textnorm.tokens('می‌خوام برم باشگاه'))
    spaced = set(textnorm.tokens('می خوام برم باشگاه'))
    assert joined & spaced
    assert 'باشگاه' in joined and 'باشگاه' in spaced


# This is a unit test.
def test_tokens_drop_stopwords_but_keep_content():
    tokens = textnorm.tokens('که از به مهسا دعوا')
    assert 'مهسا' in tokens and 'دعوا' in tokens
    assert 'که' not in tokens


# This is a unit test.
def test_sentences_split_spoken_run_ons():
    text = 'امروز رفتم سر کار و بعدش مهسا زنگ زد. خیلی خسته بودم'
    assert len(textnorm.sentences(text)) >= 2


# --- embedders -------------------------------------------------------------

# This is a unit test.
def test_ascii_hash_embedder_is_blind_to_farsi():
    """The finding the lab exists to make measurable, and the one that moved the
    brain's default: an [a-z0-9]+ tokeniser embeds a Farsi diary to the zero
    vector, so retrieval is arbitrary — ~0.01 recall against 0.617 for a real
    Persian encoder. Production has since taken that encoder and retired `hash`
    by name; this stays as the reference point the 60× is measured from."""
    vectors = embedding.make_embedder('ascii-hash').embed(['امروز با مهسا دعوام شد'])
    assert not np.any(vectors)


# This is a unit test.
def test_char_hash_prefers_a_paraphrase_over_an_unrelated_line():
    embedder = embedding.make_embedder('char-hash')
    vectors = embedder.embed(['دعوا با مهسا سر کارهای خونه',
                              'باز با مهسا دعوا کردیم سر خونه',
                              'نامه اداره مالیات رسید'])
    assert float(vectors[0] @ vectors[1]) > float(vectors[0] @ vectors[2])


# This is a unit test.
def test_token_hash_is_normalised_and_nonzero_for_farsi():
    vectors = embedding.make_embedder('token-hash').embed(['خواب بی‌خوابی کمردرد'])
    assert np.any(vectors)
    assert abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-5


# --- chunking --------------------------------------------------------------

# This is a unit test.
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


# This is a unit test.
def test_every_chunker_produces_unique_ids_and_nonempty_text(session):
    embedder = embedding.make_embedder('char-hash')
    for chunker in ('fixed', 'fixed-overlap', 'message', 'turn-pair', 'session',
                    'semantic-drift'):
        cfg = IndexConfig(chunker=chunker, embedder='char-hash')
        chunks = chunking.chunk_session(session, cfg, embedder)
        assert chunks, chunker
        assert len({c.id for c in chunks}) == len(chunks), chunker
        assert all(c.text.strip() for c in chunks), chunker


# This is a unit test.
def test_fixed_chunker_matches_the_production_packing(session):
    """The baseline has to *be* the baseline: same greedy 500-char packing the
    brain ships, or the comparison is against a straw man."""
    from .raglab.chunking import chunk_text
    cfg = IndexConfig(chunker='fixed', chunk_chars=500, contextual=False)
    ours = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))
    theirs = chunk_text(corpus.session_text(session), 500)
    assert [c.text for c in ours] == theirs


# This is a unit test.
def test_contextual_prefix_situates_the_chunk(session):
    cfg = IndexConfig(chunker='message', contextual=True)
    chunk = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))[0]
    assert session['date'] in chunk.prefix
    assert session['mood']['label'] in chunk.prefix
    assert chunk.body and not chunk.body.startswith('[')


# This is a unit test.
def test_overlap_chunker_repeats_material_between_windows(session):
    cfg = IndexConfig(chunker='fixed-overlap', chunk_chars=300, overlap=150,
                      contextual=False)
    chunks = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))
    if len(chunks) < 2:
        pytest.skip('session too short to window')
    total = sum(len(c.text) for c in chunks)
    assert total > len(corpus.session_text(session))


# This is a unit test.
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


# This is a unit test.
def test_chunk_metadata_is_chroma_safe(session):
    cfg = IndexConfig(chunker='message')
    chunk = chunking.chunk_session(session, cfg, embedding.make_embedder('char-hash'))[0]
    for key, value in chunk.metadata().items():
        assert isinstance(value, (str, int, float, bool)), key


# This is a unit test.
def test_importance_rises_with_emotional_intensity():
    calm = {'mood': {'label': 'آروم', 'valence': 6, 'arousal': 2}}
    wrecked = {'mood': {'label': 'داغون', 'valence': 1, 'arousal': 9}}
    assert chunking.importance_of(wrecked) > chunking.importance_of(calm)


# --- summary hierarchy -----------------------------------------------------

# --- habits: the card you repeat instead of finish -------------------------
# The board grew a habit type — a card carrying `habitCount` repetitions per
# `habitFreq` period and a `habitHistory` of the completions themselves. Diary
# memory has to be able to answer questions about it, and none of the existing
# layers can: "how many times did I go to the gym in تیر" is an aggregation over
# fifty sessions, and "am I still doing German" is a knowledge-update question
# whose answer is an *absence* of recent entries. So the corpus declares its
# habits the way a board card does, and the lab indexes an adherence ledger
# beside the raw text.

HABIT_FREQS = ('daily', 'weekly', 'monthly', 'yearly')


def habit_period(freq: str, day: str) -> str:
    """The board's period id for a date, reimplemented from the spec rather than
    imported — app.js is the other implementation, and a test that shares code
    with the thing it checks cannot catch the two drifting apart."""
    from datetime import date
    d = date.fromisoformat(day)
    if freq == 'yearly':
        return f'{d.year}'
    if freq == 'monthly':
        return f'{d.year}-{d.month:02d}'
    if freq == 'weekly':
        year, week, _ = d.isocalendar()
        return f'{year}-W{week:02d}'
    return d.isoformat()


# This is a unit test.
def test_the_corpus_declares_its_habits_the_way_a_board_card_does(diary):
    habits = diary['habits']
    assert habits, 'the corpus must carry the habits the diarist tracks'
    for slug, habit in habits.items():
        assert habit['freq'] in HABIT_FREQS, slug
        assert habit['count'] >= 1, slug
        assert habit['title_fa'], slug
        assert isinstance(habit['times'], list), slug
        assert isinstance(habit['history'], dict), slug
    # All four board cadences are not required, but a corpus that only ever
    # measured weekly habits would leave the monthly and daily period arithmetic
    # untested by every run.
    assert {h['freq'] for h in habits.values()} >= {'daily', 'weekly', 'monthly'}


# This is a unit test.
def test_every_habit_completion_sits_in_the_period_it_is_filed_under(diary):
    """`habitHistory` is bucketed by period id, so a date filed under the wrong
    bucket would make every count wrong in a way no other assertion notices."""
    for slug, habit in diary['habits'].items():
        for period, days in habit['history'].items():
            for day in days:
                assert habit_period(habit['freq'], day) == period, f'{slug} {day}'


# This is a unit test.
def test_a_habit_is_never_punched_more_often_than_its_period_asks(diary):
    """The punch strip has exactly `count` boxes, so a history with more
    completions than that in one period could not have come from the board."""
    for slug, habit in diary['habits'].items():
        for period, days in habit['history'].items():
            assert len(days) <= habit['count'], f'{slug} {period}'
            assert len(days) == len(set(days)), f'{slug} {period} has a repeat'


# This is a unit test.
def test_the_habit_sessions_joined_the_corpus_without_disturbing_it(diary):
    """Additive on purpose: the habit sessions were appended on dates the corpus
    had not used, so every pre-existing session — and therefore every cached
    summary and every earlier leaderboard row — stays exactly as it was."""
    sessions = diary['sessions']
    ids = [s['session_id'] for s in sessions]
    assert len(ids) == len(set(ids)), 'a session id was reused'
    assert ids == sorted(ids), 'the corpus must stay chronological'
    habit_sessions = [s for s in sessions if 'habit-tracking' in s['recurring_threads']]
    assert len(habit_sessions) >= 8
    period = diary['meta']['period']
    for s in habit_sessions:
        assert period['from'] <= s['date'] <= period['to'], s['session_id']


# This is a unit test.
def test_the_habit_storyline_is_described_like_every_other_thread(diary):
    """thread_layer builds its digest title from this description; a thread with
    no entry gets an empty one, which reads as a bug in the digest."""
    assert diary['threads']['habit-tracking']


# This is a unit test.
def test_every_chunk_reports_a_habit_field_even_when_it_has_none(session):
    """Chroma metadata is a fixed shape per collection in practice: a field that
    only some rows carry turns a `where` clause into a silent partial scan."""
    chunk = chunking.chunk_session(session, IndexConfig(chunker='session'),
                                   None)[0]
    assert chunk.metadata()['session_id'] == session['session_id']


# This is a unit test.
def test_habit_questions_cite_verbatim_evidence_like_the_rest_of_the_set(
        diary, ground_truth):
    """The evidence quote is what quote-recall measures survival of, so a quote
    that is not literally in its cited message silently scores every config down."""
    sessions = corpus.sessions_by_id(diary)
    habit_questions = [q for q in ground_truth['questions']
                       if q['type'] == 'habit']
    assert len(habit_questions) >= 6
    for question in habit_questions:
        assert question['answer_fa'] and question['key_facts'], question['id']
        for ev in question['evidence']:
            messages = sessions[ev['session_id']]['messages']
            cited = ' '.join(messages[i]['content'] for i in ev['message_indices'])
            assert ev['quote'] in cited, f"{question['id']} {ev['session_id']}"


# This is a unit test.
def test_every_question_type_is_one_the_report_breaks_down(ground_truth):
    """metrics.aggregate walks TYPES, so a question type missing from it is
    dropped from the per-type table without any error — the breakdown just
    quietly stops covering part of the set."""
    assert {q['type'] for q in ground_truth['questions']} <= set(metrics.TYPES)


# --- query understanding ---------------------------------------------------

# This is a unit test.
@pytest.mark.parametrize('question,expect_from,expect_to', [
    ('آذر چه خبر بود؟', 20251122, 20251221),
    ('پارسال پاییز حالم چطور بود؟', 20240923, 20241221),
    ('نوروز چی شد؟', 20260318, 20260404),
])
def test_time_scopes_resolve_to_the_right_window(question, expect_from, expect_to):
    scope = query.resolve_time_scope(question, '2026-07-28')
    assert scope is not None, question
    assert (scope.from_int, scope.to_int) == (expect_from, expect_to)


# This is a unit test.
def test_untimed_question_has_no_scope():
    assert query.resolve_time_scope('چرا با مهسا دعوا می‌کنیم؟', '2026-07-28') is None


# This is a unit test.
def test_relative_month_scope_is_the_previous_calendar_month():
    scope = query.resolve_time_scope('ماه پیش چی کار کردم؟', '2026-07-28')
    assert scope and (scope.from_int, scope.to_int) == (20260601, 20260630)


# This is a unit test.
def test_where_clause_overlaps_rather_than_contains():
    """A chunk whose span straddles the edge of the window is kept: a scope asks
    about a period, it does not claim the evidence sits entirely inside it."""
    scope = query.TimeScope(20260101, 20260131, 'دی', 'jalali-month')
    clause = query.where_clause(scope)
    assert clause['$and'][0] == {'span_from': {'$lte': 20260131}}
    assert clause['$and'][1] == {'span_to': {'$gte': 20260101}}
    assert query.where_clause(None) is None


# This is a unit test.
def test_expansion_adds_a_synonym_variant():
    variants = query.expand('دعوا با همسرم سر چی بود؟')
    assert len(variants) >= 2
    assert any('مهسا' in v for v in variants)


# This is a unit test.
def test_keyword_query_strips_interrogatives():
    assert 'چی' not in query.keyword_query('حال مامان چی شد؟')


# --- retrieval primitives --------------------------------------------------

# This is a unit test.
def test_bm25_finds_the_document_with_the_rare_term():
    bm25 = retrieval.BM25(['نامه اداره مالیات رسید و جریمه خوردم',
                           'با مهسا دعوا کردیم', 'رفتم پیاده‌روی'])
    top = bm25.top('مالیات جریمه', 2)
    assert top and top[0][0] == 0


# This is a unit test.
def test_bm25_respects_the_allowed_mask():
    bm25 = retrieval.BM25(['مالیات', 'مالیات'])
    allowed = np.array([False, True])
    assert [i for i, _ in bm25.top('مالیات', 2, allowed)] == [1]


# This is a unit test.
def test_rrf_ranks_a_document_both_retrievers_agree_on_first():
    fused = retrieval.rrf([['a', 'b', 'c'], ['b', 'a', 'd']])
    assert max(fused, key=fused.get) in ('a', 'b')
    assert fused['a'] > fused['c'] and fused['b'] > fused['d']


# This is a unit test.
def test_mmr_breaks_up_near_duplicates():
    vectors = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    relevance = np.array([1.0, 0.99, 0.5], dtype=np.float32)
    assert retrieval.mmr(vectors, relevance, 2, 1.0) == [0, 1]
    assert retrieval.mmr(vectors, relevance, 2, 0.5) == [0, 2]


# This is a unit test.
def test_mmr_falls_back_when_vectors_are_missing():
    relevance = np.array([0.2, 0.9], dtype=np.float32)
    assert retrieval.mmr(np.zeros((0, 2), dtype=np.float32), relevance, 2, 0.5) == [1, 0]


# This is a unit test.
def test_recency_weight_halves_after_one_half_life():
    weight = retrieval.recency_weight(20260101, 20260701, 180.0)
    assert 0.4 < weight < 0.6


# This is a unit test.
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

# This is a unit test.
def test_retrieval_metric_arithmetic():
    retrieved, gold = ['a', 'x', 'b'], ['a', 'b', 'c']
    assert metrics.recall_at_k(retrieved, gold, 3) == pytest.approx(2 / 3)
    assert metrics.precision_at_k(retrieved, gold, 3) == pytest.approx(2 / 3)
    assert metrics.mrr(retrieved, gold) == 1.0
    assert metrics.hit_at_k(['x'], gold, 1) == 0.0
    assert metrics.ndcg_at_k(['a', 'b'], gold, 2) > metrics.ndcg_at_k(['x', 'a'], gold, 2)


# This is a unit test.
def test_quote_recall_needs_the_answering_sentence_not_just_the_session():
    question = {'evidence': [{'session_id': 's1', 'message_indices': [0],
                              'quote': 'آذر تموم شد و از هیچ شرکتی هیچ خبری نیس'}]}
    assert metrics.quote_recall('حرف‌های دیگری از همان نشست', question) == 0.0
    assert metrics.quote_recall('گفتم آذر تموم شد و از هیچ شرکتی هیچ خبری نیس بعدش',
                                question) == 1.0


# This is a unit test.
def test_quote_recall_tolerates_whitespace_normalisation():
    question = {'evidence': [{'quote': 'می خوام برم باشگاه', 'session_id': 's',
                              'message_indices': [0]}]}
    assert metrics.quote_recall('گفت می  خوام   برم باشگاه', question) == 1.0


# This is a unit test.
def test_latest_state_session_is_the_newest_evidence():
    question = {'evidence': [{'session_id': '2025-12-01-a'},
                             {'session_id': '2026-05-12-a'}]}
    assert metrics.latest_state_session(question) == '2026-05-12-a'


# This is a unit test.
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


# --- the ephemeral vector store --------------------------------------------
# An experiment's vectors, contexts and answers live for the process and no
# longer: the lab owns a store in memory instead of a Chroma database, and its
# only durable output is the JSON run file. These tests are that boundary. They
# exist because the persistence came back as one import line the first time.

# This is a unit test.
def test_memory_store_ranks_by_cosine_distance():
    """Chroma's contract, because LabIndex.dense reads it: `distances`, not
    similarities, so the caller's `1 - d` keeps meaning what it meant."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['near', 'orthogonal', 'opposite'],
                   documents=['a', 'b', 'c'],
                   embeddings=[[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]],
                   metadatas=[{'layer': 'chunk'}] * 3)
    res = vectors.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert res['ids'][0] == ['near', 'orthogonal', 'opposite']
    assert res['distances'][0] == pytest.approx([0.0, 1.0, 2.0], abs=1e-6)
    assert res['documents'][0][0] == 'a'
    assert res['metadatas'][0][0] == {'layer': 'chunk'}


# This is a unit test.
def test_memory_store_answers_several_query_vectors_at_once():
    """Multi-query expansion sends one row per variant and merges the results,
    so a store that silently answered only the first would score expansion as
    doing nothing."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['x', 'y'], documents=['a', 'b'],
                   embeddings=[[1.0, 0.0], [0.0, 1.0]], metadatas=[{}, {}])
    res = vectors.query(query_embeddings=[[1.0, 0.0], [0.0, 1.0]], n_results=1)
    assert res['ids'] == [['x'], ['y']]


# This is a unit test.
def test_memory_store_upsert_replaces_a_record_instead_of_duplicating_it():
    """Chunk ids are deterministic, so a rebuild writes the same ids again."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['a'], documents=['first'], embeddings=[[1.0, 0.0]],
                   metadatas=[{'layer': 'chunk'}])
    vectors.upsert(ids=['a'], documents=['second'], embeddings=[[0.0, 1.0]],
                   metadatas=[{'layer': 'session'}])
    assert vectors.count() == 1
    res = vectors.query(query_embeddings=[[0.0, 1.0]], n_results=1)
    assert res['documents'][0] == ['second']
    assert res['metadatas'][0] == [{'layer': 'session'}]


# This is a unit test.
def test_memory_store_applies_the_where_clause_the_lab_actually_builds():
    """The filter is the one place a hand-rolled store could quietly differ from
    Chroma, so it is asserted against `query.where_clause` itself rather than a
    hand-written dict."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(
        ids=['in-scope', 'too-early', 'too-late'],
        documents=['keep', 'drop', 'drop'],
        embeddings=[[1.0, 0.0]] * 3,
        metadatas=[{'span_from': 20251201, 'span_to': 20251201},
                   {'span_from': 20250101, 'span_to': 20250101},
                   {'span_from': 20260301, 'span_to': 20260301}])
    where = query.where_clause(query.TimeScope(20251122, 20251221, 'آذر', 'jalali'))
    res = vectors.query(query_embeddings=[[1.0, 0.0]], n_results=3, where=where)
    assert res['ids'][0] == ['in-scope']


# This is a unit test.
def test_memory_store_keeps_a_chunk_that_merely_overlaps_the_scope():
    """The same property `where_clause` documents: a chunk spanning a wide range
    is kept when it overlaps the window, because containment would drop exactly
    the evidence a scoped question is reaching for."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['thread'], documents=['a year of it'],
                   embeddings=[[1.0, 0.0]],
                   metadatas=[{'span_from': 20250801, 'span_to': 20260720}])
    where = query.where_clause(query.TimeScope(20251122, 20251221, 'آذر', 'jalali'))
    res = vectors.query(query_embeddings=[[1.0, 0.0]], n_results=3, where=where)
    assert res['ids'][0] == ['thread']


# This is a unit test.
def test_memory_store_does_not_match_a_metadata_key_a_record_lacks():
    """Chroma's semantics, and the reason `Chunk.metadata()` carries `habit` on
    every chunk: a record missing the filtered key is excluded, never kept."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['has', 'lacks'], documents=['a', 'b'],
                   embeddings=[[1.0, 0.0], [1.0, 0.0]],
                   metadatas=[{'habit': 'gym'}, {}])
    res = vectors.query(query_embeddings=[[1.0, 0.0]], n_results=2,
                        where={'habit': 'gym'})
    assert res['ids'][0] == ['has']


# This is a unit test.
def test_memory_store_never_returns_more_than_it_holds():
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['a'], documents=['one'], embeddings=[[1.0, 0.0]],
                   metadatas=[{}])
    assert vectors.query(query_embeddings=[[1.0, 0.0]], n_results=8)['ids'] == [['a']]
    empty = store.MemoryVectors('raglab-empty')
    assert empty.count() == 0
    assert empty.query(query_embeddings=[[1.0, 0.0]], n_results=5)['ids'] == [[]]


# This is a unit test.
def test_memory_store_returns_stored_vectors_in_the_order_asked_for():
    """`LabIndex.vectors_for` reads vectors back for MMR rather than re-embedding
    them, and it zips the result against the ids it asked for."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['a', 'b'], documents=['x', 'y'],
                   embeddings=[[1.0, 0.0], [0.0, 1.0]], metadatas=[{}, {}])
    got = vectors.get(ids=['b', 'a'], include=['embeddings'])
    assert got['ids'] == ['b', 'a']
    assert np.allclose(got['embeddings'][0], [0.0, 1.0])
    assert np.allclose(got['embeddings'][1], [1.0, 0.0])


# This is a unit test.
def test_memory_store_get_skips_an_id_it_does_not_hold():
    """A silent partial result, exactly like Chroma's: the caller pairs ids with
    vectors by name, so a placeholder row would be a wrong vector."""
    vectors = store.MemoryVectors('raglab-test')
    vectors.upsert(ids=['a'], documents=['x'], embeddings=[[1.0, 0.0]],
                   metadatas=[{}])
    assert vectors.get(ids=['a', 'missing'], include=['embeddings'])['ids'] == ['a']


# This is an integration test.
def test_the_index_holds_its_vectors_in_process_memory(index):
    """Asserted on the type: 'there is no database' is not observable from a
    query that succeeds."""
    assert isinstance(index.store, store.MemoryVectors)
    assert index.store.count() == index.stats.chunks


# This is a configuration invariant: the lab must never grow a vector-database dependency.
def test_no_lab_module_imports_a_vector_database_client():
    """chromadb is production's dependency, not the lab's — and neither is
    production's ChatStore. This is the line that would bring the persistence
    back: it is one import, and it looks harmless."""
    offenders = []
    for path in sorted(RAGLAB_DIR.glob('*.py')):
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped.startswith(('import ', 'from ')):
                continue
            if 'chromadb' in stripped or 'ChatStore' in stripped:
                offenders.append(f'{path.name}: {stripped}')
    assert offenders == []


# This is an integration test.
def test_a_fresh_lab_process_rebuilds_its_index(diary):
    """Nothing outlives the registry. A second one over the same config has to
    build again rather than find a collection waiting for it, which is what
    'ephemeral' means in a test."""
    cfg = IndexConfig(chunker='session', embedder='token-hash')
    first = IndexRegistry(LAB_SETTINGS, diary).get(cfg)
    second = IndexRegistry(LAB_SETTINGS, diary).get(cfg)
    assert second is not first and second.store is not first.store
    assert not first.stats.reused and not second.stats.reused
    assert second.stats.chunks == first.stats.chunks
    assert second.store.count() == second.stats.chunks


# This is an integration test.
def test_building_an_index_opens_no_socket(diary, monkeypatch):
    """The strongest form of the boundary: with nothing to talk to, a build must
    not be able to reach anything. The offline embedders download nothing, so a
    connection here could only be a store trying to persist."""
    def refuse(*_args, **_kwargs):
        raise AssertionError('the lab opened a network connection while building')

    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    built = IndexRegistry(LAB_SETTINGS, diary).get(
        IndexConfig(chunker='session', embedder='ascii-hash'))
    assert built.stats.chunks == len(diary['sessions'])


# --- index and pipeline (integration, in-process memory) -------------------

# This is an integration test.
def test_index_is_reused_for_the_same_fingerprint(registry):
    """`reused` used to mean "Chroma already held the right number of records" —
    the one form of reuse that can no longer happen. It now reports the only one
    left: this process built the index earlier and still has it."""
    cfg = IndexConfig(chunker='turn-pair', embedder='token-hash')
    first = registry.get(cfg)
    assert not first.stats.reused
    assert registry.get(cfg) is first
    assert first.stats.reused
    assert first.stats.collection == cfg.collection()


# This is a unit test.
def test_different_configs_get_different_collections():
    a = IndexConfig(chunker='fixed').collection()
    b = IndexConfig(chunker='session').collection()
    assert a != b and a.startswith('raglab-')


# This is an integration test.
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


# This is an integration test.
def test_time_filter_narrows_the_candidate_pool(index, ground_truth):
    scoped = 'آذر چه خبر بود؟'
    with_filter = pipeline.retrieve(index, RetrievalConfig(time_filter=True),
                                    scoped, '2026-07-28')
    without = pipeline.retrieve(index, RetrievalConfig(time_filter=False),
                                scoped, '2026-07-28')
    assert with_filter.time_scope is not None
    assert (with_filter.diagnostics['candidates_in_scope']
            < without.diagnostics['candidates_in_scope'])
    dates = [corpus.date_int(c.date) for c in with_filter.contexts]
    assert dates and all(20251122 <= d <= 20251221 for d in dates), dates


# This is an integration test.
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


# This is an integration test.
def test_answerer_emits_the_refusal_when_abstaining(index):
    outcome = pipeline.retrieve(index, RetrievalConfig(grader='lexical',
                                                      grade_threshold=0.99),
                                'قرارداد کشتی', '2026-07-28')
    outcome = pipeline.answer(outcome, GenerationConfig(answerer='extractive'))
    assert outcome.answer == pipeline.REFUSAL
    assert outcome.abstained


# This is a unit test.
def test_quoting_the_diarist_saying_i_dont_know_is_not_an_abstention():
    """The diarist writes «نمیدونم» constantly. Counting it as a refusal scored
    6.5% of answerable questions as abstentions on a pipeline with no gate."""
    assert not pipeline.reads_as_refusal('نمیدونم چیکار کنم [2026-01-05-a]',
                                         'extractive')
    assert not pipeline.reads_as_refusal(
        'کارت رو عوض کردی. خودت گفتی نمیدونم درست بود یا نه.', 'llm')
    assert pipeline.reads_as_refusal(pipeline.REFUSAL, 'extractive')
    assert pipeline.reads_as_refusal('چیزی در این مورد ذکر نشده.', 'llm')


# This is an integration test.
def test_ascii_hash_baseline_retrieves_worse_than_char_hash(registry, ground_truth):
    """The lab's headline comparison, asserted: the production embedder cannot
    represent this corpus, so it must lose to a Unicode-aware one."""
    questions = [q for q in ground_truth['questions']
                 if q['type'] == 'single-hop'][:8]
    cfg = RetrievalConfig(retriever='dense', k=8, reranker='none', time_filter=False)

    def rate(embedder_name):
        index = registry.get(IndexConfig(chunker='fixed', embedder=embedder_name,
                                         contextual=False))
        total = 0.0
        for question in questions:
            outcome = pipeline.retrieve(index, cfg, question['question_fa'],
                                        question['query_date'])
            total += metrics.hit_at_k(outcome.sessions,
                                      corpus.evidence_sessions(question), cfg.k)
        return total / len(questions)

    assert rate('char-hash') > rate('ascii-hash')


# --- evaluation harness ----------------------------------------------------

# This is an integration test.
def test_a_run_writes_one_json_file_and_nothing_else(registry, ground_truth,
                                                     tmp_path, monkeypatch):
    """The whole artifact policy in one assertion. A run's index, contexts and
    answers are experimental data and die with the process; the single thing that
    outlives it is one strict-JSON file holding the config, the metrics and the
    per-question detail needed to reopen the result.

    `rglob` rather than `glob`, because the way this rule broke before was a
    subdirectory — `.runs/cache/` — appearing beside the runs."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='session', embedder='char-hash'),
                    retrieval=RetrievalConfig(k=4),
                    generation=GenerationConfig(answerer='extractive'))
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=2, ragas_mode='off')
    assert [p.name for p in tmp_path.rglob('*')] == [f'{result.run_id}.json']
    saved = json.loads((tmp_path / f'{result.run_id}.json').read_text(
        encoding='utf-8'), parse_constant=lambda literal: pytest.fail(
            f'{literal} is not JSON a strict parser accepts'))
    assert saved['run_id'] == result.run_id
    assert saved['config'] and saved['summary'] and saved['rows']


# This is an integration test.
def test_run_eval_scores_a_slice_end_to_end(registry, ground_truth, tmp_path,
                                            monkeypatch):
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='message', embedder='char-hash',
                                      contextual=True),
                    retrieval=RetrievalConfig(k=6, reranker='lexical'),
                    generation=GenerationConfig(answerer='extractive'),
                    label='test-slice')
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=12, ragas_mode='off')
    assert len(result.rows) == 12
    assert result.summary['overall']['headline'] is not None
    assert result.summary['by_type']
    assert (tmp_path / f'{result.run_id}.json').exists()
    assert all('answer' in row for row in result.rows)


# This is an integration test.
def test_started_at_is_when_the_run_started(registry, ground_truth, tmp_path,
                                            monkeypatch):
    """`started_at` must agree with the run id, which is stamped at the start.

    The document that cites these runs prints `started_at` as the time the
    experiment began; a field named for the start that actually holds the finish
    turns a 10-minute run into a timeline nobody can reconstruct."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='session', embedder='char-hash'),
                    retrieval=RetrievalConfig(k=4),
                    generation=GenerationConfig(answerer='extractive'))
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=2, ragas_mode='off')
    stamp = result.run_id.split('-')[1]                      # HHMMSS
    assert result.started_at.endswith(f'{stamp[:2]}:{stamp[2:4]}:{stamp[4:]}')


# This is a unit test.
def test_select_questions_strides_across_types(ground_truth):
    picked = evaluate.select_questions(ground_truth, limit=10)
    assert len(picked) == 10
    assert len({q['type'] for q in picked}) > 1, 'a limited run must stay diverse'


# This is a unit test.
def test_a_limited_run_reaches_the_end_of_the_question_set(ground_truth):
    """Striding with `questions[::step][:limit]` silently drops a tail whenever
    the count is not a multiple of the limit: at 112 questions and a limit of 20
    the step is 5, `::5` yields 23, and truncating to 20 stops at index 95 — so
    the last 16 questions could never be sampled.

    That is not a rounding detail. The set is grouped by type and the newest type
    is appended at the end, so the effect is that the most recently added
    question type is absent from every limited run, and a sweep tuned on those
    runs is tuned on a corpus that excludes it."""
    questions = ground_truth['questions']
    for limit in (5, 10, 20, 25, 40):
        picked = evaluate.select_questions(ground_truth, limit=limit)
        assert len(picked) == limit, limit
        ids = [q['id'] for q in picked]
        assert len(set(ids)) == limit, f'{limit} produced duplicates'
        assert ids[0] == questions[0]['id'], limit
        # The last pick is within one stride of the end, not 16 short of it.
        stride = -(-len(questions) // limit)          # ceil
        assert questions.index(picked[-1]) >= len(questions) - stride, limit


# This is a unit test.
def test_a_limited_run_covers_the_newest_question_type(ground_truth):
    """The concrete consequence, asserted on the type that exposed it: habit
    questions are last in the file, so a limit that cannot reach the end cannot
    measure habit retrieval at all."""
    picked = evaluate.select_questions(ground_truth, limit=20)
    assert any(q['type'] == 'habit' for q in picked)


# This is a unit test.
def test_config_round_trips_through_the_panel_payload():
    cfg = LabConfig.from_dict({'index': {'chunker': 'session', 'unknown': 1},
                               'retrieval': {'k': 3},
                               'generation': {'answerer': 'none'},
                               'label': 'x'})
    assert cfg.index.chunker == 'session' and cfg.retrieval.k == 3
    assert cfg.validate() == []
    assert LabConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()


# This is a unit test.
def test_the_lab_names_no_vector_database_at_all():
    """The guard used to be "refuse the production database". Having no such
    setting is the stronger version of it: a database the lab cannot name is one
    it cannot be pointed at by a typo, an old shell, or a copied command."""
    settings = LabSettings()
    assert [f for f in vars(settings) if 'chroma' in f or 'database' in f] == []
    with pytest.raises(TypeError):
        LabSettings(chroma_database='lodestar')


# This is a unit test.
def test_the_lab_ignores_a_leftover_chroma_environment(monkeypatch):
    """The board's Chroma stack runs whenever a board does, and a shell that ran
    the old lab commands still exports these. Neither may reach the lab."""
    monkeypatch.setenv('RAGLAB_CHROMA_DATABASE', 'lodestar')
    monkeypatch.setenv('BRAIN_CHROMA_URL', 'http://localhost:8001')
    assert 'lodestar' not in repr(config.load_lab_settings())


# --- RAGAS bridge ----------------------------------------------------------

# This is a unit test.
def test_ragas_telemetry_is_disabled_on_import():
    """RAGAS's usage ping blocks for ~150 seconds per evaluate() call when its
    endpoint is unreachable — longer than the measurement itself by three orders
    of magnitude. Importing the bridge must be enough to prevent that."""
    from .raglab import ragas_eval  # noqa: F401
    assert os.environ.get('RAGAS_DO_NOT_TRACK') == 'true'


# This is a unit test.
def test_ragas_availability_reports_missing_pieces_instead_of_raising():
    from .raglab import ragas_eval
    status = ragas_eval.availability(LAB_SETTINGS)
    assert isinstance(status.installed, bool)
    if status.installed:
        assert not status.llm_ready   # no key in LAB_SETTINGS
    assert 'ragas' in status.as_dict()['install_hint']


# This is a unit test.
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


# This is a unit test.
def test_evidence_texts_fall_back_to_quotes_for_unknown_sessions():
    question = {'evidence': [{'session_id': 'nope', 'message_indices': [0],
                              'quote': 'یه چیزی'}]}
    assert corpus.evidence_texts({}, question) == ['یه چیزی']


# This is a unit test.
def test_json_safe_replaces_undefined_metrics_with_null():
    assert evaluate.json_safe({'a': float('nan'), 'b': [1.0, float('nan')]}) == \
        {'a': None, 'b': [1.0, None]}


# This is an integration test.
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


# --- the four metrics that decide the architecture -------------------------
# Everything the lab measures is reported, but only four of them get a vote.
# They are RAGAS's, they are judged, and between them they cover the two ways a
# RAG pipeline fails: retrieval that did not fetch what was needed (context
# precision and recall) and generation that did not stay inside what it fetched
# (faithfulness, answer relevancy). Our own deterministic metrics are the ones
# to *debug* with — they cannot be gamed and they never vary — but they grade
# retrieval almost exclusively, so ranking on them picks a config that finds
# evidence and says nothing useful about it.

# This is a unit test.
def test_the_deciding_metrics_are_exactly_the_four_chosen_ones():
    from .raglab import ragas_eval
    assert ragas_eval.DECISION_METRICS == (
        'faithfulness', 'answer_relevancy',
        'llm_context_precision_with_reference', 'context_recall')
    # Everything else stays measured and reported; it simply does not vote.
    assert set(ragas_eval.DECISION_METRICS) < set(ragas_eval.LLM_METRICS)
    assert 'factual_correctness(mode=f1)' not in ragas_eval.DECISION_METRICS


# This is a unit test.
def test_the_decision_score_is_the_unweighted_mean_of_those_four():
    """Unweighted on purpose: any weighting would be a claim about their relative
    importance that this fixture cannot support, and a hidden thumb on the scale
    is how a sweep ends up confirming whatever the author already believed."""
    from .raglab import ragas_eval
    score = ragas_eval.decision_score({
        'faithfulness': 1.0, 'answer_relevancy': 0.6,
        'llm_context_precision_with_reference': 0.4, 'context_recall': 0.0,
        # Present, reported, and deliberately ignored by the arithmetic.
        'factual_correctness(mode=f1)': 0.0, 'non_llm_context_recall': 1.0,
    })
    assert score == 0.5


# This is a unit test.
def test_the_decision_score_is_undefined_unless_all_four_are_present():
    """A mean over whichever metrics happened to succeed is not comparable
    between runs: an offline run would score on two metrics and outrank a judged
    run scored on four."""
    from .raglab import ragas_eval
    assert ragas_eval.decision_score({'faithfulness': 1.0}) is None
    assert ragas_eval.decision_score({}) is None
    assert ragas_eval.decision_score(
        {'faithfulness': 1.0, 'answer_relevancy': 1.0,
         'llm_context_precision_with_reference': 1.0}) is None


# This is a unit test.
def test_the_decision_score_carries_its_own_uncertainty():
    """A ranking of means with no spread cannot say whether it ranked anything.

    The candidates in this sweep land within ~0.01 of each other on 24
    questions. Whether that is a result or noise is not a matter of opinion, and
    the leaderboard is the wrong place to leave it unanswered — so the score
    ships with the standard error of the per-question composite beside it.

    Computed per question and then across questions, not per metric: the four
    are measured on the same answers and are correlated, and averaging four
    independent standard errors would understate the real spread."""
    from .raglab import ragas_eval
    rows = [{'faithfulness': 1.0, 'answer_relevancy': 1.0,
             'llm_context_precision_with_reference': 1.0, 'context_recall': 1.0},
            {'faithfulness': 0.0, 'answer_relevancy': 0.0,
             'llm_context_precision_with_reference': 0.0, 'context_recall': 0.0}]
    spread = ragas_eval.decision_spread(rows)
    assert spread['n'] == 2
    assert spread['mean'] == 0.5
    # Two composites at 0 and 1: sd = 0.7071, so SE = sd/sqrt(2) = 0.5.
    assert spread['stderr'] == 0.5

    # A question missing one of the four has no composite, so it cannot be one.
    partial = ragas_eval.decision_spread(
        rows + [{'faithfulness': 1.0, 'answer_relevancy': 1.0}])
    assert partial['n'] == 2, 'a partial composite is not a sample'

    # One question cannot have a standard error, and must not claim zero.
    assert ragas_eval.decision_spread(rows[:1])['stderr'] is None
    assert ragas_eval.decision_spread([])['n'] == 0


# This is a unit test.
def test_every_ragas_report_carries_a_spread_even_when_it_measured_nothing():
    """The key has to exist on every path, because the leaderboard reads it.

    A run that could not measure the four reports `n=0` rather than omitting the
    field: a missing key would make the frontend fall back to printing the bare
    mean, which is the presentation this exists to prevent."""
    from .raglab import ragas_eval
    report = ragas_eval.run([], LAB_SETTINGS, None, mode='off')
    assert report['decision_spread'] == {'n': 0, 'mean': None, 'stderr': None}


# This is an integration test.
def test_an_offline_ragas_run_reports_no_decision_score(index, ground_truth):
    """The offline mode cannot measure any of the four, so it must say so rather
    than produce a number that looks comparable to a judged run's."""
    pytest.importorskip('ragas')
    pytest.importorskip('rapidfuzz')
    from .raglab import ragas_eval
    questions = [q for q in ground_truth['questions'] if q['answerable']][:2]
    pairs = [(q, pipeline.retrieve(index, RetrievalConfig(k=5), q['question_fa'],
                                   q['query_date'])) for q in questions]
    report = ragas_eval.run(pairs, LAB_SETTINGS, index.embedder, mode='offline')
    assert report['decision'] is None
    assert report['decision_metrics'] == list(ragas_eval.DECISION_METRICS)


# This is a unit test.
def test_the_decision_score_explains_itself_like_every_other_number():
    """It is the number the architecture was chosen by, so of everything on the
    screen it is the one that must not be a bare figure."""
    keys = {measure['key']: measure for measure in explain.measures()}
    decision = keys.get('ragas_decision')
    assert decision, 'the deciding score has no definition'
    assert decision['formula'] and decision['library'] and decision['help']
    for name in ('faithfulness', 'answer relevancy', 'context precision',
                 'context recall'):
        assert name in decision['help'].lower(), name
    assert explain.topics()['metric.ragas_decision']


# This is an integration test.
def test_the_leaderboard_row_carries_the_deciding_score(index, ground_truth):
    """A leaderboard that ranks on a number it does not carry cannot be checked
    against the run it came from."""
    result = evaluate.RunResult(
        run_id='x', label='y', config={}, index={},
        summary={'overall': {}, 'n_questions': 0},
        ragas={'mode': 'llm', 'metrics': {'faithfulness': 0.8},
               'decision': 0.75, 'decision_metrics': [],
               'decision_spread': {'n': 24, 'mean': 0.75, 'stderr': 0.05}})
    assert result.brief()['ragas_decision'] == 0.75
    # The error travels with the mean, or the row it lands in cannot say whether
    # it beat the row below it.
    assert result.brief()['ragas_decision_stderr'] == 0.05


# This is a unit test.
def test_a_row_recorded_before_the_spread_existed_reports_no_error(tmp_path,
                                                                  monkeypatch):
    """Older runs have no per-question composites to recover, so the row says
    so — an absent error must not be rendered as `± 0`, which would claim the
    run was measured more precisely than the ones that carry a real number."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    (tmp_path / '20260101-000000-abcdef.json').write_text(json.dumps({
        'run_id': '20260101-000000-abcdef', 'label': 'old',
        'summary': {'n_questions': 24, 'overall': {}},
        'ragas': {'metrics': {}, 'decision': 0.6}}), encoding='utf-8')
    row = evaluate.list_runs()[0]
    assert row['ragas_decision'] == 0.6
    assert row['ragas_decision_stderr'] is None


# --- the sweep that produces the leaderboard --------------------------------
# The sweep spends hours of judged model calls, so everything it can get wrong
# silently is worth an assertion: a row that is not comparable to the others, a
# row that cannot be ranked at all, and a row that raises after 40 minutes.

# This is a unit test.
def test_every_candidate_is_selected_by_a_unique_letter():
    """`--only A F` and `--final G` select on `label.split()[0]`.

    Two candidates sharing a letter means `--final` silently re-runs whichever
    comes first — and the run it writes carries the *other* one's label."""
    letters = [c.label.split()[0] for c in sweep.candidates()]
    assert len(letters) == len(set(letters)), letters
    assert all(len(letter) == 1 for letter in letters), letters


# This is a unit test.
def test_every_candidate_holds_the_embedder_and_both_models_fixed():
    """The sweep's claim is that each row changes one thing.

    The embedder decides whether anything is measurable at all on a Farsi
    corpus, and the two models decide what the numbers mean; a row that moved
    one of them would be incomparable to every other row while looking like a
    knob result."""
    for cfg in sweep.candidates():
        assert cfg.index.embedder == sweep.EMBEDDER, cfg.label
        assert cfg.index.embed_model == sweep.EMBED_MODEL, cfg.label
        assert cfg.generation.model == sweep.ANSWER_MODEL, cfg.label
        assert cfg.generation.ragas_model == sweep.JUDGE_MODEL, cfg.label
        assert cfg.generation.model != cfg.generation.ragas_model, (
            'a model grading its own answer is not evidence')


# This is a unit test.
def test_every_candidate_generates_an_answer_so_it_can_be_ranked():
    """All four deciding metrics need a response. A candidate that retrieved
    without answering would score `None`, drop to the bottom of the ranking as
    if it had lost, and cost a full run to say nothing."""
    assert all(c.generation.answerer == 'llm' for c in sweep.candidates())


# This is a unit test.
def test_every_candidate_validates_before_the_sweep_starts():
    """`run_eval` raises on an invalid config. Candidate H is the eighth row, so
    a typo there would surface after an hour of paid judging."""
    for cfg in sweep.candidates():
        assert cfg.validate() == [], cfg.label


# This is a unit test.
def test_no_two_candidates_are_the_same_configuration():
    """A duplicated row costs ten minutes and reads as reproducibility."""
    seen = {}
    for cfg in sweep.candidates():
        key = json.dumps(replace(cfg, label='').to_dict(), sort_keys=True)
        assert key not in seen, f'{cfg.label} duplicates {seen.get(key)}'
        seen[key] = cfg.label


# This is a unit test.
def test_the_final_run_refuses_to_start_without_a_judge(monkeypatch, tmp_path):
    """The final run is the one whose numbers go in the document.

    Without a key the LLM stages fall back to the offline fake, so it would
    produce a full leaderboard row of meaningless scores under the winner's
    name — the worst output of the two, because the sweep that chose it did
    refuse.

    `RUNS_DIR` is redirected as well, and that is not belt-and-braces: writing
    this test found that the unguarded `final()` had already dropped a
    112-question fake-provider run into the real `.runs/`, labelled `WINNER`,
    where it sat in the leaderboard beside the judged rows."""
    monkeypatch.setattr(sweep, 'load_lab_settings', lambda: LAB_SETTINGS)
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    with pytest.raises(SystemExit):
        sweep.final(None, 1, 'A')
    assert not list(tmp_path.iterdir()), 'refusing must happen before any run'


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


# This is a unit test.
def test_every_llm_stage_has_a_role_in_the_registry():
    assert {role.key for role in models.ROLES} == {
        'expand', 'rerank', 'grade', 'answer', 'judge', 'ragas'}


# This is a unit test.
def test_every_model_role_points_at_a_real_config_field():
    cfg = LabConfig()
    for role in models.ROLES:
        group, _, field = role.field.partition('.')
        assert field in getattr(cfg, group).__dataclass_fields__, role.key


# This is a unit test.
def test_every_model_in_the_catalogue_declares_where_its_weights_stand():
    entries = models.catalogue(LAB_SETTINGS)
    assert entries[0]['id'] == ''          # the lab default stays the first choice
    assert all(e['source'] in ('default', 'open', 'closed') for e in entries)
    assert any(e['source'] == 'closed' for e in entries)
    # Every open-weight option the remote list had answered 404 on this account
    # (2026-08-02), so 'open' lives on the local list now — still declared.
    assert all(option.source == 'open' for option in models.OLLAMA_MODELS)
    assert all(e['label'] for e in entries)


# This is a unit test.
def test_an_unverified_model_is_offered_as_unavailable_rather_than_dropped():
    """A model this lab has not actually run is still worth trying, so it stays
    in the list marked NA. Silently omitting it would hide the option."""
    entries = models.catalogue(LAB_SETTINGS)     # no API key: nothing to probe
    assert any(not e['available'] for e in entries)
    by_id = {e['id']: e for e in entries}
    assert by_id[LAB_SETTINGS.llm_model]['available']


# This is a unit test.
def test_the_configured_model_is_always_offered_even_if_it_is_not_in_the_registry():
    settings = replace(LAB_SETTINGS, llm_model='someone/custom-7b')
    entries = models.catalogue(settings)
    assert 'someone/custom-7b' in [e['id'] for e in entries]
    assert entries[0]['label'].endswith('someone/custom-7b)')


# This is a unit test.
def test_a_blank_role_falls_back_to_the_lab_default_model():
    settings = replace(LAB_SETTINGS, llm_model='lab/default')
    roles = models.resolve(LabConfig(), settings)
    assert roles.answer == 'lab/default' and roles.grade == 'lab/default'
    assert roles.ragas == 'lab/default' and roles.judge == 'lab/default'


# This is a unit test.
def test_each_role_round_trips_from_the_panels_json():
    cfg = LabConfig.from_dict({
        'retrieval': {'reranker_model': 'rerank/model', 'grader_model': 'grade/model',
                      'expansion_model': 'hyde/model'},
        'generation': {'model': 'answer/model', 'judge_model': 'judge/model',
                       'ragas_model': 'ragas/model'}})
    roles = models.resolve(cfg, LAB_SETTINGS)
    assert (roles.rerank, roles.grade, roles.expand, roles.answer,
            roles.judge, roles.ragas) == (
        'rerank/model', 'grade/model', 'hyde/model', 'answer/model',
        'judge/model', 'ragas/model')


# This is an integration test.
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


# This is an integration test.
def test_a_stage_with_no_model_choice_leaves_it_to_the_provider(index):
    """'' rather than a guess: the provider already knows its default model, and
    a lab that hard-codes one here would silently ignore RAGLAB_MODEL."""
    provider = Recorder()
    pipeline.retrieve(index, RetrievalConfig(k=3, rerank_depth=3, reranker='llm'),
                      'قول باشگاه', '2026-07-28', llm=provider)
    assert provider.calls == ['']


# This is a unit test.
def test_the_key_facts_judge_uses_the_judge_model():
    provider = Recorder(reply='1: yes')
    score = evaluate.judge_key_facts(provider, 'judge/model',
                                     {'key_facts': ['he went to the gym']}, 'رفتم')
    assert provider.calls == ['judge/model']
    assert score == pytest.approx(1.0)


# This is a unit test.
def test_every_configuration_factor_has_an_explainer():
    """An unexplained knob is a knob nobody can make a real decision about."""
    assert explain.missing() == []


# This is a unit test.
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

FARSI_MODELS = ('heydariAI/persian-embeddings',
                'intfloat/multilingual-e5-small',
                'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')


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


# This is a unit test.
def test_every_embedder_says_which_languages_it_covers():
    """A hint per option, covering the whole registry: an embedder the panel
    offers without saying what it can read is a run nobody can interpret."""
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert set(hints) == set(EMBEDDERS)
    assert all(h['languages'] and h['label'] and h['note'] for h in hints.values())


# This is a unit test.
def test_the_production_default_is_labelled_as_latin_only():
    """ascii-hash scores 0.014 on this corpus for one reason, and the dropdown
    has to say it out loud rather than leave it to be discovered by a run."""
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert hints['ascii-hash']['farsi'] is False
    assert 'latin' in hints['ascii-hash']['languages'].lower()
    for kind in ('token-hash', 'char-hash', 'fastembed'):
        assert hints[kind]['farsi'] is True, kind


# This is a unit test.
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


# This is a unit test.
def test_an_english_only_model_is_offered_but_says_so(monkeypatch):
    """The brain hardwires bge-small-en today. The lab must be able to measure
    that choice, and must never let it be picked by accident."""
    _fastembed_serving(monkeypatch, embedding.MODEL_IDS)
    by_id = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    english = by_id['BAAI/bge-small-en-v1.5']
    assert english['farsi'] is False
    assert 'english' in english['languages'].lower()
    assert english['available'] is True      # installable, just wrong for Farsi


# This is a unit test.
def test_a_model_this_fastembed_cannot_serve_reads_NA(monkeypatch):
    """NA now means one thing only: *this installation* cannot load it. An older
    fastembed serves a shorter list, and the panel has to say so rather than
    promise a wheel that is not there."""
    _fastembed_serving(
        monkeypatch,
        {'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'})
    entries = embedding.embed_model_catalogue(LAB_SETTINGS)
    by_id = {entry['id']: entry for entry in entries}
    assert by_id[
        'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'][
            'available'] is True
    assert by_id['BAAI/bge-small-en-v1.5']['available'] is False
    flags = [entry['available'] for entry in entries]
    assert flags == sorted(flags, reverse=True), 'usable models come first'


# This is a unit test.
def test_fastembed_models_are_NA_until_the_semantic_extra_is_installed(monkeypatch):
    """The mirror of the sentence-transformers case, and the reason the catalogue
    checks the import on top of the served list: with the extra missing, every
    fastembed model must read NA rather than promise a wheel that is not there.
    The served list is left generous on purpose — the import check alone decides."""
    monkeypatch.setattr(embedding, 'fastembed_models',
                        lambda: frozenset(embedding.MODEL_IDS))
    monkeypatch.setattr(embedding, 'fastembed_available', lambda: False)
    absent = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert absent['sentence-transformers/all-MiniLM-L6-v2']['available'] is False
    assert absent['BAAI/bge-small-en-v1.5']['available'] is False
    monkeypatch.setattr(embedding, 'fastembed_available', lambda: True)
    present = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert present['BAAI/bge-small-en-v1.5']['available'] is True


# This is a unit test.
def test_e5_models_carry_the_prefixes_they_were_trained_with():
    """E5 was trained with "query: " / "passage: ". Dropping the prefixes is a
    silent quality loss, so they belong to the model entry, not to a caller."""
    by_id = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    e5 = by_id['intfloat/multilingual-e5-small']
    assert (e5['query_prefix'], e5['passage_prefix']) == ('query: ', 'passage: ')
    plain = by_id['sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2']
    assert (plain['query_prefix'], plain['passage_prefix']) == ('', '')


# This is a unit test.
def test_a_prefixed_embedder_marks_queries_and_passages_apart():
    fake = FakeTextEmbedding()
    embedder = embedding.FastEmbedMultilingual(
        'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
        query_prefix='query: ', passage_prefix='passage: ',
        factory=lambda name: fake)
    embedder.embed(['دعوا با مهسا سر خونه'])
    embedder.embed_queries(['دعوا با مهسا'])
    assert 'passage: دعوا با مهسا سر خونه' in fake.seen
    assert 'query: دعوا با مهسا' in fake.seen


# This is a unit test.
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


# This is a unit test.
def test_a_symmetric_embedder_needs_no_query_method():
    """Every hash embedder embeds both sides the same way, and must keep
    working without knowing this distinction exists."""
    vectors = embedding.query_vectors(embedding.make_embedder('char-hash'),
                                      ['سلام'])
    assert vectors.shape[0] == 1 and np.any(vectors)


# This is an integration test.
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


# This is a unit test.
def test_the_embedding_model_names_the_collection_only_when_it_is_used():
    """Same rule as the summary model: a model nobody loads must not invalidate
    an index and cost a 157-session rebuild."""
    hashed = IndexConfig(embedder='char-hash')
    assert hashed.fingerprint() == \
        replace(hashed, embed_model='BAAI/bge-small-en-v1.5').fingerprint()
    real = IndexConfig(embedder='fastembed')
    assert real.fingerprint() != \
        replace(real, embed_model='BAAI/bge-small-en-v1.5').fingerprint()


# This is a unit test.
def test_the_chosen_embedding_model_is_the_one_that_gets_loaded(monkeypatch):
    seen: dict = {}

    def spy(model_name, **kwargs):
        seen.update({'model': model_name} | kwargs)
        return object()

    monkeypatch.setattr(embedding, 'SentenceTransformerEmbedder', spy)
    embedding.make_embedder('sentence-transformers', LAB_SETTINGS,
                            model='intfloat/multilingual-e5-small')
    assert seen['model'] == 'intfloat/multilingual-e5-small'
    assert seen['query_prefix'] == 'query: '
    assert seen['passage_prefix'] == 'passage: '


# This is a unit test.
def test_a_blank_embedding_model_keeps_following_the_lab_default(monkeypatch):
    """'' means RAGLAB_FASTEMBED_MODEL, exactly as '' means RAGLAB_MODEL for the
    chat roles — the lab never hard-codes a model of its own."""
    seen: dict = {}
    monkeypatch.setattr(embedding, 'FastEmbedMultilingual',
                        lambda model_name, **kw: seen.update(model=model_name))
    embedding.make_embedder('fastembed', LAB_SETTINGS)
    assert seen['model'] == LAB_SETTINGS.fastembed_model


# This is a unit test.
def test_the_index_builds_with_the_embedding_model_from_its_config(monkeypatch,
                                                                  diary):
    from .raglab import index as index_module
    seen: list[tuple] = []

    def spy(kind, settings=None, model=''):
        seen.append((kind, model))
        return embedding.CharHashEmbedder()   # anything that embeds, offline

    monkeypatch.setattr(index_module.embedding, 'make_embedder', spy)
    cfg = IndexConfig(chunker='session', embedder='fastembed',
                      embed_model='BAAI/bge-small-en-v1.5')
    LabIndex.build(cfg, {'sessions': diary['sessions'][:2], 'threads': {}},
                   LAB_SETTINGS)
    assert seen == [('fastembed', 'BAAI/bge-small-en-v1.5')]


# This is a unit test.
def test_the_language_note_names_the_model_that_was_actually_used():
    note = embedding.language_note('fastembed', 'BAAI/bge-small-en-v1.5')
    assert 'bge-small-en' in note and 'english' in note.lower()
    assert 'ascii-hash' in embedding.language_note('ascii-hash', '')


# This is an integration test.
def test_a_run_records_which_languages_its_embedder_can_represent(
        registry, ground_truth, tmp_path, monkeypatch):
    """A leaderboard row whose embedder could not read the corpus is not a
    result, and three days later nothing on the row says so."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    cfg = LabConfig(index=IndexConfig(chunker='fixed', embedder='ascii-hash',
                                      contextual=False),
                    retrieval=RetrievalConfig(k=4),
                    generation=GenerationConfig(answerer='none'))
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=2, ragas_mode='off')
    notes = ' '.join(result.notes).lower()
    assert 'ascii-hash' in notes and 'latin' in notes


# This is a unit test.
def test_the_embedding_model_knob_explains_itself():
    topics = explain.topics()
    assert 'farsi' in topics['index.embed_model'].lower()
    assert 'farsi' in topics['index.embedder'].lower()


# --- the catalogue offers only what has run on this machine -----------------
#
# The rule used to be that a model nobody had measured stayed listed as NA —
# "worth trying, nobody scored it yet". Checked against the wire on 2026-08-02
# that had rotted into something else: six of the ten remote chat models answered
# 404, and the embedder list offered a 16 GB download whose weights had been
# half-fetched and abandoned twice. NA had stopped meaning "unmeasured" and
# started meaning "broken", which is the one thing a dropdown must not hide. What
# is listed now is what answered here.
#
# The local list keeps the old rule, because there NA is honest: a tag that is
# merely not pulled yet is one `ollama pull` away, and the daemon is asked
# directly rather than guessed at.

REACHABLE_CHAT = ('openai/gpt-5-nano', 'openai/gpt-5-mini',
                  'anthropic/claude-haiku-4.5', 'google/gemini-2.5-flash')

# All six answered 404 "No endpoints available matching your guardrail
# restrictions and data policy" on this account, measured 2026-08-02. They are
# every open-weight option the remote list had.
UNREACHABLE_CHAT = ('openai/gpt-5', 'meta-llama/llama-3.3-70b-instruct',
                    'qwen/qwen-2.5-72b-instruct', 'google/gemma-3-27b-it',
                    'mistralai/mistral-nemo', 'deepseek/deepseek-chat')

# Each of these embedded a Farsi sentence here on 2026-08-02, through the backend
# it names. Dropped with them: Qwen3-Embedding-8B (three of four shards were
# incomplete downloads), BAAI/bge-m3 (this fastembed serves neither it nor
# e5-small), both OpenAI models with their backend, and e5-large / mpnet-base-v2 /
# jina-embeddings-v3, which nothing here has ever loaded.
VERIFIED_EMBED = {
    'heydariAI/persian-embeddings': 'sentence-transformers',
    'intfloat/multilingual-e5-small': 'sentence-transformers',
    'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2': 'fastembed',
    'BAAI/bge-small-en-v1.5': 'fastembed',
    'sentence-transformers/all-MiniLM-L6-v2': 'fastembed',
}


# This is a unit test.
def test_the_remote_catalogue_offers_only_models_this_account_can_reach():
    ids = {option.id for option in models.CHAT_MODELS}
    assert ids == set(REACHABLE_CHAT)
    assert not ids & set(UNREACHABLE_CHAT)
    # And the local list is deliberately untouched: llama3.1:8b is not installed,
    # reads NA, and stays, because pulling it is a one-line fix by the user.
    assert 'llama3.1:8b' in {option.id for option in models.OLLAMA_MODELS}


# This is a unit test.
def test_the_embedding_catalogue_offers_only_models_that_loaded_here():
    assert {m.id: m.backend for m in embedding.EMBED_MODELS} == VERIFIED_EMBED


# This is a unit test.
def test_the_lab_has_no_openai_embedding_backend():
    """It went with its two models. A backend whose whole catalogue is gone is
    still selectable, and would have built an embedder with no model and dim 0 —
    worse than the API bill it was there to offer."""
    assert 'openai' not in EMBEDDERS
    assert 'openai' not in embedding.BACKENDS
    assert 'openai' not in embedding.BACKEND_DEFAULTS
    assert not hasattr(embedding, 'OpenAIEmbedder')
    assert 'openai' not in {hint['kind'] for hint in embedding.embedder_hints()}
    with pytest.raises(ValueError):
        embedding.make_embedder('openai', LAB_SETTINGS)
    # The key goes too, rather than sitting in the settings advertising a backend
    # that is not there.
    assert not hasattr(LabSettings(), 'openai_api_key')


# This is a unit test.
def test_e5_small_is_offered_through_the_backend_that_can_load_it():
    """Its weights were already on disk and unreachable anyway: the entry named
    fastembed, which does not serve it, so `validate()` refused the one backend
    that could. The prefixes come along — they belong to the model, not to the
    backend that happens to load it."""
    entry = {m.id: m for m in embedding.EMBED_MODELS}[
        'intfloat/multilingual-e5-small']
    assert entry.backend == 'sentence-transformers'
    assert (entry.query_prefix, entry.passage_prefix) == ('query: ', 'passage: ')
    assert LabConfig(index=IndexConfig(
        embedder='sentence-transformers',
        embed_model='intfloat/multilingual-e5-small')).validate() == []
    assert LabConfig(index=IndexConfig(
        embedder='fastembed',
        embed_model='intfloat/multilingual-e5-small')).validate()


# --- models fastembed cannot serve -----------------------------------------
#
# The Persian-tuned encoder is not in fastembed's list — it is a HuggingFace
# checkpoint — so each model names the backend that serves it and the lab grows a
# second one. Everything here runs offline: the local backend is exercised
# through an injected factory, because a test that needs a 2 GB download is a
# test nobody runs.

REQUESTED_MODELS = {
    'heydariAI/persian-embeddings': ('sentence-transformers', 1024, 'open'),
    'intfloat/multilingual-e5-small': ('sentence-transformers', 384, 'open'),
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


# This is a unit test.
def test_the_catalogue_offers_every_requested_model_with_its_backend():
    by_id = {model.id: model for model in embedding.EMBED_MODELS}
    for model_id, (backend, dim, source) in REQUESTED_MODELS.items():
        entry = by_id.get(model_id)
        assert entry is not None, model_id
        assert (entry.backend, entry.dim, entry.source) == (backend, dim, source)
        assert entry.farsi and entry.note, model_id


# This is a unit test.
def test_no_knob_offers_an_openrouter_embedding_or_rerank_model():
    """Availability is verified here, never guessed — the rule the embedder list
    already follows. Measured against OpenRouter's published catalogue on
    2026-07-31: 337 models, and not one embedding, rerank or whisper entry;
    `qwen/qwen3-embedding-8b` and `cohere/rerank-4-fast` are both absent. The
    gateway does answer 401 rather than 404 on /embeddings and /rerank, so the
    routes exist — but a route with no servable model is not a backend, and a key
    in the environment is not evidence that one is there.

    An unservable *reranker* is the worse half: `pipeline._rerank` swallows every
    exception and returns the pre-rerank order, so such a candidate would report
    itself as reranked while having done nothing — a silent accuracy difference,
    which is the exact failure this lab exists to catch."""
    assert 'openrouter' not in EMBEDDERS
    assert 'openrouter' not in embedding.BACKENDS
    assert 'openrouter' not in embedding.BACKEND_DEFAULTS
    assert 'rerank-4-fast' not in RERANKERS
    # And neither is reachable as a default, which is how they arrived.
    assert IndexConfig().embedder == 'sentence-transformers'
    assert RetrievalConfig().reranker == 'lexical'


# This is a unit test.
def test_every_model_names_a_backend_the_lab_actually_has():
    assert all(model.backend in embedding.BACKENDS
               for model in embedding.EMBED_MODELS)
    assert set(embedding.BACKENDS) <= set(EMBEDDERS)


# This is a unit test.
def test_the_persian_tuned_model_is_the_default():
    """The lab defaults to a Persian-tuned encoder — a Farsi corpus deserves one,
    and at ~2.2 GB it is the cheapest real encoder here. Qwen3 was listed above it
    as the recommended ceiling until 2026-08-02, when it turned out never to have
    loaded on this machine at all."""
    assert IndexConfig().embedder == 'sentence-transformers'
    assert IndexConfig().embed_model == ''      # '' = the backend's default
    assert embedding.BACKEND_DEFAULTS['sentence-transformers'] == \
        'heydariAI/persian-embeddings'
    assert embedding.resolve_model('sentence-transformers', LAB_SETTINGS, '') == \
        'heydariAI/persian-embeddings'
    by_id = {m.id: m for m in embedding.EMBED_MODELS}
    # Visible in the option itself, not only behind the explainer: the standing is
    # what you are looking for while the dropdown is open.
    assert by_id['heydariAI/persian-embeddings'].tag == 'lab default'
    # RAGLAB_FASTEMBED_MODEL still drives the fastembed backend, untouched.
    assert embedding.resolve_model('fastembed', LAB_SETTINGS, '') == \
        LabSettings().fastembed_model


# This is a unit test.
def test_the_persian_tuned_model_says_which_language_it_was_tuned_for():
    entry = {m.id: m for m in embedding.EMBED_MODELS}['heydariAI/persian-embeddings']
    assert 'persian' in entry.languages.lower() or 'farsi' in entry.languages.lower()


# This is a unit test.
def test_the_local_backend_is_offered_as_an_embedder_with_its_coverage():
    assert 'sentence-transformers' in EMBEDDERS
    hints = {hint['kind']: hint for hint in embedding.embedder_hints()}
    assert set(hints) == set(EMBEDDERS)
    for kind in ('sentence-transformers', 'fastembed'):
        assert hints[kind]['farsi'] is True
        assert hints[kind]['languages'] and hints[kind]['note']


# This is a unit test.
def test_a_local_model_is_offered_as_NA_until_its_library_is_installed(monkeypatch):
    monkeypatch.setattr(embedding, 'sentence_transformers_available', lambda: False)
    absent = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert absent['intfloat/multilingual-e5-small']['available'] is False
    assert absent['heydariAI/persian-embeddings']['available'] is False
    monkeypatch.setattr(embedding, 'sentence_transformers_available', lambda: True)
    present = {e['id']: e for e in embedding.embed_model_catalogue(LAB_SETTINGS)}
    assert present['intfloat/multilingual-e5-small']['available'] is True


# This is a unit test.
def test_the_local_backend_applies_the_prefixes_the_model_was_trained_with():
    """The same guarantee the fastembed side keeps, on the backend that now
    serves the E5 model: query and passage are marked apart, and getting that
    backwards is a silent accuracy loss."""
    fake = FakeSentenceTransformer('intfloat/multilingual-e5-small')
    embedder = embedding.SentenceTransformerEmbedder(
        'intfloat/multilingual-e5-small', query_prefix='query: ',
        passage_prefix='passage: ', factory=lambda name: fake)
    fake.seen.clear()                      # drop anything the probe encoded
    passages = embedder.embed(['امروز جلسه داشتم'])
    queries = embedder.embed_queries(['جلسه کی بود؟'])
    assert fake.seen == ['passage: امروز جلسه داشتم', 'query: جلسه کی بود؟']
    assert embedder.dim == fake.dim == passages.shape[1] == queries.shape[1]
    assert 'intfloat/multilingual-e5-small' in embedder.name


# This is a unit test.
def test_make_embedder_builds_the_local_backend(monkeypatch):
    monkeypatch.setattr(embedding, '_sentence_transformer',
                        lambda name: FakeSentenceTransformer(name))
    local = embedding.make_embedder('sentence-transformers', LAB_SETTINGS,
                                    'intfloat/multilingual-e5-small')
    assert 'intfloat/multilingual-e5-small' in local.name
    # Blank means "the default model for the backend you chose", the same rule as
    # '' meaning RAGLAB_FASTEMBED_MODEL for fastembed.
    default = embedding.make_embedder('sentence-transformers', LAB_SETTINGS, '')
    assert 'heydariAI/persian-embeddings' in default.name


# This is a unit test.
def test_the_chosen_model_survives_the_fingerprint_for_every_model_backend():
    """The model is part of what got stored, so it has to reach the collection
    name — for all three backends, not just the first one the lab had."""
    for kind in ('fastembed', 'sentence-transformers'):
        kept = IndexConfig(embedder=kind, embed_model='some/model').normalized()
        assert kept.embed_model == 'some/model', kind
    dropped = IndexConfig(embedder='char-hash', embed_model='some/model').normalized()
    assert dropped.embed_model == ''
    a = IndexConfig(embedder='sentence-transformers',
                    embed_model='heydariAI/persian-embeddings')
    b = IndexConfig(embedder='sentence-transformers',
                    embed_model='intfloat/multilingual-e5-small')
    assert a.fingerprint() != b.fingerprint()


# This is a unit test.
def test_a_model_from_the_wrong_backend_is_refused_before_the_run():
    """Picking a HuggingFace checkpoint while the embedder is fastembed used to
    mean "load the default instead" — a run labelled with one model that had
    measured another."""
    problems = LabConfig(index=IndexConfig(
        embedder='fastembed',
        embed_model='heydariAI/persian-embeddings')).validate()
    assert any('sentence-transformers' in problem for problem in problems)
    assert LabConfig(index=IndexConfig(
        embedder='sentence-transformers',
        embed_model='heydariAI/persian-embeddings')).validate() == []


# This is a unit test.
def test_the_embedder_explainer_says_how_to_reach_a_model_it_cannot_download():
    """Two backends is a choice nobody can make from the kind names alone."""
    text = explain.topics()['index.embedder'].lower()
    assert 'sentence-transformers' in text and 'fastembed' in text


# --- what each number on the dashboard actually means -----------------------
#
# Every score in the panel is a claim about quality, and a claim nobody can check
# is worse than no claim: "faithfulness 0.74" means nothing without knowing whose
# definition, which formula, and which library produced it. So each metric carries
# the same four facts, from the same registry, shown through the same `!` the knobs
# use — and a metric a run can report without an explainer fails a test.

# This is a unit test.
def test_every_reported_metric_has_a_definition():
    """The gate: `aggregate()` can report these keys, so the panel can show them,
    so every one of them has to be explainable."""
    defined = {measure.key for measure in metrics.MEASURES}
    reported = set(metrics.AGGREGATED) | {'headline'}
    assert reported <= defined, reported - defined
    for measure in metrics.MEASURES:
        assert measure.label and measure.short, measure.key
        assert measure.formula and measure.library and measure.help, measure.key


# This is a unit test.
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


# This is a unit test.
def test_every_metric_names_the_library_that_computes_it():
    by_key = {measure.key: measure for measure in metrics.MEASURES}
    assert 'metrics.recall_at_k' in by_key['recall'].library
    assert 'difflib' in by_key['quote_recall'].library
    assert 'difflib' in by_key['answer_similarity'].library
    # A deterministic metric must not claim to be a model, and vice versa.
    assert 'llm' not in by_key['recall'].library.lower()
    assert 'llm' in by_key['key_fact_coverage'].library.lower()


# This is a unit test.
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


# This is a unit test.
def test_the_ragas_definitions_cover_every_metric_ragas_can_report():
    from .raglab import ragas_eval
    defined = {measure.key for measure in ragas_eval.RAGAS_MEASURES}
    reported = set(ragas_eval.OFFLINE_METRICS) | set(ragas_eval.LLM_METRICS)
    assert reported <= defined, reported - defined


# This is a unit test.
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


# This is a unit test.
def test_a_judged_metric_says_which_model_judged_it():
    """A number produced by a model is a number with variance, and the reader has
    to know which model — the same reason every stage carries its own dropdown.

    The decision score is judged too, being a mean of four judged metrics: a
    composite must not launder its inputs' variance by being an average."""
    from .raglab import ragas_eval
    judged = set(ragas_eval.LLM_METRICS) | {'ragas_decision'}
    for measure in ragas_eval.RAGAS_MEASURES:
        if measure.key in judged:
            assert 'RAGAS judge' in measure.library, measure.key
        else:
            assert 'no model' in measure.library.lower(), measure.key


# This is a unit test.
def test_no_metric_ships_without_an_explainer():
    """The counterpart of explain.missing() for the knobs: a metric added to
    AGGREGATED or to the RAGAS list without a definition fails here."""
    assert explain.missing_metrics() == []


# This is a unit test.
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

# This is a unit test.
def test_the_pipeline_steps_are_named_once_in_pipeline_order():
    assert [step.key for step in config.STEPS] == ['index', 'retrieval',
                                                   'generation']
    # Two names on purpose: the long one titles a panel, the short one tags a
    # group of models inside another panel, where a whole sentence would not fit.
    assert all(step.label and step.short and step.note for step in config.STEPS)
    assert [step.short for step in config.STEPS] == ['Index', 'Retrieval',
                                                     'Generation']


# This is a unit test.
def test_the_steps_are_exactly_the_config_groups():
    """A step is a config group with a colour, so the two lists cannot drift:
    a fourth group would otherwise render in a panel nobody colours."""
    assert {step.key for step in config.STEPS} == {group for group, _
                                                   in explain.GROUPS}


# This is a unit test.
def test_every_model_role_says_which_step_it_serves():
    steps = {step.key for step in config.STEPS}
    assert all(role.step in steps for role in models.ROLES)
    # The colour cannot disagree with where the value is stored: the step is the
    # group of the field the dropdown writes to.
    assert all(role.step == role.field.split('.')[0] for role in models.ROLES)


# This is a unit test.
def test_every_step_owns_at_least_one_model():
    """Each colour has to mean something in the models panel — a step with no
    model in it is a legend entry pointing at nothing. The index step owns the
    *embedder*: not a chat role, but a model all the same, and it wears the index
    ink in the same right-hand column."""
    served = {role.step for role in models.ROLES} | {'index'}
    assert served == {step.key for step in config.STEPS}


# This is a unit test.
def test_a_model_role_is_serialised_with_its_step():
    role = next(r for r in models.ROLES if r.key == 'grade')
    assert role.as_dict()['step'] == 'retrieval'


# --- exporting a run for reading ------------------------------------------
# A leaderboard says which architecture won. It cannot show what the pipeline
# did to any individual question, which is what you need to argue about whether
# the win is real. The export writes that out from a finished run, and only from
# what the run stored — inventing the missing parts is the one thing it must not
# do.

RUN_FIXTURE = {
    'run_id': '20260101-010101-abc123', 'label': 'D wider context k=12',
    'seconds': 671.68, 'started_at': '2026-01-01 01:01:01',
    'config': {'index': {'chunker': 'semantic-drift', 'embedder': 'x',
                         'embed_model': 'y', 'layers': ['chunk', 'habit']},
               'retrieval': {'k': 12, 'retriever': 'hybrid-rrf',
                             'reranker': 'lexical'},
               'generation': {'answerer': 'llm', 'model': 'm'}},
    'index': {'chunks': 732, 'by_layer': {'chunk': 700, 'habit': 5},
              'embed_dim': 1024},
    'summary': {'n_questions': 3, 'overall': {}, 'by_type': {}},
    'ragas': {'mode': 'llm', 'metrics': {'faithfulness': 0.77},
              'decision': 0.6501, 'n_samples': 3,
              'decision_spread': {'n': 3, 'mean': 0.65, 'stderr': 0.04}},
    'rows': [
        {'id': 'q-hb-001', 'type': 'habit', 'difficulty': 'medium',
         'answerable': True, 'abstained': False, 'hit': 1.0, 'recall': 1.0,
         'quote_recall': 1.0, 'ndcg': 0.63, 'precision': 0.2, 'mrr': 0.5,
         'n_contexts': 8, 'context_chars': 4689, 'latency_ms': 21140.4,
         'layers': ['chunk', 'habit'], 'time_scope': None,
         'retrieved_sessions': ['2026-05-16-a', '2025-11-08-a'],
         'answer': 'هفته‌ای سه بار.', 'answer_similarity': 0.31,
         'answer_token_f1': 0.36},
        {'id': 'q-ab-001', 'type': 'abstention', 'difficulty': 'hard',
         'answerable': False, 'abstained': True, 'hit': None, 'recall': None,
         'quote_recall': None, 'n_contexts': 8, 'context_chars': 100,
         'latency_ms': 900.0, 'layers': ['chunk'], 'time_scope': None,
         'retrieved_sessions': [], 'answer': 'پیدا نکردم.'},
        {'id': 'q-sh-001', 'type': 'single-hop', 'difficulty': 'easy',
         'answerable': True, 'abstained': False, 'hit': 0.0, 'recall': 0.0,
         'quote_recall': 0.0, 'n_contexts': 8, 'context_chars': 300,
         'latency_ms': 800.0, 'layers': ['chunk'], 'time_scope': 'تیر',
         'retrieved_sessions': ['2025-01-01-a'], 'answer': 'نمی‌دانم.'},
    ],
}


# This is a unit test.
def test_the_difficulty_table_counts_answers_not_just_retrieval():
    """"What share of the hard questions came out right" needs a definition, and
    the only per-question one this data supports is evidence-based: the run files
    store no judged grade per question, so a "correct" column implying a verdict
    would be an invention.

    An answerable question counts when the pipeline did not refuse *and* reached
    a gold session; an unanswerable one counts when it did refuse. Refusing a
    question that had an answer and answering one that did not are the two
    failures that matter, and both are visible here."""
    from .raglab import export
    table = export.difficulty_rates(RUN_FIXTURE['rows'])
    assert [row['difficulty'] for row in table] == ['easy', 'medium', 'hard']
    easy, medium, hard = table
    assert easy['n'] == 1 and easy['answered'] == 0.0     # retrieved nothing gold
    assert medium['n'] == 1 and medium['answered'] == 1.0
    assert hard['n'] == 1 and hard['answered'] == 1.0     # correctly refused
    # The share is a share of that difficulty, so it needs the count beside it:
    # one hard question at 100% is not a finding.
    assert all('n' in row for row in table)


# This is a unit test.
def test_the_difficulty_table_reports_evidence_separately_from_answers():
    """Retrieval reaching the evidence and the answer using it are different
    failures, and collapsing them hides which half to fix."""
    from .raglab import export
    rows = export.difficulty_rates(RUN_FIXTURE['rows'])
    easy = rows[0]
    assert easy['evidence_found'] == 0.0
    assert easy['quotes_in_context'] == 0.0
    # An unanswerable question has no evidence to find, so it must not be
    # averaged in as a miss.
    assert rows[2]['evidence_found'] is None


# This is a unit test.
def test_a_question_page_shows_reference_retrieval_response_and_grades(ground_truth):
    """The four things you need to judge one question, in one file."""
    from .raglab import export
    question = next(q for q in ground_truth['questions'] if q['id'] == 'q-sh-001')
    row = next(r for r in RUN_FIXTURE['rows'] if r['id'] == 'q-sh-001')
    page = export.question_page(RUN_FIXTURE, question, row)
    assert question['question_fa'] in page
    assert question['question_en'] in page
    assert question['answer_fa'] in page                  # the reference
    assert question['evidence'][0]['quote'] in page       # and its quote
    assert question['evidence'][0]['session_id'] in page
    assert row['answer'] in page                          # what it replied
    assert '2025-01-01-a' in page                         # what it retrieved
    # Every grade names its own arithmetic, the same rule the dashboard follows.
    assert '|gold ∩ top-k| / |gold|' in page
    assert 'Recall@k' in page
    # And the run it came from, or the page cannot be traced back.
    assert RUN_FIXTURE['run_id'] in page and RUN_FIXTURE['label'] in page


# This is a unit test.
def test_a_question_page_says_which_grades_are_not_per_question(ground_truth):
    """The four deciding metrics are stored as run means only.

    Printing them on a question page unlabelled would read as that question's
    faithfulness, which is the most misleading thing this export could do."""
    from .raglab import export
    question = next(q for q in ground_truth['questions'] if q['id'] == 'q-sh-001')
    row = next(r for r in RUN_FIXTURE['rows'] if r['id'] == 'q-sh-001')
    page = export.question_page(RUN_FIXTURE, question, row)
    assert 'run mean' in page.lower()
    assert 'not per question' in page.lower()


# This is a unit test.
def test_the_export_writes_one_file_per_question_plus_an_index(ground_truth,
                                                              tmp_path):
    from .raglab import export
    written = export.write_run(RUN_FIXTURE, ground_truth, tmp_path)
    names = sorted(path.name for path in written)
    assert names == ['README.md', 'q-ab-001.md', 'q-hb-001.md', 'q-sh-001.md']
    index = (tmp_path / 'README.md').read_text(encoding='utf-8')
    assert 'easy' in index and 'medium' in index and 'hard' in index
    assert RUN_FIXTURE['run_id'] in index
    # The index links the files, or a folder of 24 pages is unnavigable.
    assert '(q-sh-001.md)' in index


# This is a unit test.
def test_the_export_never_invents_the_context_text(ground_truth, tmp_path):
    """Runs store the retrieved session ids, not the chunk text.

    So the page says what it has and names what it does not, rather than
    reconstructing chunks by re-running retrieval — which would document a
    different retrieval than the one that was graded."""
    from .raglab import export
    export.write_run(RUN_FIXTURE, ground_truth, tmp_path)
    page = (tmp_path / 'q-sh-001.md').read_text(encoding='utf-8')
    assert 'chunk text is not stored' in page.lower()


# --- the service -----------------------------------------------------------

@pytest.fixture(scope='module')
def client():
    from fastapi.testclient import TestClient

    from .raglab.server import create_app
    return TestClient(create_app())


def _finished(client, job_id: str, timeout: float = 30.0) -> dict:
    """Poll a job to its terminal state, the way both frontends do."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['state'] not in ('running', 'cancelling'):
            return job
        time.sleep(0.01)
    raise AssertionError(f'job {job_id} still running after {timeout}s')


def _ask(client, payload: dict) -> dict:
    """POST one question and wait for its job — the panel's ask flow."""
    res = client.post('/api/queries', json=payload)
    assert res.status_code == 202, res.text
    job = _finished(client, res.json()['job_id'])
    assert job['state'] == 'done', job.get('error')
    return job['result']


# This is an integration test.
def test_options_describes_the_corpus_and_capabilities(client):
    body = client.get('/api/options').json()
    # Pinned rather than derived: the corpus is the measuring instrument, so a
    # change to its size should have to be stated here on purpose.
    assert body['corpus']['sessions'] == 167
    assert body['corpus']['questions'] == 112
    assert 'semantic-drift' in body['chunkers']
    assert 'ragas' in body['capabilities']


# This is an integration test.
def test_options_advertises_no_vector_database(client):
    """The panel used to carry a `chroma <db> @ <url>` badge, which is now a
    claim about a service that is not involved. It is replaced by a positive
    statement rather than an absence, because the panel does have to say where an
    experiment's vectors live and where its one durable artifact lands."""
    caps = client.get('/api/options').json()['capabilities']
    assert [key for key in caps if 'chroma' in key] == []
    assert caps['storage'] == {'index': 'memory',
                               'runs': 'brain/tests/raglab/.runs'}


# This is an integration test.
def test_health_says_the_lab_depends_on_no_service(client):
    body = client.get('/api/health').json()
    assert body['ok'] and body['storage'] == 'memory'
    assert [key for key in body if 'chroma' in key or key == 'database'] == []


# This is an integration test.
def test_a_build_starts_without_any_service_running(client):
    """`/api/index` used to answer 503 unless a Chroma heartbeat came back. With
    the index in process memory there is nothing that can be down, so the job
    starts — and the gate that could refuse it is gone rather than passing."""
    from .raglab import server as lab_server

    assert not hasattr(lab_server, 'require_chroma')
    body = client.post('/api/indexes', json={
        'index': {'chunker': 'session', 'embedder': 'ascii-hash',
                  'layers': ['session']}}).json()
    assert body['job_id']


# This is an integration test.
def test_options_counts_the_habits_the_corpus_tracks(client):
    """The habit ledger is only as good as the habits behind it, so how many the
    fixture declares is part of describing the corpus."""
    corpus_facts = client.get('/api/options').json()['corpus']
    assert corpus_facts['habits'] == 5


# This is an integration test.
def test_options_names_habit_as_a_question_type(client):
    """The per-type breakdown is where habit retrieval either shows up or hides
    inside the aggregation bucket."""
    assert 'habit' in client.get('/api/options').json()['question_types']


# This is an integration test.
def test_options_explains_the_new_metadata_and_the_deciding_score(client):
    body = client.get('/api/options').json()
    assert body['help']['metric.ragas_decision']
    by_key = {measure['key']: measure for measure in body['metrics']}
    assert by_key['ragas_decision']['step'] == ''


# This is an integration test.
def test_panel_is_served(client):
    page = client.get('/')
    assert page.status_code == 200
    assert 'RAG Lab' in page.text


# This is an integration test.
def test_ad_hoc_query_returns_stages_and_contexts(client):
    body = _ask(client, {
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash',
                  'layers': ['chunk']},
        'retrieval': {'k': 4},
        'generation': {'answerer': 'extractive'}})
    assert body['contexts'] and body['answer']
    assert body['time_scope']['label'] == 'آذر'
    assert 'retrieve_ms' in body['timings']


# This is an integration test.
def test_query_rejects_an_unknown_strategy(client):
    res = client.post('/api/queries', json={'question': 'x',
                                          'index': {'chunker': 'nope'}})
    assert res.status_code == 400
    assert 'unknown chunker' in res.json()['detail']


# This is an integration test.
def test_questions_endpoint_hides_the_answers(client):
    body = client.get('/api/questions?limit=5').json()
    assert len(body['questions']) == 5
    assert 'answer_fa' not in body['questions'][0]
    assert body['questions'][0]['evidence_sessions']


# This is an integration test.
def test_options_offers_a_model_choice_for_every_llm_task(client):
    body = client.get('/api/options').json()
    roles = {role['key']: role for role in body['model_roles']}
    assert set(roles) == {'expand', 'rerank', 'grade', 'answer',
                          'judge', 'ragas'}
    assert all(role['help'] and role['label'] and role['field']
               for role in roles.values())
    ids = [m['id'] for m in body['models']]
    assert ids[0] == '' and '4skl/gemma4-e2b-mtp' in ids
    # 'open' is no longer guaranteed here: the remote list kept only what this
    # account can reach (all closed, measured 2026-08-02); open weights are the
    # local list's business.
    assert {m['source'] for m in body['models']} >= {'default', 'closed'}


# This is an integration test.
def test_options_explains_every_knob(client):
    body = client.get('/api/options').json()
    for key in ('index.chunker', 'retrieval.reranker',
                'retrieval.grade_threshold', 'generation.answerer', 'model.answer',
                'model.answer'):
        assert body['help'].get(key), key


# This is an integration test.
def test_defaults_carry_the_per_task_model_fields(client):
    """The panel merges saved settings over these, so a field missing here is a
    dropdown that renders as undefined on an old browser tab."""
    defaults = client.get('/api/options').json()['defaults']
    assert defaults['retrieval']['reranker_model'] == ''
    assert defaults['retrieval']['grader_model'] == ''
    assert defaults['retrieval']['expansion_model'] == ''
    assert defaults['generation']['judge_model'] == ''
    assert defaults['generation']['ragas_model'] == ''


# This is an integration test.
def test_a_per_task_model_is_accepted_by_the_query_endpoint(client):
    body = _ask(client, {
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash', 'layers': ['chunk']},
        'retrieval': {'k': 4,
                      'grader_model': 'anthropic/claude-haiku-4.5'},
        'generation': {'answerer': 'extractive',
                       'judge_model': 'openai/gpt-5-mini'}})
    assert body['contexts']


# This is a unit test.
def test_the_standalone_panel_offers_the_model_pickers_too():
    """The lab still runs without a board, and that panel must not be the one
    place where a model is hard-coded."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'model_roles' in html and 'rag-model' in html


# This is a unit test.
def test_the_standalone_panel_reads_only_fields_the_lab_still_produces():
    """Deleting the summary hierarchy left the panel reading two fields nobody
    sends any more. `context.layer` merely prints "undefined";
    `summary.layer_usage` is `Object.entries(undefined)`, which throws and takes
    the whole results screen with it. So the panel's reads are checked against
    what the lab returns rather than against a list of names someone has to
    remember to prune."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')

    served = set(metrics.aggregate([]))
    read = set(re.findall(r'result\.summary\.(\w+)', html))
    assert read <= served, (
        'the panel reads summary fields the lab no longer returns: '
        f'{sorted(read - served)}')

    # Scoped to the contexts loop on purpose: `c` names the corpus object
    # elsewhere in the same file, so an unscoped `c.` would match its fields too.
    loop = html.split('out.contexts.map')[1].split(".join('')")[0]
    sent = set(pipeline.Context(chunk_id='c1', text='t', session_id='s1',
                                date='2026-01-01', score=1.0).as_dict())
    read = set(re.findall(r'\bc\.(\w+)', loop))
    assert read <= sent, (
        'the panel reads context fields the lab no longer returns: '
        f'{sorted(read - sent)}')


# This is an integration test.
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


# This is an integration test.
def test_options_say_which_languages_each_embedder_covers(client):
    body = client.get('/api/options').json()
    hints = {hint['kind']: hint for hint in body['embedder_hints']}
    assert set(hints) == set(body['embedders'])
    assert all(hint['languages'] for hint in hints.values())
    assert hints['ascii-hash']['farsi'] is False


# This is an integration test.
def test_options_offer_farsi_capable_embedding_models(client):
    body = client.get('/api/options').json()
    assert body['embed_models'][0]['id'] == ''
    by_id = {entry['id']: entry for entry in body['embed_models']}
    assert by_id['intfloat/multilingual-e5-small']['farsi'] is True
    assert by_id['BAAI/bge-small-en-v1.5']['farsi'] is False
    assert body['defaults']['index']['embed_model'] == ''
    assert body['help']['index.embed_model']


# This is an integration test.
def test_an_embedding_model_is_accepted_by_the_query_endpoint(client):
    """The field has to survive the panel round trip even when the running
    embedder ignores it, or a stale tab breaks a query."""
    body = _ask(client, {
        'question': 'آذر چه خبر بود؟',
        'index': {'chunker': 'message', 'embedder': 'char-hash',
                  'embed_model': 'intfloat/multilingual-e5-small',
                  'layers': ['chunk']},
        'retrieval': {'k': 4},
        'generation': {'answerer': 'extractive'}})
    assert body['contexts']


# This is a unit test.
def test_the_standalone_panel_offers_the_embedding_models_too():
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'embed_models' in html and 'embedder_hints' in html


# This is an integration test.
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


# This is an integration test.
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
    assert by_key['rerank'] == 'retrieval'
    assert by_key['answer'] == 'generation'


# This is an integration test.
def test_options_offer_the_local_backend_and_its_models(client):
    body = client.get('/api/options').json()
    assert 'sentence-transformers' in set(body['embedders'])
    assert 'openai' not in set(body['embedders'])
    by_id = {entry['id']: entry for entry in body['embed_models']}
    for model_id, (backend, dim, _) in REQUESTED_MODELS.items():
        assert model_id in by_id, model_id
        assert by_id[model_id]['backend'] == backend
        assert by_id[model_id]['dim'] == dim
    # The panel reports what is installed, so a dropdown never promises a
    # download or an API call that cannot happen.
    caps = body['capabilities']
    assert isinstance(caps['sentence_transformers'], bool)
    assert 'openai_embeddings' not in caps


# This is a unit test.
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


# This is a unit test.
def test_the_standalone_panel_takes_its_metric_definitions_from_the_service():
    """No second list of score labels: the panel that runs without a board has to
    explain a metric the same way the board's page does, or the same number ends
    up with two names and one definition."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'OPTIONS.metrics' in html
    assert 'metric.${key}' in html or "metric.' + key" in html
    assert 'SCORE_CARDS' not in html, 'the hard-coded score list is back'


# This is a unit test.
def test_the_standalone_panel_says_which_backends_consult_the_model():
    """It said "fastembed only", which stopped being true the moment a second
    backend could load a model — and "or openai" stopped being true when that
    backend left with its catalogue."""
    import re

    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    label = re.search(r'<label>Embedding model.*?</label>', html, re.S)
    assert label, 'the standalone panel lost its embedding-model label'
    assert 'sentence-transformers' in label.group(0)
    assert 'fastembed' in label.group(0)
    assert 'openai' not in label.group(0)


# This is a unit test.
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


# This is a unit test.
def test_the_standalone_panel_ranks_the_leaderboard_by_the_deciding_score():
    """Two numbers on one row invite ranking by the wrong one, so the panel has
    to say which column chose the architecture."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'ragas_decision' in html
    # And by the score *with its error*: neither panel may show the mean alone,
    # because the candidates in a sweep sit inside each other's error bars.
    assert 'ragas_decision_stderr' in html


# --- the local backend: a model on this machine ----------------------------
# The four deciding metrics are judged, so an unkeyed lab could measure nothing
# at all — which is what made the expensive candidates (an LLM gate is k calls
# per question) unmeasurable when the credit ran out. Ollama closes that gap, and
# the requirement is that it closes it *honestly*: a run judged locally must be
# labelled locally, and a slug the daemon cannot load must stop the run rather
# than quietly become whatever the other backend serves.

OLLAMA_SETTINGS = replace(LAB_SETTINGS, llm_provider='ollama',
                          llm_model='gemma4:e2b')


# This is a unit test.
def test_the_lab_provider_resolves_to_a_real_backend_or_the_fake():
    """Local is the default; another backend is always an explicit choice."""
    assert LabSettings(openrouter_api_key='').provider == 'ollama'
    assert LabSettings(openrouter_api_key='sk-x').provider == 'ollama'
    assert LabSettings(openrouter_api_key='sk-x', llm_provider='openrouter').provider == 'openrouter'
    # A named provider is a commitment, and outranks whether a key happens to
    # exist: with 'ollama' set, a key in the environment must not divert the run
    # to a paid API.
    assert LabSettings(openrouter_api_key='sk-x',
                       llm_provider='ollama').provider == 'ollama'


# This is a unit test.
def test_an_unknown_lab_provider_raises_rather_than_falling_back():
    with pytest.raises(ValueError, match='RAGLAB_LLM'):
        LabSettings(llm_provider='ollamma')


# This is a unit test.
def test_llm_ready_asks_whether_a_real_model_is_reachable_not_whether_a_key_is():
    """The distinction the whole change rests on. The fake provider answers and
    grades every question without ever failing, so 'has a backend' and 'has a
    key' are different questions — and a local judge needs no key at all."""
    assert LabSettings(openrouter_api_key='').llm_ready
    assert LabSettings(openrouter_api_key='sk-x').llm_ready
    assert LabSettings(llm_provider='ollama').llm_ready
    assert not LabSettings(openrouter_api_key='sk-x', llm_provider='fake').llm_ready


# This is a unit test.
def test_the_lab_builds_its_local_model_through_the_production_seam():
    """CLAUDE.md's rule: the lab tracks production seams, so whatever wins here
    ports over unchanged. Adding 'ollama' to make_chat_model is what made it
    reachable in the lab, and there is no lab-side branch to drift from it."""
    from .raglab.llm import brain_settings
    built = brain_settings(replace(OLLAMA_SETTINGS,
                                   ollama_base_url='http://localhost:11434/v1'))
    assert built.llm_provider == 'ollama'
    assert built.ollama_base_url == 'http://localhost:11434/v1'
    assert built.model == 'gemma4:e2b'


# This is a unit test.
def test_the_ragas_judge_follows_the_provider_instead_of_hardcoding_openrouter():
    """The bug this replaces: ragas_eval named ChatOpenAI, the OpenRouter key and
    the OpenRouter base URL itself, so the judge was the one stage RAGLAB_LLM
    could not move. A local answerer with a remote judge is a paid run that looks
    free, and nothing on the row said which."""
    from .raglab.llm import judge_llm
    judge = judge_llm(replace(OLLAMA_SETTINGS, llm_model='gemma4:e2b'),
                      'qwen3.5:2b')
    assert str(judge.openai_api_base) == OLLAMA_SETTINGS.ollama_base_url
    # The judge slug reaches the wire because RAGAS binds the model at
    # construction and never forwards one per request.
    assert judge.model_name == 'qwen3.5:2b'


# This is a unit test.
def test_ragas_availability_accepts_a_local_judge_with_no_api_key():
    from .raglab import ragas_eval
    status = ragas_eval.availability(OLLAMA_SETTINGS)
    if status.installed:
        assert status.llm_ready, status.notes
    fake = ragas_eval.availability(LAB_SETTINGS)
    if fake.installed:
        assert not fake.llm_ready
        assert any('ollama' in note for note in fake.notes), (
            'the note has to name the way out, not just the missing key')


# This is a unit test.
def test_the_judge_is_pushed_far_less_hard_when_it_runs_locally():
    """The failure this exists to prevent, measured: RAGAS defaults to 16
    concurrent calls, one laptop model serves two or three, and the queued
    requests tripped the client timeout — a judged run came back with one of its
    four deciding metrics and three `TimeoutError`s.

    Concurrency and timeout cannot change *what* a judge scores, only whether the
    score arrives, which is why tuning them per backend is not a thumb on the
    scale."""
    from .raglab import ragas_eval
    local = ragas_eval.judge_load(OLLAMA_SETTINGS)
    remote = ragas_eval.judge_load(replace(LAB_SETTINGS,
                                           openrouter_api_key='sk-x',
                                           llm_provider='openrouter'))
    assert local['max_workers'] < remote['max_workers']
    assert local['timeout'] > remote['timeout']
    assert local['timeout'] >= 600, 'calls under load were measured at 80–92s'


# This is a unit test.
def test_a_run_records_which_backend_judged_it():
    """A decision score is comparable only within one judge, and the model slug
    alone does not say whether it ran locally or was paid for."""
    note = models.note_for(LabConfig(), OLLAMA_SETTINGS)
    assert 'ollama' in note
    assert 'fake' in models.note_for(LabConfig(), LAB_SETTINGS)


# This is a unit test.
def test_the_dropdown_offers_the_local_models_when_the_backend_is_local():
    """Two lists, not one: an OpenRouter slug is not something Ollama can load,
    and a local tag is not something OpenRouter serves. One merged dropdown would
    offer every user half a menu of choices that cannot work."""
    local = {e['id'] for e in models.catalogue(OLLAMA_SETTINGS)}
    remote = {e['id'] for e in models.catalogue(LAB_SETTINGS)}
    assert 'qwen3.5:2b' in local and 'gemma4:e2b' in local
    assert 'openai/gpt-5-nano' in remote
    assert 'qwen3.5:2b' not in remote
    # The lists are disjoint, which is the property that matters. Note it cannot
    # be checked by the shape of a slug: `4skl/gemma4-e2b-mtp` is a namespaced
    # Ollama tag and contains a '/' exactly like an OpenRouter one.
    assert not ({o.id for o in models.CHAT_MODELS}
                & {o.id for o in models.OLLAMA_MODELS})
    assert not (local - {''}) & {o.id for o in models.CHAT_MODELS}


# This is a unit test.
def test_every_local_model_says_what_it_is_for():
    """The catalogue rule applies to the local list too: the licence is part of
    the label and every option says why you would pick it. On a local backend
    that note is doing more work than usual — the models differ mostly in speed,
    and the judge is ~276 calls per run."""
    for option in models.OLLAMA_MODELS:
        assert option.source == 'open', option.id
        assert option.note, option.id
        assert option.label


# This is a unit test.
def test_a_model_the_local_backend_does_not_serve_stops_the_run(monkeypatch):
    """The embedder rule applied to chat models: a mismatch is a validation
    error, never a silent fallback. A leaderboard row labelled qwen3.5:2b that
    was actually scored by gpt-5-mini is the worst artefact this lab can produce,
    because no other field on the row contradicts it."""
    monkeypatch.setattr(models, 'served_ids',
                        lambda settings: frozenset({'gemma4:e2b'}))
    cfg = LabConfig(generation=GenerationConfig(ragas_model='qwen3.5:2b'))
    problems = models.provider_problems(cfg, OLLAMA_SETTINGS)
    assert problems and 'qwen3.5:2b' in problems[0]
    assert models.provider_problems(
        LabConfig(generation=GenerationConfig(ragas_model='gemma4:e2b')),
        OLLAMA_SETTINGS) == []


# This is a unit test.
def test_an_unreachable_daemon_claims_nothing_rather_than_refusing_everything(
        monkeypatch):
    """"Cannot check" and "not there" are different facts. With the daemon down
    the served list is empty, and a guard that read that as "serves nothing"
    would refuse every run on a machine that was merely idle."""
    monkeypatch.setattr(models, 'served_ids', lambda settings: frozenset())
    cfg = LabConfig(generation=GenerationConfig(ragas_model='anything:1b'))
    assert models.provider_problems(cfg, OLLAMA_SETTINGS) == []


# This is a unit test.
def test_the_local_tag_list_is_read_from_the_daemon_not_guessed(monkeypatch):
    """Availability is verified, never inferred from the shape of a slug."""
    calls = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'models': [{'name': 'qwen3.5:2b'},
                               {'name': 'gemma4:e2b:latest'}]}

    def fake_get(url, timeout=None):
        calls['url'] = url
        return Response()

    import httpx
    monkeypatch.setattr(httpx, 'get', fake_get)
    monkeypatch.setattr(models, '_LIVE', {})
    ids = models.ollama_ids(OLLAMA_SETTINGS)
    # /api/tags, not the OpenAI-compatible /v1/models: a tag is what `ollama
    # pull` names and what a run should be labelled with.
    assert calls['url'] == 'http://localhost:11434/api/tags'
    assert 'qwen3.5:2b' in ids
    # Both spellings, because `ollama run gemma4:e2b` works even though the
    # daemon prints the ':latest' form.
    assert 'gemma4:e2b' in ids and 'gemma4:e2b:latest' in ids


# This is a unit test.
def test_the_sweep_can_be_pointed_at_a_local_pairing():
    """The sweep's two model pins are env-settable so a local run needs no edit
    to the file — but the rule they exist to enforce is unchanged: two different
    models, because a model grading its own output is not evidence."""
    assert sweep.ANSWER_MODEL != sweep.JUDGE_MODEL
    for cfg in sweep.candidates():
        assert cfg.generation.model == sweep.ANSWER_MODEL, cfg.label
        assert cfg.generation.ragas_model == sweep.JUDGE_MODEL, cfg.label


# This is a unit test.
def test_each_provider_names_its_own_pairing_and_never_crosses_them():
    """A slug only means something to the backend that serves it, so the default
    pairing is per provider. Crossing them is the failure `provider_problems`
    exists to stop, and a default must not be the thing that trips it."""
    for provider, pair in sweep.PAIRINGS.items():
        assert pair['answerer'] != pair['judge'], provider
        served = (models.OLLAMA_MODELS if provider == 'ollama'
                  else models.CHAT_MODELS)
        slugs = {m.id for m in served}
        assert pair['answerer'] in slugs, (provider, pair)
        assert pair['judge'] in slugs, (provider, pair)


# This is a unit test.
def test_choosing_the_local_backend_is_enough_to_get_a_local_default_model():
    """`RAGLAB_LLM=ollama` on its own has to produce a runnable lab. It did not:
    the default model stayed a remote slug, so `provider_problems` refused every
    run for a model the user never chose — a default that cannot run is a broken
    default, not a strict one."""
    local = LabSettings(llm_provider='ollama')
    assert local.llm_model in {m.id for m in models.OLLAMA_MODELS}, local.llm_model
    assert not models.provider_problems(LabConfig(), local)


# This is a unit test.
def test_an_explicit_model_is_never_replaced_by_the_provider_default():
    """The resolution is for the *unset* case only. Overwriting a stated model
    would mean a run labelled with one model was scored by another."""
    local = LabSettings(llm_provider='ollama', llm_model='gemma4:e2b')
    assert local.llm_model == 'gemma4:e2b'


# This is a unit test.
def test_every_provider_has_a_default_model_it_can_actually_serve():
    for provider, model in config.PROVIDER_MODELS.items():
        served = (models.OLLAMA_MODELS if provider == 'ollama'
                  else models.CHAT_MODELS)
        assert model in {m.id for m in served}, (provider, model)


# This is a unit test.
def test_the_local_pairing_is_the_one_that_was_screened():
    """The judge is part of the apparatus, so the default judge has to be a model
    `.screens/` has a row for — a default nobody screened is judge-shopping with
    extra steps."""
    assert sweep.PAIRINGS['ollama'] == {'answerer': '4skl/gemma4-e2b-mtp',
                                        'judge': 'gemma4:e2b'}


# This is a unit test.
def test_the_sweep_refuses_a_judge_that_grades_its_own_answers(monkeypatch,
                                                              tmp_path):
    monkeypatch.setattr(sweep, 'ANSWER_MODEL', 'gemma4:e2b')
    monkeypatch.setattr(sweep, 'JUDGE_MODEL', 'gemma4:e2b')
    monkeypatch.setattr(sweep, 'load_lab_settings', lambda: OLLAMA_SETTINGS)
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    with pytest.raises(SystemExit, match='own output'):
        sweep.judged_settings()


# This is a unit test.
def test_the_sweep_starts_with_a_local_judge_and_no_api_key(monkeypatch):
    """The guard used to test for a credential, so anyone judging locally was
    sent away from a run they could have made."""
    monkeypatch.setattr(sweep, 'load_lab_settings', lambda: OLLAMA_SETTINGS)
    assert sweep.judged_settings().provider == 'ollama'


# --- the 49-question sample, balanced across difficulty --------------------
# The four deciding metrics are means over questions, so which questions a run
# scored is part of the measurement. The natural distribution is 29 easy / 57
# medium / 26 hard, and a plain stride hands medium about half of any sample —
# which measures the medium pipeline and reports it as the pipeline.

# This is a unit test.
def test_a_balanced_sample_splits_the_difficulty_bands_as_evenly_as_it_can(
        ground_truth):
    picked = evaluate.select_questions(ground_truth, limit=49,
                                       balance='difficulty')
    assert len(picked) == 49
    counts = {name: sum(1 for q in picked if q['difficulty'] == name)
              for name in config.DIFFICULTIES}
    # 49 does not divide by three; the remainder goes to the earlier bands in
    # DIFFICULTIES order, so the split is 17/16/16 and not "whatever came out".
    assert counts == {'easy': 17, 'medium': 16, 'hard': 16}, counts


# This is a unit test.
def test_a_balanced_sample_that_divides_evenly_is_exactly_equal(ground_truth):
    picked = evaluate.select_questions(ground_truth, limit=51,
                                       balance='difficulty')
    counts = {name: sum(1 for q in picked if q['difficulty'] == name)
              for name in config.DIFFICULTIES}
    assert counts == {'easy': 17, 'medium': 17, 'hard': 17}, counts


# This is a unit test.
def test_a_balanced_sample_is_the_same_questions_every_time(ground_truth):
    """Two candidates are only comparable if they scored the same questions, so
    the selection has to be deterministic rather than merely proportionate."""
    first = evaluate.select_questions(ground_truth, limit=49,
                                      balance='difficulty')
    second = evaluate.select_questions(ground_truth, limit=49,
                                       balance='difficulty')
    assert [q['id'] for q in first] == [q['id'] for q in second]


# This is a unit test.
def test_a_balanced_sample_still_spreads_across_the_question_types(ground_truth):
    """Balancing difficulty must not cost type coverage — habit questions are
    last in the file and were the type a bad stride used to lose entirely."""
    picked = evaluate.select_questions(ground_truth, limit=49,
                                       balance='difficulty')
    types = {q['type'] for q in picked}
    assert len(types) >= 9, types
    assert 'habit' in types


# This is a unit test.
def test_a_balanced_sample_keeps_the_fixture_order(ground_truth):
    """Band-by-band output would make two runs undiffable line by line for no
    reason."""
    picked = evaluate.select_questions(ground_truth, limit=49,
                                       balance='difficulty')
    order = [q['id'] for q in ground_truth['questions']]
    assert [q['id'] for q in picked] == [i for i in order
                                        if i in {q['id'] for q in picked}]


# This is a unit test.
def test_a_band_too_small_for_its_share_does_not_shrink_the_sample():
    """A run asked for N questions must produce N whenever the set holds that
    many; what a small band cannot supply is offered to the others."""
    questions = ([{'id': f'e{i}', 'difficulty': 'easy', 'type': 't'}
                  for i in range(2)]
                 + [{'id': f'm{i}', 'difficulty': 'medium', 'type': 't'}
                    for i in range(20)]
                 + [{'id': f'h{i}', 'difficulty': 'hard', 'type': 't'}
                    for i in range(20)])
    picked = evaluate.select_questions({'questions': questions}, limit=12,
                                       balance='difficulty')
    assert len(picked) == 12
    assert sum(1 for q in picked if q['difficulty'] == 'easy') == 2


# This is a unit test.
def test_the_default_sampling_rule_is_unchanged(ground_truth):
    """The twelve runs already in `.runs/` were strided. Changing the default
    underneath the leaderboard would make those rows incomparable rather than
    merely old — so 'stride' stays the default and the sweep opts in."""
    strided = evaluate.select_questions(ground_truth, limit=24)
    explicit = evaluate.select_questions(ground_truth, limit=24,
                                         balance='stride')
    assert [q['id'] for q in strided] == [q['id'] for q in explicit]


# This is a unit test.
def test_an_unknown_balance_raises_rather_than_silently_striding(ground_truth):
    with pytest.raises(ValueError, match='balance'):
        evaluate.select_questions(ground_truth, limit=10, balance='difficlty')


# This is a unit test.
def test_an_unknown_balance_raises_even_when_there_is_no_limit(ground_truth):
    """Checked after the early return, the validation passed silently on any run
    without a limit — so a typo would only raise on the runs where it happened to
    change something, which is the worst possible place to find it."""
    with pytest.raises(ValueError, match='balance'):
        evaluate.select_questions(ground_truth, balance='difficlty')


# This is an integration test.
def test_a_run_saves_the_questions_it_was_measured_on(registry, ground_truth):
    """Neither the config nor the metric means say which questions produced them,
    so the ids travel with the row. Losing them is how two rows get compared
    across two different samples with nothing to reveal it."""
    cfg = LabConfig(index=IndexConfig(chunker='semantic-drift',
                                      embedder='char-hash', contextual=True),
                    generation=GenerationConfig(answerer='none'))
    result = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                               limit=9, balance='difficulty', ragas_mode='off')
    selection = result.selection
    assert selection['balance'] == 'difficulty' and selection['limit'] == 9
    assert selection['n'] == 9
    assert len(selection['question_ids']) == 9
    assert selection['by_difficulty'] == {'easy': 3, 'medium': 3, 'hard': 3}
    assert result.as_dict()['selection'] == selection
    # And on the leaderboard row — minus the ids, which would swamp it.
    assert result.brief()['selection']['balance'] == 'difficulty'
    assert 'question_ids' not in result.brief()['selection']


# This is a unit test.
def test_the_sweep_measures_every_candidate_on_the_same_balanced_30():
    """The sample is a property of the sweep, not of the invocation: a row
    measured on a different sample is a different measurement."""
    assert sweep.SWEEP_LIMIT == 30
    assert sweep.SWEEP_BALANCE == 'difficulty'
    assert sweep.SWEEP_BALANCE in config.BALANCES


# This is a unit test.
def test_the_sweep_sample_is_exactly_ten_of_each_band(ground_truth):
    """30 divides by three, so this sample needs no remainder rule at all — the
    bands are equal rather than merely as-equal-as-possible."""
    picked = evaluate.select_questions(ground_truth, limit=sweep.SWEEP_LIMIT,
                                       balance=sweep.SWEEP_BALANCE)
    counts = {name: sum(1 for q in picked if q['difficulty'] == name)
              for name in config.DIFFICULTIES}
    assert counts == {'easy': 10, 'medium': 10, 'hard': 10}, counts


# --- progress: a run that reports nothing is indistinguishable from a hang ---
# With a local judge the judged phase is hours, not minutes, so every phase has
# to say where it is. The callback carries a human detail beside the fraction
# because "0.92" for two hours tells the reader nothing about what is happening.

PROGRESS_CFG = LabConfig(index=IndexConfig(chunker='message', embedder='char-hash'),
                         generation=GenerationConfig(answerer='extractive'),
                         label='progress')


# This is an integration test.
def test_progress_reports_which_question_it_is_on(registry, ground_truth,
                                                  tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    seen = []
    evaluate.run_eval(registry, ground_truth, PROGRESS_CFG, LAB_SETTINGS,
                      limit=4, balance='difficulty', ragas_mode='off',
                      progress=lambda stage, fraction, detail='': seen.append(
                          (stage, round(fraction, 3), detail)))
    scoring = [row for row in seen if row[0] == 'scoring']
    assert len(scoring) == 4, seen
    # The count is the point: "question 3/4" is checkable against the sample the
    # row itself records, where a bare fraction is not.
    assert scoring[2][2].startswith('question 3/4'), scoring
    assert scoring[-1][2].startswith('question 4/4'), scoring
    # And the band, because a slow phase on hard questions is a different fact
    # from a slow phase overall.
    assert any(band in scoring[0][2] for band in config.DIFFICULTIES), scoring


# This is an integration test.
def test_a_two_argument_progress_callback_still_works(registry, ground_truth,
                                                      tmp_path, monkeypatch):
    """The detail is additive. The panel's reporter predates it, and a run must
    not fail because its caller does not want the third argument."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    seen = []
    evaluate.run_eval(registry, ground_truth, PROGRESS_CFG, LAB_SETTINGS,
                      limit=2, ragas_mode='off',
                      progress=lambda stage, fraction: seen.append(stage))
    assert 'scoring' in seen and 'done' in seen


# This is a unit test.
def test_the_judged_phase_reports_calls_as_they_land():
    """The judged phase is the whole wall clock on a local judge. RAGAS scores a
    batch, so without a per-call hook the bar sits at one number for hours."""
    watch = ragas_eval.JudgeWatch(total=6)
    seen = []
    watch.progress = lambda stage, fraction, detail='': seen.append(detail)
    watch.on_llm_end(None)
    watch.on_llm_end(None)
    assert 'judge call 2' in seen[-1]
    assert '~6' in seen[-1], 'the estimate is marked as one, not stated as fact'
    # A judge that makes more calls than estimated must not report >100%.
    for _ in range(20):
        watch.on_llm_end(None)
    assert watch.fraction() <= 1.0


# This is a unit test.
def test_the_job_carries_the_detail_to_whoever_is_polling():
    """The panel polls a job dict, so the detail has to be a field on it — a
    progress line only the terminal sees leaves the two UIs looking hung."""
    from .raglab.server import Jobs
    jobs = Jobs()
    captured = {}

    def target(report):
        report('scoring', 0.5, 'question 16/30 · hard')
        captured['snapshot'] = dict(jobs.jobs[jobs.current])
        return {'ok': True}

    job_id = jobs.start('run', target)
    while jobs.jobs[job_id]['state'] == 'running':
        time.sleep(0.01)
    assert captured['snapshot']['detail'] == 'question 16/30 · hard'
    assert captured['snapshot']['stage'] == 'scoring'
    # Present from the start, so a poll landing before the first report reads a
    # blank rather than undefined.
    assert 'detail' in jobs.jobs[job_id]


# This is a unit test.
def test_a_running_job_can_be_cancelled_before_its_next_call():
    """Stopping a run must prevent its next unit of work, not just its polling."""
    from .raglab.server import Jobs
    jobs = Jobs()
    started = threading.Event()

    def target(report, cancelled):
        started.set()
        while not cancelled():
            time.sleep(0.001)
        # The normal checkpoint used by indexing and evaluation raises the
        # cooperative cancellation exception as soon as the active call ends.
        report('scoring', 0.5, 'would have made another model call')

    job_id = jobs.start('run', target)
    assert started.wait(timeout=1)
    stopped = jobs.cancel(job_id)
    assert stopped['state'] == 'cancelling'
    for _ in range(100):
        if jobs.get(job_id)['state'] == 'cancelled':
            break
        time.sleep(0.01)
    job = jobs.get(job_id)
    assert job['state'] == 'cancelled'
    assert job['cancel_requested'] is True
    assert '_cancel' not in job


# This is a unit test.
def test_both_frontends_read_the_progress_detail():
    """Neither UI may quietly stop showing it: a judged local run spends hours in
    one stage, and the detail is the only thing that moves."""
    panel = (RAGLAB_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    board = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')
    assert 'job.detail' in panel
    assert '.job.detail' in board


# This is a unit test.
def test_both_rag_lab_frontends_offer_a_cooperative_stop():
    """The embedded panel is the usual surface, but the standalone lab needs it too."""
    panel = (RAGLAB_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    board = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')
    assert 'Stop experiment' in panel
    assert "'/api/jobs/' + jobId + '/cancel'" in panel
    assert 'raglab-cancel' in board
    assert "'/jobs/' + ragState.jobId + '/cancel'" in board


# This is a unit test.
def test_the_terminal_bar_says_stage_fraction_elapsed_and_detail():
    line = sweep.bar('Stage F', 'scoring', 0.5, 'question 16/30 · hard',
                     time.time() - 63)
    assert line.startswith('\r'), 'the line is rewritten in place, not appended'
    assert 'Stage F' in line
    assert '50.0%' in line
    assert '1m03s' in line, line          # elapsed, because a fraction alone
    assert 'question 16/30 · hard' in line   # cannot tell slow from stuck
    filled = line.count('█')
    assert filled == sweep.BAR_WIDTH // 2, filled


# This is a unit test.
def test_a_shorter_detail_cannot_leave_the_tail_of_a_longer_one_behind():
    """Without the padding a redraw leaves stale characters on the line, which
    reads as a stale *number* rather than as a drawing artefact."""
    written = []
    report = sweep.live('Stage A', time.time(),
                        stream=type('S', (), {'write': written.append,
                                              'flush': lambda self: None})())
    report('ragas', 0.94, 'judge call 137 of ~420')
    report('done', 1.0, '')
    assert len(written[0]) == len(written[1])
    assert '137' not in written[1]


# This is a unit test.
def test_the_expected_judge_call_count_scales_with_k():
    """Context precision asks one verdict per retrieved chunk, so k is what
    drives the bill — the estimate has to know that or it is decoration."""
    at_k5 = ragas_eval.expected_judge_calls(n_samples=10, k=5)
    at_k12 = ragas_eval.expected_judge_calls(n_samples=10, k=12)
    assert at_k12 > at_k5
    assert at_k12 - at_k5 == 10 * 7, (at_k5, at_k12)


# This is a unit test.
def test_the_balance_control_is_explained_like_every_other_knob():
    """`explain.missing()` covers config fields; a run-level control has to be
    added to the same registry by hand or it reaches the panel unexplained."""
    assert 'run.balance' in explain.topics()
    assert 'run.difficulty' in explain.topics()


# --- the leaderboard, and what it refuses to rank together ------------------
# A decision score is comparable only against rows that scored the same questions
# with the same judge. One flat ranking over everything in `.runs/` is exactly
# where that gets forgotten, so the producer groups first and ranks second.

def _row(run_id, label, decision, ids, judge, stderr=None):
    return {'run_id': run_id, 'label': label, 'ragas_decision': decision,
            'ragas_decision_stderr': stderr, 'started_at': '2026-07-31 10:00:00',
            'seconds': 60, 'n_questions': len(ids), 'summary': {}, 'ragas': {},
            'config': {}, 'judge': judge,
            'selection': {'balance': 'difficulty', 'n': len(ids),
                          'question_ids': ids}}


# This is a unit test.
def test_the_sweeps_own_ranking_applies_the_same_error_test():
    """Measured, and it is why this exists: F scored 0.7375 against A's 0.7222 on
    identical questions, and the sweep printed F on top of a list headed "ranked
    by the decision score" — a win, by presentation. The combined error was
    0.0477, three times the lead. The leaderboard refused to call it; the sweep
    that produced the rows must refuse too, or the first thing anyone reads is the
    conclusion the analysis rejects."""
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    ids = ['q1', 'q2']
    lines = sweep.ranking_verdict([
        _row('r2', 'F llm relevance gate', 0.7375, ids, judge, stderr=0.0333),
        _row('r1', 'A baseline', 0.7222, ids, judge, stderr=0.0341)])
    text = '\n'.join(lines)
    assert 'do not separate' in text or 'No winner' in text, text
    assert '0.0477' in text or '0.048' in text, 'the error it was judged against'


# This is a unit test.
def test_the_sweeps_ranking_names_a_winner_when_there_is_one():
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    ids = ['q1', 'q2']
    text = '\n'.join(sweep.ranking_verdict([
        _row('r2', 'F', 0.90, ids, judge, stderr=0.01),
        _row('r1', 'A', 0.50, ids, judge, stderr=0.01)]))
    assert 'F' in text and 'Winner' in text


# This is a unit test.
def test_rows_that_scored_different_questions_are_not_ranked_together():
    """The failure this exists to stop: F measured on 30 balanced questions read
    as beating A measured on 24 strided ones, when neither number bears on the
    other."""
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    groups = leaderboard.group([
        _row('r1', 'A', 0.61, ['q1', 'q2'], judge),
        _row('r2', 'F', 0.72, ['q3', 'q4'], judge),
    ])
    assert len(groups) == 2, 'two samples are two measurements'
    assert all(len(g.rows) == 1 for g in groups)


# This is a unit test.
def test_rows_scored_by_different_judges_are_not_ranked_together():
    ids = ['q1', 'q2']
    groups = leaderboard.group([
        _row('r1', 'A', 0.61, ids, {'model': 'gemma4:e2b', 'provider': 'ollama'}),
        _row('r2', 'F', 0.72, ids, {'model': 'openai/gpt-5-mini',
                                    'provider': 'openrouter'}),
    ])
    assert len(groups) == 2, 'a judge swap is a different measurement'


# This is a unit test.
def test_a_group_ranks_by_decision_score_and_keeps_the_unranked_rows_last():
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    ids = ['q1', 'q2']
    group, = leaderboard.group([
        _row('r1', 'A', 0.61, ids, judge),
        _row('r2', 'no judged metrics', None, ids, judge),
        _row('r3', 'F', 0.72, ids, judge),
    ])
    assert [r['label'] for r in group.rows] == ['F', 'A', 'no judged metrics']
    # Present, not dropped: a run that measured nothing is a fact about the run.
    assert group.rows[-1]['ragas_decision'] is None


# This is a unit test.
def test_a_lead_inside_the_error_is_reported_as_a_tie():
    """0.6487 against 0.6501 was a real pair in this lab, and a bare ranking read
    it as a win. The margin has to be compared to the error or the leaderboard
    manufactures conclusions."""
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    ids = ['q1', 'q2']
    group, = leaderboard.group([
        _row('r1', 'A', 0.6487, ids, judge, stderr=0.03),
        _row('r2', 'F', 0.6501, ids, judge, stderr=0.03),
    ])
    assert leaderboard.verdict(group) == 'tie'
    clear, = leaderboard.group([
        _row('r1', 'A', 0.50, ids, judge, stderr=0.01),
        _row('r2', 'F', 0.72, ids, judge, stderr=0.01),
    ])
    assert leaderboard.verdict(clear) == 'F'


# This is a unit test.
def test_runs_that_never_recorded_their_sample_are_not_treated_as_one_sample():
    """Found by running the thing: every run predating `RunResult.selection` has
    no question ids, so keying on the ids alone put 3-, 24- and 100-question runs
    in one ranked table. A missing sample is not evidence of a *shared* sample."""
    judge = {'model': 'openai/gpt-5-mini', 'provider': 'openrouter'}
    rows = [_row('r1', 'A 24q', 0.6385, [], judge),
            _row('r2', 'A 3q', 0.5488, [], judge)]
    rows[0]['n_questions'] = 24
    rows[1]['n_questions'] = 3
    for r in rows:
        r['selection'] = {}
    assert len(leaderboard.group(rows)) == 2, 'different counts, different samples'


# This is a unit test.
def test_an_unrecorded_sample_can_never_declare_a_winner():
    """Two runs of 24 questions apiece may still be two *different* 24 — striding
    changed, and nothing on those rows says which questions they were. So even
    with errors measured, the comparison is not established."""
    judge = {'model': 'openai/gpt-5-mini', 'provider': 'openrouter'}
    rows = [_row('r1', 'D', 0.6501, [], judge, stderr=0.01),
            _row('r2', 'A', 0.5000, [], judge, stderr=0.01)]
    for r in rows:
        r['selection'] = {}
        r['n_questions'] = 24
    found, = leaderboard.group(rows)
    assert leaderboard.verdict(found) == 'unknown'
    text = leaderboard.markdown([found])
    assert 'not recorded' in text
    # And no rank numbers, or the table contradicts the sentence above it.
    assert '| 1 |' not in text, text


# This is a unit test.
def test_a_group_with_no_measured_error_cannot_claim_a_winner():
    """`± 0` on the oldest rows would present them as the most precise."""
    judge = {'model': 'openai/gpt-5-mini', 'provider': 'openrouter'}
    ids = ['q1', 'q2']
    group, = leaderboard.group([
        _row('r1', 'A', 0.50, ids, judge, stderr=None),
        _row('r2', 'F', 0.72, ids, judge, stderr=None),
    ])
    assert leaderboard.verdict(group) == 'unknown'


# This is a unit test.
def test_the_group_that_decides_something_is_printed_first():
    """Sorting by question count put a 100-question group of unrecorded samples —
    which cannot be ranked at all — above the 30-question group that decides the
    architecture. A reader opens this for the live decision."""
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    stale = _row('r0', 'old 100q', 0.61, [], judge)
    stale['selection'], stale['n_questions'] = {}, 100
    stale['started_at'] = '2026-07-29 10:00:00'
    live_rows = [_row('r1', 'F', 0.90, ['q1', 'q2'], judge, stderr=0.01),
                 _row('r2', 'A', 0.50, ['q1', 'q2'], judge, stderr=0.01)]
    groups = leaderboard.group([stale] + live_rows)
    assert leaderboard.verdict(groups[0]) == 'F', [g.sample for g in groups]


# This is a unit test.
def test_every_judge_label_reads_as_a_noun_after_judged_by():
    judge = {'model': '', 'provider': ''}
    unjudged, = leaderboard.group([_row('r1', 'retrieval only', None,
                                        ['q1'], judge)])
    text = leaderboard.markdown([unjudged])
    assert 'judged by nothing judged' not in text, text
    assert 'judged by no judge —' in text


# This is a unit test.
def test_the_markdown_names_the_sample_and_the_judge_on_every_group():
    judge = {'model': 'gemma4:e2b', 'provider': 'ollama'}
    text = leaderboard.markdown(leaderboard.group([
        _row('r1', 'A baseline', 0.61, ['q1', 'q2'], judge, stderr=0.02)]))
    assert 'gemma4:e2b' in text and 'ollama' in text
    assert '2 questions' in text
    assert '0.610' in text and '0.020' in text
    assert 'r1' in text, 'the run id is what makes a row checkable'


# This is a unit test.
def test_the_run_list_carries_the_two_fields_comparability_needs(tmp_path,
                                                                 monkeypatch):
    """`brief()` had them and `list_runs` did not, so the panel's own leaderboard
    could not tell an incomparable row from a comparable one.

    Writes its own run file rather than reading `.runs/`: a test that skips when
    the developer's disk happens to be empty is not coverage."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    (tmp_path / '20260731-120000-abc123.json').write_text(json.dumps({
        'run_id': '20260731-120000-abc123', 'label': 'A baseline',
        'selection': {'balance': 'difficulty', 'n': 2,
                      'question_ids': ['q1', 'q2']},
        'summary': {'n_questions': 2},
        'ragas': {'metrics': {'faithfulness': 0.9}, 'decision': 0.61,
                  'decision_spread': {'stderr': 0.02},
                  'judge': {'model': 'gemma4:e2b', 'provider': 'ollama'}},
    }), encoding='utf-8')
    row, = evaluate.list_runs(limit=5)
    assert row['selection']['question_ids'] == ['q1', 'q2']
    assert row['judge'] == {'model': 'gemma4:e2b', 'provider': 'ollama'}
    # And the grouping keys off exactly those, so the two travel together.
    found, = leaderboard.group([row])
    assert found.question_ids == ('q1', 'q2')
    assert found.judge_model == 'gemma4:e2b'


# --- screening a judge before it is allowed to grade ------------------------
# Four of the metrics are judged, so the judge is part of the apparatus. A weak
# one does not produce noisy rankings — it produces confident wrong ones, and two
# of the local models screened so far answered identically to every claim, which
# scores 0.5 on a balanced set and separates no candidate from any other.

# This is a unit test.
def test_the_screen_pairs_a_verified_answer_with_one_fabricated_number(
        diary, ground_truth):
    """Built from the ground truth, not hand-authored: a supported claim is a
    question's own verified answer, and its partner is that answer with one
    numeral changed to one the context never states."""
    from .raglab import judgescreen
    sessions = corpus.sessions_by_id(diary)
    items = judgescreen.build_items(ground_truth, sessions, pairs=4)
    assert len(items) == 8
    yes = [i for i in items if i.supported]
    no = [i for i in items if not i.supported]
    assert len(yes) == len(no) == 4, 'an unbalanced screen flatters a constant judge'
    for supported, fabricated in zip(yes, no):
        assert supported.question_id == fabricated.question_id
        assert supported.claim != fabricated.claim
        # Word-for-word identical apart from digits: that is what removes the
        # lexical shortcut.
        strip = lambda text: ''.join(c for c in text if not c.isdigit()
                                     and c not in '۰۱۲۳۴۵۶۷۸۹')
        assert strip(supported.claim) == strip(fabricated.claim)


# This is a unit test.
def test_the_screen_measures_how_much_word_overlap_could_explain(diary,
                                                                ground_truth):
    """Reported, not assumed — and it is not zero, which is a deliberate trade.

    Two designs were tried and both leaked. Picking the most similar answer from a
    *different* question left the true answer ahead on overlap 0.43 to 0.20.
    Mutating one numeral made the classes lexically identical (0.533 vs 0.517) but
    the labels were **wrong**: the context was raw message text, which never states
    a date, so a true claim mentioning one was correctly refused by every judge.
    Dating the context fixed the labels and brought a modest signal back, because
    the correct numeral now appears in the context and the fabricated one does not.

    Correct labels win that trade every time: a mislabelled screen disqualifies
    good judges, which is worse than one a word-counter could partly game. So the
    number is measured and travels with the result, and the check that actually
    decides is degeneracy, which no lexical shortcut can pass."""
    from .raglab import judgescreen
    items = judgescreen.build_items(ground_truth, corpus.sessions_by_id(diary),
                                    pairs=6)
    signal = judgescreen.lexical_signal(items)
    assert signal['difference'] is not None
    assert 'blind' in signal
    # Small enough that overlap cannot be the whole story: the fabricated claims
    # still share almost all their vocabulary with the context.
    assert abs(signal['difference']) <= 0.15, signal


# This is a unit test.
def test_the_screen_dates_its_context_the_way_the_pipeline_does(diary,
                                                               ground_truth):
    """The defect that made the first screen's verdicts worthless.

    Diary messages are spoken and almost never state a date; the date is session
    metadata. Reference answers state dates. So a judge shown bare message text
    refuses a true claim *for the right reason* — which is exactly what happened:
    `gemma4:e2b` scored 0.2 and `qwen3.5:2b` 0.0 on the supported class, and
    reading their reasons showed the screen was wrong, not the models. The
    pipeline under test has the same problem and solves it the same way
    (`IndexConfig.contextual` prepends a dated header before embedding)."""
    from .raglab import judgescreen
    sessions = corpus.sessions_by_id(diary)
    items = judgescreen.build_items(ground_truth, sessions, pairs=6)
    for item in items:
        for line in item.context:
            assert re.match(r'^\[\d{4}-\d{2}-\d{2}\]', line), line


# This is a unit test.
def test_a_screened_claim_is_one_sentence_not_a_whole_answer(diary,
                                                            ground_truth):
    """The second defect. A reference answer is several clauses spanning several
    sessions, so a judge asked to entail all of it against one question's evidence
    is right to refuse. RAGAS's own faithfulness decomposes a response into atomic
    statements *before* judging any of them, so an undecomposed paragraph did not
    resemble the real task either."""
    from .raglab import judgescreen
    sessions = corpus.sessions_by_id(diary)
    items = judgescreen.build_items(ground_truth, sessions, pairs=6)
    answers = {q['id']: q['answer_fa'] for q in ground_truth['questions']}
    for item in items:
        # One sentence. Not "shorter than the answer": a single-sentence answer
        # legitimately yields a claim of the same length, and asserting length
        # would be testing the fixture's prose rather than the decomposition.
        assert len(textnorm.sentences(item.claim)) == 1, item.id
        assert len(item.claim) <= len(answers[item.question_id]), item.id
        # And it is anchored: it states a number the context also states, so the
        # context can actually settle it either way.
        anchored = [n for n in judgescreen.NUMERAL.findall(item.claim)
                    if textnorm.normalize(n)
                    in textnorm.normalize(' '.join(item.context))]
        assert anchored or not item.supported, item.id


# This is a unit test.
def test_the_fabricated_number_is_one_the_context_never_states(diary,
                                                              ground_truth):
    """Otherwise the claim is labelled unsupported while being arguably
    supported, and the screen would disqualify the judge that got it right."""
    from .raglab import judgescreen
    sessions = corpus.sessions_by_id(diary)
    items = judgescreen.build_items(ground_truth, sessions, pairs=6)
    for item in (i for i in items if not i.supported):
        context = textnorm.normalize(' '.join(item.context))
        original = next(i for i in items
                        if i.question_id == item.question_id and i.supported)
        changed = [n for n in judgescreen.NUMERAL.findall(item.claim)
                   if n not in judgescreen.NUMERAL.findall(original.claim)]
        assert changed, item.id
        for numeral in changed:
            assert textnorm.normalize(numeral) not in context, (item.id, numeral)


# This is a unit test.
def test_a_question_that_cannot_be_mutated_cleanly_is_skipped(diary):
    """No mutation is better than a mislabelled one."""
    from .raglab import judgescreen
    sessions = corpus.sessions_by_id(diary)
    # No numerals at all, so nothing can be fabricated.
    ground_truth = {'questions': [
        {'id': 'q-x', 'answerable': True, 'answer_fa': 'هیچ عددی اینجا نیست',
         'evidence': [{'quote': 'متن بدون عدد', 'session_id': 'nope',
                       'message_indices': []}]}]}
    assert judgescreen.build_items(ground_truth, sessions, pairs=4) == []


# This is a unit test.
def test_the_screen_reads_a_ragas_shaped_reply_and_nothing_looser():
    """RAGAS asks for nested JSON and retries on malformed output, so a model that
    judges well but writes prose spends its speed advantage on retries. Counting
    a bare 'yes' as an answer here would hide exactly that cost."""
    from .raglab import judgescreen
    good = '{"statements": [{"statement": "x", "verdict": 1, "reason": "y"}]}'
    assert judgescreen._verdict(good) == 1
    # A fenced block is a formatting habit, not a failure to answer.
    assert judgescreen._verdict('```json\n{"statements":[{"verdict":0}]}\n```') == 0
    assert judgescreen._verdict('Yes, it is supported.') is None
    assert judgescreen._verdict('{"statements": []}') is None
    assert judgescreen._verdict('{"verdict": 1}') is None
    assert judgescreen._verdict('') is None


# This is a unit test.
def test_a_constant_judge_is_reported_as_degenerate_not_as_fifty_percent():
    """The field that decides. A model answering the same way every time is
    unusable at any accuracy, because it cannot separate two candidates — and on
    a balanced set it posts 0.5, which reads like a merely weak judge."""
    from .raglab.judgescreen import Call, score
    calls = [Call(item_id=f'i{i}', supported=i % 2 == 0, verdict=1, parsed=True,
                  seconds=1.0, prompt='p', reply='r') for i in range(8)]
    result = score(calls)
    assert result['degenerate'] is True
    assert result['accuracy'] == 0.5
    assert result['recall_supported'] == 1.0
    assert result['recall_unsupported'] == 0.0


# This is a unit test.
def test_a_judge_that_tracks_the_claim_is_not_flagged_degenerate():
    from .raglab.judgescreen import Call, score
    calls = [Call(item_id=f'i{i}', supported=i % 2 == 0,
                  verdict=int(i % 2 == 0), parsed=True, seconds=1.0,
                  prompt='p', reply='r') for i in range(8)]
    result = score(calls)
    assert result['degenerate'] is False and result['accuracy'] == 1.0


# This is a unit test.
def test_unparseable_replies_are_counted_separately_from_wrong_ones():
    """Two different problems with two different fixes: a prompt/format issue and
    a comprehension issue. Folding them together would send you tuning the wrong
    one."""
    from .raglab.judgescreen import Call, score
    calls = [Call(item_id='a', supported=True, verdict=1, parsed=True,
                  seconds=1.0, prompt='p', reply='r'),
             Call(item_id='b', supported=False, verdict=None, parsed=False,
                  seconds=1.0, prompt='p', reply='I think maybe')]
    result = score(calls)
    assert result['schema_failures'] == 1
    assert result['n_parsed'] == 1
    assert result['accuracy'] == 1.0, 'accuracy is over what could be graded'


# This is a unit test.
def test_the_screen_refuses_to_run_without_a_backend(monkeypatch):
    """The same guard as the sweep, for the same reason: the fake provider judges
    every claim without failing, and a screen it passed would be a licence."""
    from .raglab import judgescreen
    monkeypatch.setattr(judgescreen, 'load_lab_settings', lambda: LAB_SETTINGS)
    with pytest.raises(SystemExit, match='no LLM backend'):
        judgescreen.screen(['whatever:1b'], pairs=1)


# This is a unit test.
def test_the_screen_keeps_every_prompt_and_reply_it_sent():
    """A screen that reported only an accuracy could not be re-read to see *how*
    a model failed, and 'it was a constant predictor' is a conclusion nobody can
    check from a number. This is the field that a wiped scratch directory took
    last time."""
    from dataclasses import fields

    from .raglab.judgescreen import Call
    names = {f.name for f in fields(Call)}
    assert {'prompt', 'reply', 'verdict', 'parsed', 'seconds', 'usage'} <= names


# This is a unit test.
def test_a_remote_slug_is_never_refused_on_the_strength_of_a_listing(monkeypatch):
    """OpenRouter's list is authoritative in one direction only: everything on it
    works, but a slug missing from it may still be valid — the routing suffixes
    (`:free`, `:floor`) do not appear as ids. Blocking runs that used to work is a
    worse failure than the mislabelled row this guard exists to prevent, so the
    refusal is scoped to the local backend, whose tag list *is* authoritative both
    ways."""
    keyed = replace(LAB_SETTINGS, openrouter_api_key='sk-x')
    monkeypatch.setattr(models, 'openrouter_ids',
                        lambda settings: frozenset({'openai/gpt-5-nano'}))
    cfg = LabConfig(generation=GenerationConfig(ragas_model='openai/gpt-5-mini:floor'))
    assert models.provider_problems(cfg, keyed) == []


# ---------------------------------------------------------------------------
# The HTTP surface — resource collections rather than action verbs.
# ---------------------------------------------------------------------------

# This is an integration test.
def test_the_run_and_runs_collision_is_gone(client):
    """`POST /api/run` sat one character away from `GET /api/runs`, meaning two
    unrelated things: start an evaluation, and list finished ones. Reading a
    caller you had to check the verb to know which. Both are now the same
    collection — POST creates, GET lists — and the old spellings are gone
    rather than aliased, because a second name for one thing is the thing this
    rename was fixing."""
    assert client.post('/api/run', json={}).status_code == 404
    assert client.get('/api/runs').status_code == 404
    assert client.post('/api/index', json={}).status_code == 404
    assert client.post('/api/query', json={'question': 'x'}).status_code == 404


# This is an integration test.
def test_starting_work_creates_a_job_and_says_where_to_watch_it(client):
    """202 rather than 200: the work has been accepted, not done — the response
    body is a receipt, not a result. `Location` points at the job so a caller
    never has to build the polling url by string concatenation."""
    for path, payload in (
            ('/api/indexes', {'index': {'chunker': 'session',
                                        'embedder': 'ascii-hash'}}),
            ('/api/evaluations', {'index': {'chunker': 'session',
                                            'embedder': 'ascii-hash'},
                                  'generation': {'answerer': 'none'},
                                  'limit': 1, 'ragas_mode': 'off'})):
        res = client.post(path, json=payload)
        assert res.status_code == 202, f'{path} -> {res.status_code}'
        job_id = res.json()['job_id']
        assert job_id
        assert res.headers['Location'] == f'/api/jobs/{job_id}'
        # And the url it points at is real.
        assert client.get(res.headers['Location']).status_code == 200


# This is an integration test.
def test_evaluations_lists_and_fetches_the_same_resource(client):
    """One noun, three operations, no second spelling for any of them."""
    assert 'runs' in client.get('/api/evaluations').json()
    assert client.get('/api/evaluations/no-such-run').status_code == 404


# This is an integration test.
def test_a_query_is_a_job_like_its_sibling_collections(client):
    """The ask button used to block on a synchronous POST: an implicit index
    build (on a real embedder, a 2.2 GB download plus 167 sessions of
    encoding) and every LLM stage ran behind a static 'retrieving…' note,
    indistinguishable from a dead lab. A query is accepted as a job — 202, a
    Location to poll, stage/fraction/detail while it runs — like /api/indexes
    and /api/evaluations before it, and for the same reason: the work can
    outlive anything a spinner honestly promises."""
    res = client.post('/api/queries', json={
        'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
        'retrieval': {'retriever': 'dense', 'k': 2},
        'generation': {'answerer': 'none'},
        'question': 'وام مسکن'})
    assert res.status_code == 202
    job_id = res.json()['job_id']
    assert res.headers['Location'] == f'/api/jobs/{job_id}'
    job = _finished(client, job_id)
    assert job['kind'] == 'query'
    assert job['state'] == 'done', job.get('error')
    assert 'contexts' in job['result'] and 'diagnostics' in job['result']
    # The preconditions still refuse synchronously, and still say which one:
    # a bad payload is a 400 the panel shows at once, never a job that dies.
    assert client.post('/api/queries', json={}).status_code == 400


# This is an integration test.
def test_the_query_job_hands_its_reporter_to_the_index_build(monkeypatch):
    """The longest silent wait behind the ask button is the index the query
    builds implicitly when its fingerprint is new. If the job does not pass
    its reporter down to the registry, the bar sits on 'starting 0%' for the
    whole build — the old bug wearing a new box."""
    from fastapi.testclient import TestClient

    from .raglab.server import create_app
    seen = {}
    original = IndexRegistry.get

    def spy(self, cfg, progress=None, force=False):
        seen['progress'] = progress
        return original(self, cfg, progress=progress, force=force)

    monkeypatch.setattr(IndexRegistry, 'get', spy)
    fresh = TestClient(create_app())
    res = fresh.post('/api/queries', json={
        'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
        'retrieval': {'retriever': 'bm25', 'k': 2},
        'generation': {'answerer': 'none'},
        'question': 'وام مسکن'})
    assert res.status_code == 202
    job = _finished(fresh, res.json()['job_id'])
    assert job['state'] == 'done', job.get('error')
    assert callable(seen.get('progress'))


# This is a unit test.
def test_both_frontends_watch_the_ask_as_a_job():
    """Neither panel may block on a bare fetch behind a static note: the ask
    goes through the same job box as builds and runs, so the reader sees
    stage, fraction and detail instead of guessing whether anything is
    happening at all."""
    panel = (RAGLAB_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    board = (REPO_ROOT / 'app.js').read_text(encoding='utf-8')
    # The static placeholder was the bug: it moves at no point of a build.
    assert 'retrieving…' not in panel
    # The ask joins the job-kind map the board already polls through.
    assert "query: '/queries'" in board


# This is an integration test.
def test_a_second_job_is_refused_in_readable_english(client):
    """The refusal read 'a index job is still stopping' — wrong article, and
    'stopping' for a job that is running. A message describing the wrong state
    sends the reader looking for a bug that is not there."""
    first = client.post('/api/indexes', json={
        'index': {'chunker': 'message', 'embedder': 'token-hash'}})
    assert first.status_code == 202
    second = client.post('/api/indexes', json={
        'index': {'chunker': 'turn-pair', 'embedder': 'token-hash'}})
    if second.status_code == 409:
        detail = second.json()['detail']
        assert 'a index' not in detail
        assert 'an index job is already running' in detail


# This is an integration test.
def test_jobs_index_lists_runs_with_their_config(client):
    """The Inspector (:9003) follows the lab by polling this index for the
    newest finished job of a kind, so it has to carry the config that job
    actually ran — not the raw posted body, but `LabConfig`'s own normalised
    form — and nothing heavier than id/kind/state/config."""
    posted = client.post('/api/indexes', json={
        'index': {'chunker': 'session', 'embedder': 'ascii-hash'}})
    assert posted.status_code == 202
    job = _finished(client, posted.json()['job_id'])
    assert job['state'] == 'done', job.get('error')

    entries = client.get('/api/jobs').json()['jobs']
    assert entries, 'expected at least one job listed'
    newest = entries[0]
    assert newest['id'] == job['id']
    assert newest['kind'] == 'index'
    assert newest['config']['index']['chunker'] == 'session'
    assert newest['config']['index']['embedder'] == 'ascii-hash'
    assert 'result' not in newest and '_cancel' not in newest

    chunks_total = sum(len(g['chunks'])
                       for g in job['result']['chunks_by_session'])
    assert chunks_total == job['result']['chunks']


# ---------------------------------------------------------------------------
# The panel's two usability guarantees, held by the served data rather than by
# either frontend — a rule copied into two panels is a rule that will disagree.
# ---------------------------------------------------------------------------

# This is a unit test.
def test_every_option_list_leads_with_the_default():
    """A default buried sixth reads as an exotic choice. The measured winner
    should be the first thing offered, and this fails if a default moves without
    its list — which is how the embedder default ended up behind three hash
    embedders that exist only to be measured against it."""
    cfg = LabConfig()
    for name, options, default in (
            ('chunkers', config.CHUNKERS, cfg.index.chunker),
            ('embedders', config.EMBEDDERS, cfg.index.embedder),
            ('retrievers', config.RETRIEVERS, cfg.retrieval.retriever),
            ('rerankers', config.RERANKERS, cfg.retrieval.reranker),
            ('graders', config.GRADERS, cfg.retrieval.grader),
            ('answerers', config.ANSWERERS, cfg.generation.answerer)):
        assert options[0] == default, (
            f'{name} leads with {options[0]!r} but the default is {default!r}')


# This is a unit test.
def test_a_dependent_control_is_live_only_when_its_owner_makes_it_mean_something():
    """The rule the panels grey out by. Each case is a knob the pipeline would
    ignore, so leaving it editable invites tuning a number that does nothing.

    `semantic-drift` is deliberately in the *enabled* set for chunk_chars: it
    passes the value to `_semantic_segments` as a max_chars cap, so unlike
    message/turn-pair/session it genuinely reads it."""
    def state(cfg):
        return config.dependency_state(cfg.to_dict())

    drift = state(LabConfig(index=IndexConfig(chunker='semantic-drift')))
    assert drift['index.chunk_chars']['enabled']
    assert not drift['index.overlap']['enabled']

    per_message = state(LabConfig(index=IndexConfig(chunker='message')))
    assert not per_message['index.chunk_chars']['enabled']
    assert 'structure' in per_message['index.chunk_chars']['reason']

    hashed = state(LabConfig(index=IndexConfig(embedder='char-hash')))
    assert not hashed['index.embed_model']['enabled']
    real = state(LabConfig(index=IndexConfig(embedder='sentence-transformers')))
    assert real['index.embed_model']['enabled']

    ungated = state(LabConfig(retrieval=RetrievalConfig(grader='none')))
    assert not ungated['retrieval.grade_threshold']['enabled']
    assert not ungated['retrieval.grader_model']['enabled']
    lexical_gate = state(LabConfig(retrieval=RetrievalConfig(grader='lexical')))
    assert lexical_gate['retrieval.grade_threshold']['enabled']
    assert not lexical_gate['retrieval.grader_model']['enabled']   # no model involved

    no_hyde = state(LabConfig(retrieval=RetrievalConfig(hyde=False)))
    assert not no_hyde['retrieval.expansion_model']['enabled']
    assert state(LabConfig(retrieval=RetrievalConfig(hyde=True))
                 )['retrieval.expansion_model']['enabled']

    extractive = state(LabConfig(generation=GenerationConfig(answerer='extractive')))
    assert not extractive['generation.model']['enabled']
    assert state(LabConfig(generation=GenerationConfig(answerer='llm'))
                 )['generation.model']['enabled']


# This is a unit test.
def test_every_disabled_control_says_why():
    """A greyed-out control with no reason is indistinguishable from a broken
    one. Every rule carries the sentence the panel shows."""
    for key, rule in config.DEPENDENCIES.items():
        assert rule['reason'], f'{key} has no reason'
        assert not rule['reason'].endswith('.'), (
            f'{key}: the panel completes "disabled because …", so no full stop')


# This is an integration test.
def test_the_panel_is_served_the_dependency_rules(client):
    """Both frontends read this rather than each keeping a copy."""
    served = client.get('/api/options').json()['dependencies']
    assert served['index.overlap']['on'] == ['fixed-overlap']
    assert 'semantic-drift' in served['index.chunk_chars']['on']


# This is a unit test.
def test_the_embedder_hints_render_in_the_same_order_as_the_embedders():
    """The standalone panel builds its embedder dropdown from EMBEDDER_HINTS, not
    from EMBEDDERS, so reordering one and not the other left the panel still
    leading with ascii-hash while the in-board panel led with the default. Two
    lists describing one set of choices have to agree on their order or the two
    frontends disagree about what is recommended."""
    from .raglab.embedding import EMBEDDER_HINTS

    assert [hint.kind for hint in EMBEDDER_HINTS] == list(config.EMBEDDERS)


# This is a unit test.
def test_no_hint_still_calls_a_hash_embedder_the_brain_default():
    """`ascii-hash` was labelled 'the brain default today'. Session 1 promoted
    heydariAI/persian-embeddings and retired `hash` in production *by name*, so
    the label described a configuration that now raises at boot."""
    from .raglab.embedding import EMBEDDER_HINTS

    for hint in EMBEDDER_HINTS:
        if hint.kind.endswith('-hash'):
            assert 'brain default' not in hint.label, hint.label


# ---------------------------------------------------------------------------
# Two bugs found by auditing the lab, 2026-08-02. Both are reproductions: they
# encode the correct behaviour and fail against the code as it stands.
# ---------------------------------------------------------------------------

# This is a unit test.
def test_a_gate_whose_model_call_fails_does_not_silently_pass_everything():
    """`llm_scores` catches every exception from the model and returns 0.5 for
    each document. 0.5 clears the default 0.4 threshold, so an unreachable
    model turns `grader='llm'` into a no-op and the run records nothing about
    it — measured on 2026-08-02 with Ollama down: grader=lexical returned 2
    contexts, grader=llm returned 4, the same as ungated.

    In the shipped brain that fallback is deliberate and right: production
    prefers answering with more context to emptying it. A lab is the opposite
    case. Its entire output is a claim about what a configuration scored, so a
    row labelled `grader=llm` that was measured ungated is the one artefact
    this lab must never produce — the same reasoning that already makes
    `judged_settings()` refuse an unbacked run rather than let the fake
    provider fill in.

    The parse fallback is a different thing and must survive: a line the model
    wrote that we could not read is genuinely 'no opinion'."""
    class Unreachable:
        def invoke(self, messages, **kwargs):
            raise ConnectionError('the model daemon is not running')

    with pytest.raises(Exception) as caught:
        retrieval.llm_scores(Unreachable(), 'm', 'q', ['a', 'b', 'c'])
    # And it names the cause, so the reader is not left guessing which stage
    # went missing.
    assert 'not running' in str(caught.value) or 'grade' in str(caught.value).lower()

    # Unchanged: a reply that arrives but cannot be parsed is still no opinion.
    class Unparseable:
        def invoke(self, messages, **kwargs):
            return type('Reply', (), {'content': 'I think they all look fine!'})()

    scores = retrieval.llm_scores(Unparseable(), 'm', 'q', ['a', 'b'])
    assert list(scores) == [pytest.approx(0.5), pytest.approx(0.5)]


# This is an integration test.
def test_running_an_evaluation_leaves_the_repositorys_runs_directory_alone(
        registry, ground_truth):
    """`run_eval` ends in `save_run`, which writes to the module-level
    RUNS_DIR. Nine tests redirect it to tmp_path; one —
    test_a_run_saves_the_questions_it_was_measured_on — does not take the
    fixtures, so every invocation of this suite deposits a real run file.

    Measured 2026-08-02: one more file in `.runs/` after every suite run, and
    124 of the 154 sitting there were this leak — four fifths of the
    directory, against 30 runs somebody asked for. The
    leaderboard's own guards quarantine them (no judge, unrecorded sample) so
    no real comparison is corrupted, which is why this went unnoticed; the
    cost is a `.runs/` that is mostly noise and a leaderboard padded with
    groups nobody produced on purpose.

    This test deliberately does *not* redirect RUNS_DIR — that is the thing
    under test. The fix worth making is structural rather than one more
    monkeypatch line: nine tests repeating a guard by hand is nine chances to
    forget, and one already did."""
    real = config.RUNS_DIR
    before = {p.name for p in real.glob('*.json')} if real.exists() else set()

    cfg = LabConfig(index=IndexConfig(chunker='session', embedder='ascii-hash'),
                    generation=GenerationConfig(answerer='none'))
    evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                      limit=2, balance='difficulty', ragas_mode='off')

    after = {p.name for p in real.glob('*.json')} if real.exists() else set()
    assert after == before, (
        f'the suite wrote {sorted(after - before)} into the real .runs/')


# This is a configuration invariant.
def test_no_test_in_this_suite_can_reach_the_real_runs_directory():
    """The structural half. Whatever redirects RUNS_DIR has to apply to every
    test, not to the ones whose author remembered — otherwise this returns the
    next time someone adds a case that calls run_eval."""
    assert evaluate.RUNS_DIR != config.RUNS_DIR, (
        'evaluate.RUNS_DIR still points at the repository .runs/ during tests')


# This is an integration test.
def test_both_run_routes_screen_the_models_the_backend_serves(client, monkeypatch):
    """`/api/evaluations` refused a model the active backend does not serve;
    `/api/queries` ran it. Two routes over the same pipeline disagreeing about
    which configs are legal is a bug on its own, and it got worse the moment a
    dead grade stage started raising: the panel's fastest feedback loop would
    answer a bare 500 where the slow one answers a 400 naming the model."""
    monkeypatch.setattr(models, 'provider_problems',
                        lambda cfg, settings: ['model "qwen3.5:2b" is not served'])
    payload = {'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
               'generation': {'answerer': 'none'}, 'question': 'چه خبر؟'}
    for path in ('/api/queries', '/api/evaluations'):
        res = client.post(path, json=payload)
        assert res.status_code == 400, f'{path} -> {res.status_code}'
        assert 'qwen3.5:2b' in res.json()['detail'], path


# This is an integration test.
def test_a_query_whose_gate_cannot_reach_its_model_says_so(client, monkeypatch):
    """The other half of the gate fix. Refusing to score is only an improvement
    if the refusal reaches the caller as something they can read: now that a
    query is a job, that is the job's error — surfaced, never swallowed — and
    it still has to name the stage that went missing, or the panel shows a
    blank result and the reader blames retrieval for what the grader did."""
    def unreachable(*args, **kwargs):
        raise ConnectionError('the model daemon is not running')

    monkeypatch.setattr(retrieval, 'lab_chat', unreachable)
    res = client.post('/api/queries', json={
        'question': 'چه خبر؟',
        'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
        'retrieval': {'k': 4, 'grader': 'llm', 'grade_threshold': 0.4},
        'generation': {'answerer': 'none'}})
    assert res.status_code == 202, res.status_code
    job = _finished(client, res.json()['job_id'])
    assert job['state'] == 'error'
    assert 'grade' in job['error'].lower() and 'not running' in job['error']


# --- provider modes: local vs OpenRouter -------------------------------------
# The models column grows a mode dropdown. A mode is a served preset: which
# backend runs the LLM stages and which model each stage defaults to. Served
# rather than kept in a frontend, so the two panels cannot disagree about what
# picking "openrouter" configures.


# This is a unit test.
def test_the_lab_offers_a_local_and_an_openrouter_mode():
    # Local first: it is the lab default, and an option list leads with its
    # default here (see test_every_option_list_leads_with_the_default).
    assert [mode.key for mode in models.MODES] == ['local', 'openrouter']
    by_key = {mode.key: mode for mode in models.MODES}
    assert by_key['local'].provider == 'ollama'
    assert by_key['openrouter'].provider == 'openrouter'
    # A mode explains itself like every other control on the page.
    assert all(mode.label and mode.note for mode in models.MODES)


# This is a unit test.
def test_openrouter_mode_runs_every_llm_stage_on_gpt5_nano(monkeypatch):
    """The preset: the whole LLM pipeline switched on, every stage on
    gpt-5-nano — and the index deliberately untouched, because
    heydariAI/persian-embeddings is the measured winner and stays local."""
    monkeypatch.setattr(models, 'openrouter_ids', lambda settings: frozenset())
    patch = models.mode_config('openrouter', LAB_SETTINGS)
    ret, gen = patch['retrieval'], patch['generation']
    nano = 'openai/gpt-5-nano'
    assert ret['hyde'] is True and ret['expansion_model'] == nano
    assert ret['reranker'] == 'llm' and ret['reranker_model'] == nano
    assert ret['grader'] == 'llm'
    assert ret['grader_model'] == nano       # nothing verified → the fallback
    assert ret['grade_threshold'] == 0.4     # the measured gate setting
    assert gen['answerer'] == 'llm' and gen['model'] == nano
    assert gen['key_facts_judge'] is True and gen['judge_model'] == nano
    assert gen['ragas_model'] == nano
    assert 'index' not in patch


# This is a unit test.
def test_the_gate_prefers_a_cohere_reranker_the_account_can_reach(monkeypatch):
    """cohere/rerank-4-fast is a purpose-built relevance scorer (query + text →
    score), so the gate prefers it; the -pro build is next. A slug OpenRouter's
    own model list does not verify falls back to gpt-5-nano rather than
    gambling a run on it — 'cannot verify' must not become a refused run."""
    def gate(served):
        monkeypatch.setattr(models, 'openrouter_ids',
                            lambda settings: frozenset(served))
        return models.mode_config('openrouter',
                                  LAB_SETTINGS)['retrieval']['grader_model']

    assert gate({'cohere/rerank-4-fast', 'cohere/rerank-4-pro',
                 'openai/gpt-5-nano'}) == 'cohere/rerank-4-fast'
    assert gate({'cohere/rerank-4-pro',
                 'openai/gpt-5-nano'}) == 'cohere/rerank-4-pro'
    assert gate({'openai/gpt-5-nano'}) == 'openai/gpt-5-nano'
    assert gate(set()) == 'openai/gpt-5-nano'


# This is a unit test.
def test_local_mode_resets_the_same_fields_openrouter_sets(monkeypatch):
    """Switching back must be a full reset to the lab defaults: a field one
    mode sets and the other forgets would leak a remote model into a local
    run's label."""
    monkeypatch.setattr(models, 'openrouter_ids', lambda settings: frozenset())
    local = models.mode_config('local', LAB_SETTINGS)
    remote = models.mode_config('openrouter', LAB_SETTINGS)
    assert ({group: set(names) for group, names in local.items()}
            == {group: set(names) for group, names in remote.items()})
    defaults = LabConfig().to_dict()
    for group, names in local.items():
        for name, value in names.items():
            assert value == defaults[group][name], f'{group}.{name}'
    # No auto modes anywhere in this repo: an unknown mode raises, never guesses.
    with pytest.raises(ValueError):
        models.mode_config('cloud', LAB_SETTINGS)


# This is a unit test.
def test_a_provider_override_rebuilds_the_settings_it_names():
    """The dropdown must move the backend, not just the model labels: a run
    whose models say openrouter while the settings still say ollama would be
    refused over models the user never picked."""
    swapped = config.settings_for_provider(LAB_SETTINGS, 'openrouter')
    assert swapped.provider == 'openrouter'
    # The old backend's default model must not survive the switch — a slug
    # only means something to the backend that serves it...
    assert swapped.llm_model == config.PROVIDER_MODELS['openrouter']
    back = config.settings_for_provider(swapped, 'ollama')
    assert back.llm_model == config.PROVIDER_MODELS['ollama']
    # ...but an explicitly named model (RAGLAB_MODEL) is never replaced.
    named = replace(LAB_SETTINGS, llm_model='someone/custom-7b')
    assert (config.settings_for_provider(named, 'openrouter').llm_model
            == 'someone/custom-7b')
    # '' means "no override": the settings pass through untouched.
    assert config.settings_for_provider(LAB_SETTINGS, '') is LAB_SETTINGS
    with pytest.raises(ValueError):
        config.settings_for_provider(LAB_SETTINGS, 'huggingface')


# This is an integration test.
def test_options_serves_the_provider_modes(client):
    body = client.get('/api/options').json()
    modes = {mode['key']: mode for mode in body['modes']}
    assert set(modes) == {'local', 'openrouter'}
    assert modes['openrouter']['provider'] == 'openrouter'
    served = modes['openrouter']['config']
    assert served['generation']['model'] == 'openai/gpt-5-nano'
    # The gate default is resolved against whatever this machine could verify
    # right now, so any of the three legal answers passes — never a fourth.
    assert served['retrieval']['grader_model'] in (
        'cohere/rerank-4-fast', 'cohere/rerank-4-pro', 'openai/gpt-5-nano')
    # The dropdown explains itself behind the same '!' as every other control.
    assert 'run.mode' in body['help']


# This is an integration test.
def test_both_run_routes_refuse_an_unknown_provider(client):
    """Both run routes apply the same screen — the two disagreeing about which
    configs are legal was a bug once already."""
    for route in ('/api/queries', '/api/evaluations'):
        res = client.post(route, json={'question': 'x',
                                       'provider': 'huggingface'})
        assert res.status_code == 400, route
        assert 'huggingface' in res.json()['detail']


# This is a unit test.
def test_a_mode_only_presets_models_its_own_catalogue_offers(monkeypatch):
    """The bug this pins: the panel's dropdowns were filled from the boot
    provider's catalogue, so under the openrouter mode gpt-5-nano was not
    offerable — and the panel's config-follows-the-panel rule then silently
    wiped the preset back to ''. Every model a mode presets must be offered by
    the catalogue that same mode carries."""
    monkeypatch.setattr(models, 'openrouter_ids', lambda settings: frozenset())
    for entry in models.mode_catalogue(LAB_SETTINGS):
        offered = {option['id'] for option in entry['models']}
        for group, names in entry['config'].items():
            for name, value in names.items():
                if name.endswith('model') and value:
                    assert value in offered, (
                        f"{entry['key']} presets {group}.{name}={value!r} "
                        'but its own catalogue does not offer it')


# This is an integration test.
def test_each_mode_carries_the_catalogue_of_its_own_backend(client):
    """A slug only means something to the backend that serves it, so the mode
    that moves the backend must bring that backend's model list with it."""
    body = client.get('/api/options').json()
    modes = {mode['key']: mode for mode in body['modes']}
    remote = {option['id'] for option in modes['openrouter']['models']}
    local = {option['id'] for option in modes['local']['models']}
    assert 'openai/gpt-5-nano' in remote
    assert '4skl/gemma4-e2b-mtp' in local
    # Disjoint apart from the '' lab-default entry and a model the user named
    # by RAGLAB_MODEL — an explicitly named model is offered everywhere by the
    # catalogue's own rule. The two known lists must never blur into one
    # dropdown of half-unusable choices.
    named = body['capabilities']['llm_model']
    assert (remote & local) <= {'', named}


# This is a unit test.
def test_both_panels_offer_the_mode_dropdown():
    """The dropdown sits in the models column of the board's page and in the
    standalone panel, both reading the served modes rather than a local copy —
    a preset kept in a frontend is a preset that will drift."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    appjs = (config.ROOT / 'app.js').read_text(encoding='utf-8')
    assert 'raglab-mode' in appjs and 'options.modes' in appjs
    assert 'modes' in html


# --- retrieval on its own, and the shipped assistant's own settings ---------
# The panel could build an index and score a full judged run, but it had no way
# to do the middle step alone: retrieve for the questions an experiment is
# about, look at what came back, change one knob, look again. That is the loop
# the Inspector (:9003) exists to serve, and it needs the lab to offer both a
# retrieval-only run over the *selected* questions and a one-click preset that
# is the shipped assistant rather than a taste.

# This is a configuration invariant.
def test_the_production_preset_mirrors_the_shipped_brain():
    """The preset button claims to be "the real RAG system". It has to be
    derived from the brain's own constants, not copied beside them: a preset
    that drifts is worse than no preset, because it invites a comparison
    against a config nothing ships."""
    from lodestar_brain import config as brain_config
    from lodestar_brain import retrieval as brain_retrieval

    preset = config.PRODUCTION_CONFIG
    index, ret = preset['index'], preset['retrieval']
    # chunking: the brain splits at 500 with 100 of overlap, so the lab's
    # honest mirror is fixed-overlap at those exact sizes.
    assert index['chunker'] == 'fixed-overlap'
    assert index['chunk_chars'] == brain_retrieval.CHUNK_SIZE
    assert index['overlap'] == brain_retrieval.CHUNK_OVERLAP
    brain_defaults = brain_config.Settings()
    assert index['embedder'] == brain_defaults.embedder
    # retrieval: every depth the shipped pipeline uses, read off its constants
    assert ret['k'] == brain_retrieval.TOP_K
    assert ret['candidates'] == brain_retrieval.CANDIDATES
    assert ret['rerank_depth'] == brain_retrieval.RERANK_DEPTH
    assert ret['grade_threshold'] == brain_retrieval.GRADE_THRESHOLD
    assert ret['grader'] == brain_defaults.grader
    # and the shape of the pipeline itself: hybrid + RRF, lexical rerank,
    # the Farsi time filter and query expansion on, HyDE and MMR off.
    assert ret['retriever'] == 'hybrid-rrf' and ret['reranker'] == 'lexical'
    assert ret['time_filter'] is True and ret['multi_query'] is True
    assert ret['hyde'] is False and ret['mmr_lambda'] == 1.0
    # a preset the lab would refuse to run is not a preset
    assert LabConfig.from_dict(preset).validate() == []


# This is an integration test.
def test_the_panel_serves_the_production_preset_for_its_button(client):
    """Served rather than written into the frontend, for the reason the mode
    dropdown is: a preset kept in a browser is a preset that will drift from
    the brain it claims to mirror."""
    assert client.get('/api/options').json()['production'] == config.PRODUCTION_CONFIG


# This is an integration test.
def test_retrieval_only_covers_exactly_the_experiment_questions(client,
                                                                ground_truth):
    """Retrieve, for the questions the eval card has selected, and nothing
    more: no answering, no judging, no run file. The selection has to be the
    *same* selection `/api/evaluations` would score, or the Inspector shows
    retrieval for questions the numbers were never about."""
    picked_type = ground_truth['questions'][0]['type']
    payload = {'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
               'retrieval': {'retriever': 'hybrid-rrf', 'reranker': 'none',
                             'grader': 'none', 'k': 3, 'rerank_depth': 20,
                             'time_filter': False, 'multi_query': False},
               'types': [picked_type], 'limit': 2, 'balance': 'stride'}
    res = client.post('/api/retrievals', json=payload)
    assert res.status_code == 202, res.text
    job = _finished(client, res.json()['job_id'])
    assert job['state'] == 'done', job.get('error')
    assert job['kind'] == 'retrieve'

    result = job['result']
    expected = evaluate.select_questions(ground_truth, [picked_type], 2,
                                        None, 'stride')
    assert [q['question_id'] for q in result['questions']] == \
        [q['id'] for q in expected]
    assert result['selection']['n'] == 2

    # The chunks it retrieved *from* travel with it. A run builds its index
    # implicitly, so without this the Inspector's chunks window would keep
    # showing whatever index job was last pressed — a different chunker than the
    # one that produced these rows, with nothing on screen saying so.
    groups = result['chunks_by_session']
    assert sum(len(g['chunks']) for g in groups) == result['index']['chunks']

    first = result['questions'][0]
    assert first['question_fa'] == expected[0]['question_fa']
    # retrieval only: the generation step never ran, so there is no answer to
    # show and no run file to leave behind.
    assert 'answer' not in first
    candidates = first['trace']['candidates']
    assert candidates
    for key in ('dense_rank', 'bm25_rank', 'fused_rank', 'rerank_score',
                'grade_score', 'kept'):
        assert key in candidates[0], f'missing {key}'
    # gold is marked per question, against that question's own evidence
    assert all(isinstance(c['gold'], bool) for c in candidates)


# This is an integration test.
def test_a_traced_evaluation_scores_identically_and_leaves_traces_off_disk(
        client, monkeypatch, tmp_path, registry, ground_truth):
    """A judged run now carries its per-question traces so the Inspector is
    never blank after an evaluation. Two things must stay true. The scores may
    not move — tracing is a recording of the same retrieval, not a different
    one. And the traces may not reach `.runs/`: a run file is the durable
    artifact the leaderboard reads, and 112 questions of full candidate text
    would bloat every one of them with data no score is computed from."""
    monkeypatch.setattr(evaluate, 'RUNS_DIR', tmp_path)
    payload = {'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
               'retrieval': {'retriever': 'hybrid-rrf', 'reranker': 'none',
                             'grader': 'none', 'k': 3, 'rerank_depth': 20,
                             'time_filter': False, 'multi_query': False},
               'generation': {'answerer': 'extractive'},
               'limit': 2, 'balance': 'stride', 'ragas_mode': 'off'}
    res = client.post('/api/evaluations', json=payload)
    assert res.status_code == 202, res.text
    job = _finished(client, res.json()['job_id'], timeout=120.0)
    assert job['state'] == 'done', job.get('error')

    result = job['result']
    assert len(result['rows']) == 2
    traces = result['traces']
    assert [t['question_id'] for t in traces] == [row['id'] for row in result['rows']]
    assert all(t['trace']['candidates'] for t in traces)

    # The chunks this run retrieved from, for the same reason `/api/retrievals`
    # carries them: an evaluation builds its index implicitly and creates no
    # index job, so this is the only way the Inspector can show the chunks the
    # scores were actually computed over instead of an unrelated earlier build.
    groups = result['chunks_by_session']
    assert sum(len(g['chunks']) for g in groups) == result['index']['chunks']

    saved = json.loads((tmp_path / f"{result['run_id']}.json").read_text(
        encoding='utf-8'))
    assert 'traces' not in saved, 'traces must not reach the run file'
    assert 'chunks_by_session' not in saved, 'chunk text must not reach the run file'
    assert saved['rows'] == result['rows']

    # The load-bearing half: the same config, untraced, produces the same rows
    # and the same summary. Latency is dropped at every depth — it measures the
    # machine, not the pipeline, so two runs of identical code never agree on it
    # and comparing it would make this test flaky rather than strict.
    def scores(value):
        if isinstance(value, dict):
            return {k: scores(v) for k, v in value.items() if 'latency' not in k}
        if isinstance(value, list):
            return [scores(v) for v in value]
        return value

    cfg = LabConfig.from_dict(payload)
    untraced = evaluate.run_eval(registry, ground_truth, cfg, LAB_SETTINGS,
                                 limit=2, balance='stride', ragas_mode='off')
    assert not untraced.traces, 'trace=False must record nothing'
    assert scores(untraced.rows) == scores(result['rows'])
    assert scores(untraced.summary) == scores(result['summary'])


# This is an integration test.
def test_the_panel_offers_retrieve_and_the_production_preset():
    """Both buttons the loop needs: run retrieval for the selected questions,
    and load the shipped assistant's settings in one click."""
    from .raglab.server import STATIC
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'id="retrieve-selected"' in html
    assert 'id="use-production"' in html
