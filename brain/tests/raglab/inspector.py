"""The RAG Lab Inspector — a read-only viewer served on :9003.

Three views (ground-truth pairs, chunks-by-session, per-question retrieval
trace) over the same fixtures and pipeline the lab measures with. It builds its
own in-memory index and writes nothing. Composition root: `create_inspector_app`.

It is also a **live follower of the lab itself**: `GET /api/follow` polls the
lab (:9002, `RAGLAB_INSPECTOR_LAB_URL`) over plain `urllib` for its newest
finished index and query jobs, so the two panels stay separate OS processes
sharing nothing but HTTP — the lab keeps no database and the Inspector keeps
no write path, so this is the only link between them. A lab that is not
running is a normal state, not an error: every failure to reach it comes back
as `{'lab': 'down', ...}` rather than an exception, the same rule the rest of
this file follows for a missing service.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import evaluate, models, pipeline
from .config import LabConfig, load_lab_settings, settings_for_provider
from .corpus import load_diary, load_ground_truth
from .index import IndexRegistry, _lab_llm
from .present import chunks_by_session, mark_gold
from .server import Jobs

STATIC = Path(__file__).resolve().parent / 'static'

LAB_URL_ENV = 'RAGLAB_INSPECTOR_LAB_URL'
DEFAULT_LAB_URL = 'http://localhost:9002'
# Short on purpose: the Inspector polls this every ~2s from the page, so a lab
# that is merely slow to answer must not stack up hung requests behind it.
LAB_TIMEOUT = 2.5


def lab_base_url() -> str:
    return os.environ.get(LAB_URL_ENV, DEFAULT_LAB_URL).rstrip('/')


def _lab_get(path: str) -> dict | None:
    """GET one path from the lab. Every way this can fail — connection
    refused, timeout, a non-200, a body that is not JSON — comes back as
    `None`. stdlib `urllib` only: the lab and the Inspector are both test-only
    tooling, and a poller of another local service does not earn a new
    dependency."""
    url = f'{lab_base_url()}{path}'
    try:
        with urllib.request.urlopen(url, timeout=LAB_TIMEOUT) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

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

    @app.get('/api/follow')
    def follow():
        """What the page needs to render an auto-following view, in one call:
        the lab's own newest *finished* index and query jobs, or a plain
        'down' when :9002 cannot be reached at all. HTTP 200 either way — a
        lab that is not running is a normal state here, same as everywhere
        else in this file."""
        jobs_index = _lab_get('/api/jobs')
        if jobs_index is None:
            return {'lab': 'down', 'lab_url': lab_base_url(),
                    'index': None, 'query': None}

        def newest_done(kind: str) -> dict | None:
            for entry in jobs_index.get('jobs', []):
                if entry.get('kind') == kind and entry.get('state') == 'done':
                    return entry
            return None

        def view(kind: str, fields: tuple[str, ...]) -> dict | None:
            entry = newest_done(kind)
            if entry is None:
                return None
            full = _lab_get(f"/api/jobs/{entry['id']}")
            if full is None or full.get('result') is None:
                return None
            result = full['result']
            out = {'job_id': entry['id'], 'config': full.get('config')}
            out.update({field: result.get(field) for field in fields})
            return out

        index_view = view('index', ('chunks_by_session',))
        query_view = view('query', ('trace', 'question', 'question_id', 'answer'))
        return {'lab': 'up', 'lab_url': lab_base_url(),
                'index': index_view, 'query': query_view}

    return app


app = create_inspector_app()
