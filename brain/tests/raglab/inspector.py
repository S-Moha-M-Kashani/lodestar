"""The RAG Lab Inspector — a read-only viewer served on :9003.

Three views (ground-truth pairs, chunks-by-session, per-question retrieval
trace) over the same fixtures and pipeline the lab measures with. It builds its
own in-memory index and writes nothing. Composition root: `create_inspector_app`.

It is also a **live follower of the lab itself**: `GET /api/follow` polls the
lab (:9002, `RAGLAB_INSPECTOR_LAB_URL`) over plain `urllib` for its newest
finished index and query jobs, so the two panels stay separate OS processes
sharing nothing but HTTP — the lab's index is process memory and the Inspector
keeps no write path, so this is the only link between them. Notably it does
*not* read `raglab.db`: what the Inspector shows is what the lab is running
now, and reaching into the other process's file would make a live view of a
running job look like a record of a finished one. A lab that is not
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

from . import evaluate, explain, metrics, models, pipeline
from .config import LabConfig, load_lab_settings, settings_for_provider
from .corpus import load_diary, load_ground_truth
from .index import IndexRegistry, _lab_llm
from .present import chunks_by_session, gold_available, mark_gold
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
    # No recorder, deliberately: the lab's job runner writes a row per finished
    # job into raglab.db, and this service must not. Its chunk build is a scratch
    # look, not an experiment anybody ranks, and "the Inspector writes nothing" is
    # what makes it safe to aim at a lab that is running.
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

    @app.get('/sorttable.js')
    def sorttable():
        """The column sorter, the one file this service and the panel share.

        Both are served out of the same directory, so clicking a header can mean
        one thing on both pages instead of two that drift apart."""
        return FileResponse(STATIC / 'sorttable.js',
                            media_type='application/javascript')

    @app.get('/api/health')
    def health():
        return {'ok': True, 'storage': 'memory'}

    @app.get('/api/explain')
    def explain_metrics():
        """What every score on the Generation tab means, behind its '!' mark.

        Straight from `explain`, which is where `/api/options` on :9002 gets the
        same text: a metric's definition written twice is a metric whose two
        panels will eventually disagree about what it measures."""
        return {'metrics': explain.measures(), 'help': explain.topics()}

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

    @app.post('/api/questions')
    def run_question(payload: dict):
        """Run one ground-truth question end to end, under the config the page is
        following, and return it shaped exactly like the ones the experiment
        selected — a retrieval row and a generation row.

        This is how a question you were curious about joins a run you have
        already looked at. Both halves matter: retrieval alone cannot say whether
        the answer would have been right, and a row scored under different
        settings than its neighbours is worse than no row, which is why the
        config travels in the request rather than being decided here.

        The index is built in *this* process. That is the price of the Inspector
        being a follower rather than a driver of the lab: with a real encoder the
        first question pays for embedding the corpus again, then the registry
        keeps it for the rest of the session."""
        cfg = LabConfig.from_dict(payload)
        qid = payload.get('question_id')
        question = next((q for q in ground_truth['questions']
                         if q['id'] == qid), None)
        if question is None:
            raise HTTPException(404, f'unknown question id: {qid!r}')
        run_settings = settings_for_provider(settings,
                                             payload.get('provider') or '')
        problems = cfg.validate() + models.provider_problems(cfg, run_settings)
        if problems:
            raise HTTPException(400, '; '.join(problems))
        query_date = payload.get('query_date') or ground_truth['meta']['query_date']

        def work(report):
            index = registry.get(
                cfg.index,
                progress=lambda stage, fraction, detail='':
                    report(stage, 0.6 * fraction, detail))
            llm = _lab_llm(run_settings)
            roles = models.resolve(cfg, run_settings)
            report('retrieving', 0.65, question['question_fa'][:80])
            outcome, trace = pipeline.retrieve_traced(
                index, cfg.retrieval, question['question_fa'], query_date,
                llm=llm, models=roles)
            quotes = [ev['quote'] for ev in question.get('evidence', [])]
            retrieval = evaluate.trace_row(
                question, trace,
                gold_available=gold_available(index, quotes))
            report('answering', 0.85)
            outcome = pipeline.answer(outcome, cfg.generation, llm=llm,
                                      models=roles)
            # Scored by the same function the evaluation scores with, so the row
            # carries the same metrics under the same names — the whole point of
            # adding a question beside the ones already on screen.
            row = evaluate.json_safe(
                metrics.score_question(question, outcome, cfg.retrieval.k))
            return {'config': cfg.to_dict(), 'models': roles.as_dict(),
                    'retrieval': retrieval, 'generation': row}

        return _accepted(jobs.start('question', work, config=cfg.to_dict()))

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
            return {'lab': 'down', 'lab_url': lab_base_url(), 'index': None,
                    'query': None, 'retrieval': None, 'generation': None}

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

        def question_set() -> dict | None:
            """The newest finished run that retrieved over a *set* of questions,
            normalised to one shape so the page keeps one renderer.

            Two lab routes produce one: a retrieval-only run, which returns its
            rows under `questions`, and a judged evaluation, which carries the
            same rows under `traces` beside its scores. Whichever finished last
            is what the window shows — that is what following the lab means."""
            for entry in jobs_index.get('jobs', []):
                kind, key = entry.get('kind'), None
                if kind == 'retrieve':
                    key = 'questions'
                elif kind == 'run':
                    key = 'traces'
                if key is None or entry.get('state') != 'done':
                    continue
                full = _lab_get(f"/api/jobs/{entry['id']}")
                result = (full or {}).get('result') or {}
                rows = result.get(key)
                if not rows:
                    # A run from before tracing existed, or one whose selection
                    # came out empty: keep looking rather than showing a table
                    # with no rows and no explanation.
                    continue
                return {'kind': kind, 'job_id': entry['id'],
                        'config': full.get('config'),
                        'selection': result.get('selection'),
                        'questions': rows}
            return None

        def newest_chunks() -> dict | None:
            """The chunks the lab's newest finished job actually used — whatever
            kind of job it was.

            Not `kind == 'index'`, which is the bug this replaced: a run builds
            its index *implicitly*, so an experiment creates no index job, and
            the chunks window kept showing whatever `Build` was last pressed. A
            10-question semantic-drift experiment after an unrelated turn-pair
            build displayed turn-pair chunks beside semantic-drift rankings with
            nothing on screen admitting it. Every route that builds an index now
            reports the chunks it used, so the rule is simply "the newest job
            that produced any" and the two windows cannot disagree."""
            for entry in jobs_index.get('jobs', []):
                if entry.get('state') != 'done':
                    continue
                full = _lab_get(f"/api/jobs/{entry['id']}")
                groups = ((full or {}).get('result') or {}).get('chunks_by_session')
                if not groups:
                    continue        # a query job, or a run from before this
                return {'kind': entry.get('kind'), 'job_id': entry['id'],
                        'config': full.get('config'),
                        'chunks_by_session': groups}
            return None

        def generation() -> dict | None:
            """What the newest *evaluation* wrote and how it scored.

            Only an evaluation generates: the retrieval-only route stops before
            the answerer by design, so after one of those this stays the last
            evaluation rather than becoming empty — and it carries its own config
            so a reader can see when that is an older run than the retrieval
            tables beside it. The ideal answer is not here: it belongs to the
            fixture, not to a run, and the page already holds the ground truth."""
            out = view('run', ('rows', 'summary', 'ragas'))
            return out if out and out.get('rows') else None

        query_view = view('query', ('trace', 'question', 'question_id', 'answer'))
        return {'lab': 'up', 'lab_url': lab_base_url(),
                'index': newest_chunks(), 'query': query_view,
                'retrieval': question_set(), 'generation': generation()}

    return app


app = create_inspector_app()
