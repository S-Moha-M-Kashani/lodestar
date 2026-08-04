from .raglab import evaluate, pipeline, corpus, inspector, present
from .raglab.config import IndexConfig, RetrievalConfig, LabSettings
from .raglab.index import IndexRegistry

LAB_SETTINGS = LabSettings(openrouter_api_key='', llm_provider='fake')


# This is a unit test.
def test_evidence_spans_locate_the_quote_and_never_invent_one():
    """The green highlight is drawn from these ranges, so a range that is not
    really the quote is a lie on screen. Computed here rather than in the
    browser because `mark_gold` also calls a chunk *contained by* a quote gold —
    that candidate has no verbatim quote inside it and must highlight nothing."""
    quote = 'قسط‌بندی جریمه اوکی شد شیش قسط'
    text = f'خبر خوب: {quote}، از اول ماه دیگه'
    spans = present.evidence_spans(text, [quote])
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == quote

    # gold by the *reverse* direction — the chunk sits inside the quote — so
    # there is nothing verbatim to mark, and no span may be guessed
    assert present.evidence_spans('شیش قسط', [quote]) == []
    # a quote that is simply absent
    assert present.evidence_spans('یک متن بی ربط', [quote]) == []
    # no quotes at all, and empty text
    assert present.evidence_spans(text, []) == []
    assert present.evidence_spans('', [quote]) == []

    # two quotes in one chunk come back in reading order, and touching or
    # overlapping ranges merge — two <mark>s over the same characters would
    # nest and render as a darker stripe nobody asked for
    a, b = 'اول ماه', 'ماه دیگه'
    merged = present.evidence_spans('قبل از اول ماه دیگه بود', [a, b])
    assert len(merged) == 1
    s, e = merged[0]
    assert 'قبل از اول ماه دیگه بود'[s:e] == 'اول ماه دیگه'


# This is an integration test (real fixture, real chunker).
def test_a_question_reports_how_many_gold_chunks_existed_to_find():
    """"1 gold" is not a result until you know it was 1 of how many.

    The denominator is how many chunks in the whole index hold this question's
    evidence — what was *available* to retrieve — not how many evidence quotes
    the fixture lists, because one quote can be split across two chunks and one
    chunk can carry two quotes. That makes the pair a recall statement: found
    over findable."""
    gt = corpus.load_ground_truth()
    index = IndexRegistry(LAB_SETTINGS, corpus.load_diary()).get(
        IndexConfig(chunker='fixed-overlap', chunk_chars=500, overlap=100,
                    contextual=True, embedder='ascii-hash'))
    question = next(q for q in gt['questions'] if q.get('evidence'))
    quotes = [ev['quote'] for ev in question['evidence']]

    total = present.gold_available(index, quotes)
    # counted the same way a candidate is marked, over every chunk in the index
    assert total == sum(present.mark_gold([c.text for c in index.chunks], quotes))
    assert total >= 1, 'a question whose evidence is in the corpus must be findable'

    # and it travels on the row, beside the candidates it is the denominator for
    cfg = RetrievalConfig(retriever='hybrid-rrf', reranker='none', grader='none',
                          k=5, rerank_depth=20, time_filter=False,
                          multi_query=False)
    _outcome, trace = pipeline.retrieve_traced(
        index, cfg, question['question_fa'], gt['meta']['query_date'])
    row = evaluate.trace_row(question, trace, gold_available=total)
    assert row['gold_available'] == total
    found = sum(1 for c in row['trace']['candidates'] if c['gold'])
    assert found <= row['gold_available'], (
        'more gold retrieved than exists — the two are counted differently')

    # a caller that does not know the index still gets a row, with the count
    # absent rather than a wrong number standing in for it
    assert evaluate.trace_row(question, trace)['gold_available'] is None


