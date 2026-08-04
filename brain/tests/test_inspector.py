from .raglab import pipeline, corpus, inspector
from .raglab.config import IndexConfig, RetrievalConfig, LabSettings
from .raglab.index import IndexRegistry

LAB_SETTINGS = LabSettings(openrouter_api_key='', llm_provider='fake')


# This is an integration test (real in-memory index, offline ascii-hash embedder).
def test_retrieve_traced_records_ranks_and_dropped_candidates():
    diary = corpus.load_diary()
    gt = corpus.load_ground_truth()
    index = IndexRegistry(LAB_SETTINGS, diary).get(
        IndexConfig(chunker='session', embedder='ascii-hash'))
    # rerank_depth(20) > k(3): mmr keeps 3, so at least 17 candidates are dropped.
    cfg = RetrievalConfig(retriever='hybrid-rrf', reranker='none',
                          grader='none', k=3, rerank_depth=20, time_filter=False)
    question = gt['questions'][0]['question_fa']
    query_date = gt['meta']['query_date']

    outcome, trace = pipeline.retrieve_traced(
        index, cfg, question, query_date)

    assert trace['candidates'], 'trace recorded no candidates'
    first = trace['candidates'][0]
    # every step is represented on each candidate row
    for key in ('dense_rank', 'bm25_rank', 'fused_rank',
                'retrieval_score', 'rerank_score', 'grade_score', 'kept'):
        assert key in first, f'missing {key}'
    # ranks are 1-based ints or None
    for cand in trace['candidates']:
        for key in ('dense_rank', 'bm25_rank', 'fused_rank'):
            assert cand[key] is None or (isinstance(cand[key], int) and cand[key] >= 1)
    # some candidate survived, some was dropped
    kept = [c for c in trace['candidates'] if c['kept']]
    dropped = [c for c in trace['candidates'] if not c['kept']]
    assert kept and dropped, 'expected both kept and dropped candidates'
    assert len(kept) == len(outcome.contexts)
    # the ordered step lists are present
    assert trace['dense'] and trace['bm25'] and trace['fused']

    # exercise the grader path: verify grade_score is populated as float when grader is active
    cfg_with_grader = RetrievalConfig(retriever='hybrid-rrf', reranker='none',
                                      grader='lexical', grade_threshold=0.0, k=3,
                                      rerank_depth=20, time_filter=False)
    outcome_graded, trace_graded = pipeline.retrieve_traced(
        index, cfg_with_grader, question, query_date)
    graded_candidates = [c for c in trace_graded['candidates']
                         if c['grade_score'] is not None]
    assert any(isinstance(c['grade_score'], float) for c in graded_candidates), \
        'expected at least one candidate with float grade_score'


# This is a unit test.
def test_mark_gold_matches_evidence_quote_either_direction():
    quotes = ['قسط‌بندی جریمه اوکی شد شیش قسط']
    texts = [
        'خبر خوب: قسط‌بندی جریمه اوکی شد شیش قسط، از اول ماه دیگه',  # contains quote
        'امروز هوا خیلی گرم بود و کاری پیش نرفت',                    # unrelated
    ]
    flags = inspector.mark_gold(texts, quotes)
    assert flags == [True, False]

    # quote longer than a small chunk: chunk contained by the quote also counts
    assert inspector.mark_gold(['شیش قسط'], quotes) == [True]
    # no quotes → nothing is gold
    assert inspector.mark_gold(texts, []) == [False, False]

    # empty normalisation (blank, whitespace-only, punctuation-only) → never gold
    assert inspector.mark_gold(['', '   ', '...!!!'], quotes) == [False, False, False]

    # a QUOTE that normalises to empty (punctuation-only, or a single short
    # token the tokeniser drops) must not mark every candidate gold
    assert inspector.mark_gold(
        ['یک متن کاملا بی ربط', 'هر چیز دیگر'], ['؟!...']) == [False, False]
    assert inspector.mark_gold(['یک متن کاملا بی ربط'], ['۶']) == [False]


# This is an integration test (real in-memory index, offline).
def test_chunks_by_session_groups_and_counts():
    diary = corpus.load_diary()
    index = IndexRegistry(LAB_SETTINGS, diary).get(
        IndexConfig(chunker='session', embedder='ascii-hash'))
    groups = inspector.chunks_by_session(index)

    assert len(groups) == len(index.by_session)
    total = sum(len(g['chunks']) for g in groups)
    assert total == len(index.chunks)
    first = groups[0]
    assert first['session_id'] and 'date' in first
    assert all('id' in c and 'text' in c for c in first['chunks'])


import time

from fastapi.testclient import TestClient


def _client(monkeypatch):
    from .raglab import inspector
    # Pin the offline/fake backend so no test needs a key or a network.
    monkeypatch.setattr(inspector, 'load_lab_settings', lambda: LAB_SETTINGS)
    return TestClient(inspector.create_inspector_app())


