"""The RAG Lab service: settings panel, ad-hoc query inspector, eval runner.

Binds :9002, in the 9000 block with the brains — never a board port, because the
lab's primary surface is a page *inside* the board (Assistant → "RAG test lab"),
which proxies /api/raglab/* here. It reads two JSON fixtures, holds its indexes
in memory, and writes exactly one thing: a JSON file per run in .runs/. The
standalone panel at / remains for running the lab on its own.

**It depends on no service.** There is nothing to start first and nothing that
can be down, which is why no route probes anything before creating a job.

Runs are jobs, not requests: building a fastembed index over 157 sessions and
scoring 100 questions takes longer than any sensible HTTP timeout, so POST /run
returns a job id and the panel polls it. One job at a time — concurrent runs
would fight over the same index and produce numbers neither of them describes.
"""
import threading
import traceback
import uuid
import inspect
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import (embedding, evaluate, explain, metrics, models, pipeline,
               ragas_eval, retrieval)
from .config import (ANSWERERS, BALANCES, CHUNKERS, DIFFICULTIES, EMBEDDERS,
                     EXPANSIONS, GRADERS, LAYERS, RERANKERS, RETRIEVERS, ROOT,
                     RUNS_DIR, STEPS, SUMMARIZERS, LabConfig, load_lab_settings)
from .corpus import load_diary, load_ground_truth
from .index import IndexRegistry, _lab_llm

STATIC = Path(__file__).resolve().parent / 'static'


class JobCancelled(Exception):
    """A cooperative stop requested from the RAG Lab panel."""