# This is an integration test (real fixture, real chunker).
def test_a_traced_candidate_carries_spans_that_slice_back_to_the_quote():
    """End to end over the real corpus: whatever the pipeline retrieved, every
    span on every candidate must slice out of that candidate's own text, and a
    candidate marked gold with a verbatim quote must carry at least one."""
    gt = corpus.load_ground_truth()
    index = IndexRegistry(LAB_SETTINGS, corpus.load_diary()).get(
        IndexConfig(chunker='fixed-overlap', chunk_chars=500, overlap=100,
                    contextual=True, embedder='ascii-hash'))
    cfg = RetrievalConfig(retriever='hybrid-rrf', reranker='none', grader='none',
                          k=5, rerank_depth=20, time_filter=False,
                          multi_query=False)
    question = next(q for q in gt['questions'] if q.get('evidence'))
    _outcome, trace = pipeline.retrieve_traced(
        index, cfg, question['question_fa'], gt['meta']['query_date'])
    row = evaluate.trace_row(question, trace)

    quotes = [ev['quote'] for ev in question['evidence']]
    verbatim_seen = 0
    for candidate in row['trace']['candidates']:
        spans = candidate['gold_spans']
        assert isinstance(spans, list)
        for start, end in spans:
            assert candidate['text'][start:end] in quotes
        if spans:
            verbatim_seen += 1
            assert candidate['gold'], 'a highlighted candidate must be gold'
        elif any(q in candidate['text'] for q in quotes):
            raise AssertionError('a verbatim quote was left unhighlighted')
    assert verbatim_seen, 'expected at least one candidate to highlight'


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


# This is an integration test (the served shell exposes the new views' hooks).
def test_page_exposes_the_generation_tab_and_the_evidence_reveal(monkeypatch):
    """Four things the two new features are rendered by, so a rename cannot
    quietly remove one: a fourth tab and its view, the per-question header that
    restates the question and its expected facts, the full-text reveal a hover
    opens, and the green evidence mark inside it."""
    client = _client(monkeypatch)
    html = client.get('/').text
    css = client.get('/inspector.css').text
    for hook in ('tab-generation', 'view-generation', 'generation-questions',
                 'generation-active-config'):
        assert hook in html, f'missing {hook}'
    # the reveal and the highlight are CSS-driven, so the classes must exist
    for rule in ('.chunk-reveal', '.evidence-mark'):
        assert rule in css, f'missing style {rule}'
    # hover opens the reveal without a click
    assert ':hover .chunk-reveal' in css or '.retrieval-row:hover' in css


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
    'config': {'index': {'chunker': 'semantic-drift',
                         'embedder': 'sentence-transformers'},
               'retrieval': {'retriever': 'dense', 'k': 8}},
    'result': {'run_id': '20260804-000000-abcdef',
               # a row as `metrics.score_question` builds it: the generated
               # answer plus the deterministic generation scores
               'rows': [{'id': 'q-009', 'type': 'single-hop', 'answerable': True,
                         'answer': 'یک جواب که مدل نوشت.',
                         'answer_similarity': 0.42, 'key_fact_coverage': 0.5,
                         'abstained': False, 'recall': 1.0}],
               'summary': {'n_questions': 1, 'overall': {'answer_similarity': 0.42}},
               'ragas': {'metrics': {'faithfulness': 0.75}, 'decision': 0.7},
               'traces': [{'question_id': 'q-009', 'question_fa': 'سوال نه؟',
                           'trace': {'candidates': [FAKE_CANDIDATE]}}],
               # An evaluation builds its index implicitly, so it reports the
               # chunks it used itself — there is no index job to read them from.
               'chunks_by_session': [
                   {'session_id': 'run-s1', 'date': '2026-02-02',
                    'chunks': [{'id': 'run-s1-0', 'text': 'drifted chunk'}]}]}}

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


