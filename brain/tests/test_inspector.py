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
                 'inspector-tab', 'retrieval-table'):
        assert hook in html, f'missing {hook}'
