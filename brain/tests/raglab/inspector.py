"""The RAG Lab Inspector — a read-only viewer served on :9003.

Three views (ground-truth pairs, chunks-by-session, per-question retrieval
trace) over the same fixtures and pipeline the lab measures with. It builds its
own in-memory index and writes nothing. Composition root: `create_inspector_app`.
"""
from lodestar_brain import textnorm


def _norm(text: str) -> str:
    return ' '.join(textnorm.tokens(text, drop_stopwords=False))


def mark_gold(candidate_texts: list[str],
              evidence_quotes: list[str]) -> list[bool]:
    """Which candidates contain a question's gold evidence quote.

    Substring either direction over the shared normaliser: a chunk may be
    smaller than a quote (part of one message) or larger (several). Normalising
    first means a whitespace or zero-width difference cannot hide a real match —
    the same reason the tokeniser is shared across the whole brain. A candidate
    that normalises to the empty string (blank, whitespace-only, or nothing the
    tokeniser keeps) is never gold — the empty string is a substring of every
    quote, which would mark a chunk with no evidence at all as a match."""
    quotes = [_norm(q) for q in evidence_quotes if q.strip()]
    out = []
    for text in candidate_texts:
        norm = _norm(text)
        out.append(bool(norm) and any(q in norm or norm in q for q in quotes))
    return out


def chunks_by_session(index) -> list[dict]:
    """Every chunk the index holds, grouped by session in index order — the
    'chunks after indexing' view. `by_session` is built in chunk order, which
    follows diary order, so no sorting is needed or wanted."""
    groups = []
    for session_id, chunks in index.by_session.items():
        groups.append({
            'session_id': session_id,
            'date': chunks[0].date if chunks else '',
            'chunks': [{'id': c.id, 'text': c.text} for c in chunks]})
    return groups


from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import evaluate, models, pipeline
from .config import LabConfig, load_lab_settings, settings_for_provider
from .corpus import load_diary, load_ground_truth
from .index import IndexRegistry, _lab_llm
from .server import Jobs

STATIC = Path(__file__).resolve().parent / 'static'

# Candidate F — the chosen architecture — as the Inspector's default config:
# the sweep baseline plus the LLM relevance gate. One source for the endpoint
# tests and the frontend so the two cannot drift.
CHOSEN_CONFIG = {
    'index': {'chunker': 'semantic-drift', 'embedder': 'sentence-transformers'},
    'retrieval': {'retriever': 'hybrid-rrf', 'k': 8, 'reranker': 'lexical',
                  'time_filter': True, 'grader': 'llm', 'grade_threshold': 0.4},
}


def create_inspector_app() -> FastAPI:
    settings = load_lab_settings()
    diary = load_diary()
    ground_truth = load_ground_truth()
    registry = IndexRegistry(settings, diary)
    jobs = Jobs()
    app = FastAPI(title='Lodestar RAG Lab Inspector')

    def _accepted(job_id: str) -> JSONResponse:
        return JSONResponse({'job_id': job_id}, status_code=202,
                            headers={'Location': f'/api/jobs/{job_id}'})

    @app.get('/')
    def page():
        return FileResponse(STATIC / 'inspector.html')

    @app.get('/inspector.css')
    def css():
        return FileResponse(STATIC / 'inspector.css', media_type='text/css')

    @app.get('/inspector.js')
    def js():
        return FileResponse(STATIC / 'inspector.js',
                            media_type='application/javascript')

    @app.get('/api/health')
    def health():
        return {'ok': True, 'storage': 'memory'}

    @app.get('/api/groundtruth')
    def groundtruth():
        return {'meta': ground_truth['meta'],
                'questions': ground_truth['questions']}

    @app.post('/api/chunks')
    def chunks(payload: dict):
        cfg = LabConfig.from_dict(payload)

        def work(report):
            index = registry.get(cfg.index, progress=report)
            groups = chunks_by_session(index)
            return {'chunks_by_session': groups,
                    'total': sum(len(g['chunks']) for g in groups)}

        return _accepted(jobs.start('chunks', work))

    @app.post('/api/trace')
    def trace(payload: dict):
        cfg = LabConfig.from_dict(payload)
        qid = payload.get('question_id')
        question = next((q for q in ground_truth['questions']
                         if q['id'] == qid), None)
        if question is None:
            raise HTTPException(404, f'unknown question id: {qid!r}')
        run_settings = settings_for_provider(settings,
                                             payload.get('provider') or '')
        query_date = payload.get('query_date') or ground_truth['meta']['query_date']

        def work(report):
            index = registry.get(
                cfg.index,
                progress=lambda stage, fraction, detail='':
                    report(stage, 0.7 * fraction, detail))
            llm = _lab_llm(run_settings)
            roles = models.resolve(cfg, run_settings)
            report('retrieving', 0.8, question['question_fa'][:80])
            _outcome, tr = pipeline.retrieve_traced(
                index, cfg.retrieval, question['question_fa'], query_date,
                llm=llm, models=roles)
            quotes = [ev['quote'] for ev in question.get('evidence', [])]
            flags = mark_gold([c['text'] for c in tr['candidates']], quotes)
            for cand, gold in zip(tr['candidates'], flags):
                cand['gold'] = gold
            return {'question': question, 'trace': tr, 'query_date': query_date}

        return _accepted(jobs.start('trace', work))

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str):
        return jobs.get(job_id)

    @app.get('/api/runs')
    def runs(limit: int = 50):
        return {'runs': evaluate.list_runs(limit)}

    @app.get('/api/runs/{run_id}')
    def run_detail(run_id: str):
        data = evaluate.load_run(run_id)
        if data is None:
            raise HTTPException(404, 'unknown run')
        return data

    return app


app = create_inspector_app()
