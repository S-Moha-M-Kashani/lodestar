# This is an integration test (real in-memory index, offline ascii-hash embedder).
from .raglab import pipeline, corpus
from .raglab.config import IndexConfig, RetrievalConfig, LabSettings
from .raglab.index import IndexRegistry

LAB_SETTINGS = LabSettings(openrouter_api_key='', llm_provider='fake')


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