# This is an integration test (FastAPI TestClient over the read-only app).
def test_groundtruth_endpoint_returns_full_pairs(monkeypatch):
    client = _client(monkeypatch)
    body = client.get('/api/groundtruth').json()
    q = body['questions'][0]
    # the fields the :9002 /api/questions endpoint strips must be present here
    for key in ('answer_fa', 'key_facts', 'evidence', 'question_fa',
                'type', 'difficulty', 'answerable'):
        assert key in q, f'missing {key}'
    assert 'quote' in q['evidence'][0]


# This is an integration test (FastAPI TestClient over the read-only app; real
# in-memory index build via the job runner).
def test_chunks_job_returns_sessions(monkeypatch):
    client = _client(monkeypatch)
    cfg = {'index': {'chunker': 'session', 'embedder': 'ascii-hash'}}
    acc = client.post('/api/chunks', json=cfg)
    assert acc.status_code == 202
    job_id = acc.json()['job_id']
    # jobs run on a daemon thread; poll until done
    for _ in range(200):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['state'] in ('done', 'error'):
            break
        time.sleep(0.02)
    assert job['state'] == 'done', job.get('error')
    result = job['result']
    assert result['total'] == sum(len(g['chunks'])
                                  for g in result['chunks_by_session'])


# This is an integration test (FastAPI TestClient over the read-only app; real
# in-memory index build and retrieval trace via the job runner).
def test_trace_job_marks_gold(monkeypatch):
    client = _client(monkeypatch)
    gt_q = client.get('/api/groundtruth').json()['questions'][0]
    payload = {'index': {'chunker': 'session', 'embedder': 'ascii-hash'},
               'retrieval': {'retriever': 'hybrid-rrf', 'reranker': 'none',
                             'grader': 'none', 'k': 3, 'rerank_depth': 20,
                             'time_filter': False},
               'question_id': gt_q['id']}
    acc = client.post('/api/trace', json=payload)
    assert acc.status_code == 202
    job_id = acc.json()['job_id']
    for _ in range(200):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['state'] in ('done', 'error'):
            break
        time.sleep(0.02)
    assert job['state'] == 'done', job.get('error')
    cands = job['result']['trace']['candidates']
    assert cands and all('gold' in c for c in cands)


# This is an integration test (the served shell exposes its test-stable hooks).
def test_inspector_page_exposes_the_three_views(monkeypatch):
    client = _client(monkeypatch)
    html = client.get('/').text
    for hook in ('tab-groundtruth', 'tab-chunks', 'tab-retrieval',
                 'inspector-tab', 'retrieval-table',
                 # the followed view's config statement and the answer text
                 # from the generation half of a followed query
                 'inspector-active-config', 'inspector-answer',
                 # one table per question of the followed experiment
                 'retrieval-questions'):
        assert hook in html, f'missing {hook}'


# --- following the lab (:9002) ----------------------------------------------

import json as _json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

FAKE_INDEX_JOB = {
    'id': 'idx-fake-1', 'kind': 'index', 'state': 'done',
    'config': {'index': {'chunker': 'session', 'embedder': 'ascii-hash'}},
    'result': {'chunks': 1, 'chunks_by_session': [
        {'session_id': 's1', 'date': '2026-01-01',
         'chunks': [{'id': 's1-0', 'text': 'chunk one'}]}]}}

FAKE_QUERY_JOB = {
    'id': 'q-fake-1', 'kind': 'query', 'state': 'done',
    'config': {'retrieval': {'retriever': 'hybrid-rrf', 'k': 8}},
    'result': {
        'question': 'یک سوال؟', 'question_id': 'q-001', 'answer': 'یک جواب.',
        'trace': {'candidates': [{'chunk_id': 's1-0', 'text': 'chunk one',
                                  'gold': True, 'dense_rank': 1, 'bm25_rank': 1,
                                  'fused_rank': 1, 'kept': True}]}}}


FAKE_CANDIDATE = {'chunk_id': 's1-0', 'text': 'chunk one', 'gold': True,
                  'dense_rank': 2, 'bm25_rank': 1, 'fused_rank': 1,
                  'rerank_score': 0.71, 'grade_score': None, 'kept': True}

# A retrieval-only run over the two questions an experiment selected.
FAKE_RETRIEVE_JOB = {
    'id': 'ret-fake-1', 'kind': 'retrieve', 'state': 'done',
    'config': {'retrieval': {'retriever': 'hybrid-rrf', 'k': 3}},
    'result': {'selection': {'n': 2},
               'questions': [
                   {'question_id': 'q-001', 'question_fa': 'سوال یک؟',
                    'trace': {'candidates': [FAKE_CANDIDATE]}},
                   {'question_id': 'q-002', 'question_fa': 'سوال دو؟',
                    'trace': {'candidates': [FAKE_CANDIDATE]}}]}}