# This is an integration test (FastAPI TestClient; the lab is a canned fake).
def test_follow_shows_the_chunks_the_last_run_actually_used(monkeypatch,
                                                           fake_lab, request):
    """The two windows must describe the same pipeline.

    An evaluation builds its index *implicitly*, so it creates no index job —
    which meant the chunks window kept showing whatever `Build` was last pressed
    while the retrieval window showed the experiment. Running a 10-question
    semantic-drift experiment after an unrelated turn-pair build showed
    turn-pair chunks beside semantic-drift rankings, with nothing on screen
    admitting it. So the chunks come from the newest job that produced any,
    whatever its kind."""
    module = request.module
    monkeypatch.setenv('RAGLAB_INSPECTOR_LAB_URL', fake_lab)
    client = _client(monkeypatch)

    # the exact shape that misled: the run is newer than the index build, and
    # they name different chunkers
    monkeypatch.setattr(module, 'FAKE_ORDER', [FAKE_RUN_JOB, FAKE_INDEX_JOB])
    body = client.get('/api/follow').json()
    assert body['index']['config']['index']['chunker'] == 'semantic-drift'
    assert body['index']['chunks_by_session'][0]['session_id'] == 'run-s1'
    # and both windows now agree about which index produced what is on screen
    assert (body['index']['config']['index']['chunker']
            == body['retrieval']['config']['index']['chunker'])

    # an explicit build afterwards is the newest again, and wins
    monkeypatch.setattr(module, 'FAKE_ORDER', [FAKE_INDEX_JOB, FAKE_RUN_JOB])
    body = client.get('/api/follow').json()
    assert body['index']['config']['index']['chunker'] == 'session'


# This is an integration test (FastAPI TestClient over the read-only app).
def test_explain_serves_the_same_metric_help_the_lab_does(monkeypatch):
    """The Generation tab's '!' marks read this. Served from `explain` — the
    lab's own source for /api/options — rather than copied into the Inspector's
    page, so the two panels cannot end up explaining the same metric
    differently."""
    from .raglab import explain
    client = _client(monkeypatch)
    body = client.get('/api/explain').json()

    assert body['metrics'] == explain.measures()
    assert body['help'] == explain.topics()
    # the generation half specifically, since that is what the new tab grades
    generation = {m['key'] for m in body['metrics'] if m['step'] == 'generation'}
    assert {'answer_similarity', 'key_fact_coverage', 'faithfulness',
            'abstained_correctly'} <= generation
    # every measure carries the text the '!' opens, or the mark has nothing to say
    assert all(m.get('formula') or m.get('note') or body['help'].get(f"metric.{m['key']}")
               for m in body['metrics'])


# This is an integration test (FastAPI TestClient; the lab is a canned fake).
def test_follow_exposes_what_the_evaluation_generated(monkeypatch, fake_lab,
                                                     request):
    """The Generation tab needs three things per question that retrieval alone
    cannot give: what the model wrote, how it scored, and — from the fixture,
    not the run — the answer it should have written. The first two come from the
    evaluation's own rows; only an evaluation has them, so a retrieval-only run
    leaves this `None` rather than showing a stale answer beside fresh ranks."""
    module = request.module
    monkeypatch.setenv('RAGLAB_INSPECTOR_LAB_URL', fake_lab)
    client = _client(monkeypatch)

    monkeypatch.setattr(module, 'FAKE_ORDER', [FAKE_RUN_JOB, FAKE_INDEX_JOB])
    view = client.get('/api/follow').json()['generation']
    assert view['job_id'] == FAKE_RUN_JOB['id']
    assert view['config']['retrieval']['retriever'] == 'dense'
    row = view['rows'][0]
    assert row['id'] == 'q-009'
    assert row['answer'] == 'یک جواب که مدل نوشت.'
    assert row['answer_similarity'] == 0.42
    # the run-level judged scores, which are per run and not per question
    assert view['ragas']['metrics']['faithfulness'] == 0.75
    assert view['summary']['n_questions'] == 1

    # a retrieval-only run generates nothing, and says so rather than lying
    monkeypatch.setattr(module, 'FAKE_ORDER', [FAKE_RETRIEVE_JOB, FAKE_INDEX_JOB])
    body = client.get('/api/follow').json()
    assert body['generation'] is None
    assert body['retrieval']['kind'] == 'retrieve'   # retrieval still follows