class Jobs:
    """In-process job table. A lab restart loses running jobs; finished runs are
    on disk, which is the part that matters."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.current: str | None = None

    def start(self, kind: str, target) -> str:
        with self.lock:
            if self.current and self.jobs[self.current]['state'] in ('running', 'cancelling'):
                raise HTTPException(409, f'a {self.jobs[self.current]["kind"]} job '
                                         'is still stopping')
            job_id = uuid.uuid4().hex[:10]
            self.jobs[job_id] = {'id': job_id, 'kind': kind, 'state': 'running',
                                 'stage': 'starting', 'progress': 0.0,
                                 'detail': '',
                                 'result': None, 'error': None,
                                 'cancel_requested': False,
                                 '_cancel': threading.Event()}
            self.current = job_id

        cancel = self.jobs[job_id]['_cancel']

        def report(stage: str, fraction: float, detail: str = '') -> None:
            if cancel.is_set():
                raise JobCancelled()
            job = self.jobs[job_id]
            job['stage'] = stage
            job['progress'] = round(min(1.0, max(0.0, fraction)), 3)
            # "question 16/30 · hard" beside the fraction, because a judged run on
            # a local model spends hours inside one stage and a bar that only
            # moves at stage boundaries looks like a hang.
            job['detail'] = detail

        def run() -> None:
            job = self.jobs[job_id]
            try:
                # Targets that make external calls receive a cancellation probe.
                # Keep one-argument targets working for small callers and tests.
                wants_cancel = len(inspect.signature(target).parameters) >= 2
                job['result'] = target(report, cancel.is_set) if wants_cancel else target(report)
                if cancel.is_set():
                    raise JobCancelled()
                job['state'] = 'done'
                job['progress'] = 1.0
                job['stage'] = 'done'
            except JobCancelled:
                job['state'] = 'cancelled'
                job['stage'] = 'cancelled'
                job['detail'] = 'stopped before the next model call'
            except Exception as error:              # surfaced, never swallowed
                job['state'] = 'error'
                job['error'] = f'{type(error).__name__}: {error}'
                job['traceback'] = traceback.format_exc()[-2000:]

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, 'unknown job')
        # The event is an implementation detail, not JSON the browser can read.
        return {key: value for key, value in job.items() if key != '_cancel'}

    def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, 'unknown job')
        if job['state'] == 'running':
            job['cancel_requested'] = True
            job['_cancel'].set()
            job['state'] = 'cancelling'
            job['stage'] = 'stopping'
            job['detail'] = 'stopping before the next model call'
        return self.get(job_id)


def create_app() -> FastAPI:
    settings = load_lab_settings()
    diary = load_diary()
    ground_truth = load_ground_truth()
    registry = IndexRegistry(settings, diary)
    jobs = Jobs()
    app = FastAPI(title='Lodestar RAG Lab')

    @app.get('/')
    def panel():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/options')
    def options():
        """Everything the panel needs to render itself, including what is
        actually installed — a dropdown offering a reranker whose wheel is
        missing is a bug report waiting to happen."""
        return {
            'chunkers': list(CHUNKERS), 'embedders': list(EMBEDDERS),
            'summarizers': list(SUMMARIZERS), 'layers': list(LAYERS),
            'retrievers': list(RETRIEVERS), 'rerankers': list(RERANKERS),
            'graders': list(GRADERS), 'expansions': list(EXPANSIONS),
            'answerers': list(ANSWERERS),
            'question_types': list(metrics.TYPES),
            'difficulties': list(DIFFICULTIES),
            # How a limited run picks its questions. Served because the sample is
            # part of the measurement: two rows scored on different samples are
            # not two results, and the panel has to be able to say which.
            'balances': list(BALANCES),
            'defaults': LabConfig().to_dict(),
            # The three steps, in pipeline order. The panel groups and colours
            # every control by these, so which step a thing belongs to is served
            # as a fact about the pipeline rather than guessed in the browser.
            'steps': [{'key': step.key, 'short': step.short, 'label': step.label,
                       'note': step.note} for step in STEPS],
            # What each embedder can actually read, and which real models are
            # offerable — the choice that decides whether a run on a Farsi corpus
            # measures anything at all.
            'embedder_hints': embedding.embedder_hints(settings),
            'embed_models': embedding.embed_model_catalogue(settings),
            # One dropdown per LLM stage, and a sentence per knob. Both come from
            # here rather than the frontend so a new strategy or a new model
            # appears in the panel without touching app.js.
            'models': models.catalogue(settings),
            'model_roles': [role.as_dict() for role in models.ROLES],
            # What every number on the results screen means: its label, the step
            # it grades, the exact arithmetic, and what computed it. Served rather
            # than kept in the frontend so a metric's name cannot drift from its
            # definition.
            'metrics': explain.measures(),
            'help': explain.topics(),
            'corpus': {
                'sessions': len(diary['sessions']),
                'messages': sum(len(s['messages']) for s in diary['sessions']),
                'from': diary['meta']['period']['from'],
                'to': diary['meta']['period']['to'],
                'threads': len(diary['threads']),
                # The habit ledger is only as good as the habits behind it, so
                # how many the corpus tracks is part of describing it.
                'habits': len(diary.get('habits', {})),
                'questions': len(ground_truth['questions']),
                'query_date': ground_truth['meta'].get('query_date'),
            },
            'capabilities': {
                'fastembed': embedding.fastembed_available(),
                # One per model backend, so the panel can say which of the three
                # can run right now instead of finding out during a build.
                'sentence_transformers': embedding.sentence_transformers_available(),
                'openai_embeddings': embedding.openai_embeddings_available(settings),
                'cross_encoder': retrieval.cross_encoder_available(
                    settings.cross_encoder_model),
                'cross_encoder_model': settings.cross_encoder_model,
                'fastembed_model': settings.fastembed_model,
                # `llm` is "a real model is reachable", not "a key exists": with
                # RAGLAB_LLM=ollama every stage runs on this machine and there is
                # no key at all. The provider is served beside it because the
                # badge has to name where the numbers came from — a run on the
                # fake provider is not a cheaper run, it is not a run.
                'llm': settings.llm_ready,
                'llm_provider': settings.provider,
                'llm_model': settings.llm_model,
                'ollama_base_url': settings.ollama_base_url,
                'ragas': ragas_eval.availability(settings).as_dict(),
                # Where an experiment lives and where its one durable artifact
                # lands. Stated positively because the panel used to badge a
                # Chroma database here: a reader needs to know the index is
                # thrown away with the process, not merely that no service is
                # named.
                'storage': {'index': 'memory',
                            'runs': str(RUNS_DIR.relative_to(ROOT))},
            },
            'indexes': registry.known(),
        }

    @app.post('/api/index')
    def build_index(payload: dict):
        cfg = LabConfig.from_dict(payload)
        force = bool(payload.get('force'))

        def work(report, _cancelled):
            index = registry.get(cfg.index, progress=report, force=force)
            return {'collection': index.stats.collection,
                    'chunks': index.stats.chunks,
                    'by_layer': index.stats.by_layer,
                    'avg_chars': index.stats.avg_chars,
                    'p95_chars': index.stats.p95_chars,
                    'embed_dim': index.stats.embed_dim,
                    'build_seconds': index.stats.build_seconds,
                    'reused': index.stats.reused, 'notes': index.stats.notes}

        return {'job_id': jobs.start('index', work)}

    @app.post('/api/run')
    def start_run(payload: dict):
        cfg = LabConfig.from_dict(payload)
        problems = cfg.validate() + models.provider_problems(cfg, settings)
        if problems:
            raise HTTPException(400, '; '.join(problems))

        def work(report, cancelled):
            def check_cancelled():
                if cancelled():
                    raise JobCancelled()
            result = evaluate.run_eval(
                registry, ground_truth, cfg, settings,
                types=payload.get('types') or None,
                difficulty=payload.get('difficulty') or None,
                limit=payload.get('limit') or None,
                balance=payload.get('balance') or 'stride',
                ragas_mode=payload.get('ragas_mode', 'offline'),
                ragas_limit=payload.get('ragas_limit') or None,
                workers=int(payload.get('workers', 1)), progress=report,
                cancelled=check_cancelled)
            return result.as_dict()

        return {'job_id': jobs.start('run', work)}

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str):
        return jobs.get(job_id)

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: str):
        return jobs.cancel(job_id)

    @app.get('/api/runs')
    def runs(limit: int = 50):
        return {'runs': evaluate.list_runs(limit)}

    @app.get('/api/runs/{run_id}')
    def run_detail(run_id: str):
        data = evaluate.load_run(run_id)
        if data is None:
            raise HTTPException(404, 'unknown run')
        return data

    @app.post('/api/query')
    def ad_hoc_query(payload: dict):
        """Run one question through the current settings and return every stage.
        The fastest way to understand *why* a config scores the way it does."""
        cfg = LabConfig.from_dict(payload)
        question = (payload.get('question') or '').strip()
        if not question:
            raise HTTPException(400, 'question is required')
        problems = cfg.validate()
        if problems:
            raise HTTPException(400, '; '.join(problems))
        index = registry.get(cfg.index)
        llm = _lab_llm(settings)
        roles = models.resolve(cfg, settings)
        query_date = payload.get('query_date') or ground_truth['meta']['query_date']
        outcome = pipeline.retrieve(index, cfg.retrieval, question, query_date,
                                    llm=llm, models=roles)
        outcome = pipeline.answer(outcome, cfg.generation, llm=llm, models=roles)
        return outcome.as_dict() | {'models': roles.as_dict()}

    @app.get('/api/questions')
    def questions(limit: int = 200):
        """The ground truth without its answers — for picking a question to
        inspect in the query panel."""
        return {'questions': [
            {'id': q['id'], 'type': q['type'], 'difficulty': q['difficulty'],
             'question_fa': q['question_fa'], 'question_en': q['question_en'],
             'answerable': q['answerable'],
             'evidence_sessions': [ev['session_id'] for ev in q['evidence']]}
            for q in ground_truth['questions'][:limit]]}

    @app.get('/api/health')
    def health():
        # No dependency to report: the lab is up or it is not running.
        return {'ok': True, 'storage': 'memory'}

    @app.exception_handler(ValueError)
    def value_error(_request, error: ValueError):
        return JSONResponse({'detail': str(error)}, status_code=400)

    return app


app = create_app()