# A judged evaluation, which carries the same per-question traces under its own
# key — the eval path scores as well as retrieves, so its rows live elsewhere.
FAKE_RUN_JOB = {
    'id': 'run-fake-1', 'kind': 'run', 'state': 'done',
    'config': {'retrieval': {'retriever': 'dense', 'k': 8}},
    'result': {'run_id': '20260804-000000-abcdef', 'rows': [{'id': 'q-009'}],
               'traces': [{'question_id': 'q-009', 'question_fa': 'سوال نه؟',
                           'trace': {'candidates': [FAKE_CANDIDATE]}}]}}

# Newest first, the order the lab's own /api/jobs uses. Tests reassign this to
# say which run happened last.
FAKE_ORDER = [FAKE_QUERY_JOB, FAKE_INDEX_JOB]


class _FakeLabHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # a canned test server has nothing worth logging

    def do_GET(self):
        by_id = {job['id']: job for job in FAKE_ORDER}
        if self.path == '/api/jobs':
            body = {'jobs': [{'id': job['id'], 'kind': job['kind'],
                              'state': job['state'], 'config': job['config']}
                             for job in FAKE_ORDER]}
        elif self.path.startswith('/api/jobs/') and \
                self.path.split('/')[-1] in by_id:
            body = by_id[self.path.split('/')[-1]]
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = _json.dumps(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_lab():
    """A tiny stand-in :9002 — canned `/api/jobs` and `/api/jobs/{id}` JSON —
    so the follow test is fast, offline and independent of the real lab's own
    behaviour."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), _FakeLabHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join(timeout=2)


# This is an integration test (FastAPI TestClient; the lab it points at is an
# unreachable port, pinning "a lab that is not running is a normal state").
def test_follow_reports_lab_down_without_raising(monkeypatch):
    monkeypatch.setenv('RAGLAB_INSPECTOR_LAB_URL', 'http://127.0.0.1:9')
    client = _client(monkeypatch)
    res = client.get('/api/follow')
    assert res.status_code == 200
    body = res.json()
    assert body['lab'] == 'down'
    assert body['index'] is None and body['query'] is None


# This is an integration test (FastAPI TestClient over the read-only app; the
# lab is a canned fake HTTP server, not the real :9002).
def test_follow_reads_a_finished_index_and_query_job(monkeypatch, fake_lab):
    monkeypatch.setenv('RAGLAB_INSPECTOR_LAB_URL', fake_lab)
    client = _client(monkeypatch)
    body = client.get('/api/follow').json()

    assert body['lab'] == 'up'
    assert body['index']['config']['index']['chunker'] == 'session'
    assert body['index']['chunks_by_session'][0]['session_id'] == 's1'
    assert body['query']['config']['retrieval']['retriever'] == 'hybrid-rrf'
    assert body['query']['answer'] == 'یک جواب.'
    assert body['query']['question_id'] == 'q-001'
    assert body['query']['trace']['candidates'][0]['gold'] is True


# This is an integration test (FastAPI TestClient; the lab is a canned fake).
def test_follow_shows_one_table_per_selected_question(monkeypatch, fake_lab,
                                                      request):
    """The retrieval window is per-question, and it must show *only* the
    questions the experiment picked. Both routes that retrieve over a set feed
    it — the retrieval-only run and a judged evaluation — so `/api/follow`
    normalises them to one shape and the page keeps one renderer. Whichever ran
    last wins, because that is what "follow the lab" means."""
    module = request.module
    monkeypatch.setenv('RAGLAB_INSPECTOR_LAB_URL', fake_lab)
    client = _client(monkeypatch)

    monkeypatch.setattr(module, 'FAKE_ORDER',
                        [FAKE_RETRIEVE_JOB, FAKE_RUN_JOB, FAKE_INDEX_JOB])
    view = client.get('/api/follow').json()['retrieval']
    assert view['kind'] == 'retrieve'
    assert view['config']['retrieval']['k'] == 3
    assert [q['question_id'] for q in view['questions']] == ['q-001', 'q-002']
    candidate = view['questions'][0]['trace']['candidates'][0]
    assert candidate['gold'] is True and candidate['fused_rank'] == 1

    # an evaluation that finished later is what the window follows instead, and
    # its traces arrive under a different key on the lab's side
    monkeypatch.setattr(module, 'FAKE_ORDER',
                        [FAKE_RUN_JOB, FAKE_RETRIEVE_JOB, FAKE_INDEX_JOB])
    view = client.get('/api/follow').json()['retrieval']
    assert view['kind'] == 'run'
    assert [q['question_id'] for q in view['questions']] == ['q-009']

    # no set-wide run at all is a normal state, not an error
    monkeypatch.setattr(module, 'FAKE_ORDER', [FAKE_INDEX_JOB])
    body = client.get('/api/follow').json()
    assert body['lab'] == 'up' and body['retrieval'] is None
