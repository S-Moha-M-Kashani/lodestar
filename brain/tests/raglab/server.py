"""The RAG Lab service: settings panel, ad-hoc query inspector, eval runner.

Binds :9002, in the 9000 block with the brains — never a board port, because the
lab's primary surface is a page *inside* the board (Assistant → "RAG test lab"),
which proxies /api/raglab/* here. It reads two JSON fixtures, holds its indexes
in memory, and writes exactly one thing: a JSON file per run in .runs/. The
standalone panel at / remains for running the lab on its own.

**It depends on no service.** There is nothing to start first and nothing that
can be down, which is why no route probes anything before creating a job.

Runs are jobs, not requests: building a fastembed index over 157 sessions and
scoring 100 questions takes longer than any sensible HTTP timeout, so creating
one answers 202 with a job id and a Location, and the panel polls that. One job at a time — concurrent runs
would fight over the same index and produce numbers neither of them describes.
"""
import threading
import time
import traceback
import uuid
import inspect
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import (embedding, evaluate, explain, ledger, metrics, models, pipeline,
               ragas_eval, retrieval)
from .config import (ANSWERERS, BALANCES, CHUNKERS, DEPENDENCIES,
                     DIFFICULTIES, EMBEDDERS, GRADERS, PRODUCTION_CONFIG,
                     RERANKERS, RETRIEVERS, ROOT, RUNS_DIR, STEPS, LabConfig,
                     load_lab_settings, settings_for_provider)
from .corpus import load_diary, load_ground_truth
from .index import IndexRegistry, _lab_llm
from .present import chunks_by_session, mark_gold

STATIC = Path(__file__).resolve().parent / 'static'


class JobCancelled(Exception):
    """A cooperative stop requested from the RAG Lab panel."""


def _relative(path: Path) -> str:
    """A path to show in the panel: repo-relative when it is inside the repo,
    absolute when it is not. `RAGLAB_DB` can point anywhere — the suite points it
    at a temp file — and `relative_to` raises rather than shrugging."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


class Jobs:
    """In-process job table. A lab restart loses running jobs; finished runs are
    on disk, which is the part that matters."""

    def __init__(self, record=None):
        """`record(job, state)` is called once per finished job, or nothing is.

        A hook rather than a direct call to `ledger.record`, because the
        **Inspector runs this same class** (`inspector.py` does `from .server
        import Jobs`) and the Inspector is read-only. With the recording wired in
        here unconditionally, its manual chunk build silently became a second
        writer of the lab's experiment ledger from a second OS process — observed
        2026-08-04, a `kind: chunks` row from :9003 in :9002's raglab.db. A
        scratch build for looking at chunks is not an experiment anybody ranks,
        and "writes nothing" is the property that makes it safe to point at a
        running lab. So the service that owns the ledger passes the recorder, and
        the one that does not owns nothing to pass."""
        self.lock = threading.Lock()
        self.record = record
        self.jobs: dict[str, dict] = {}
        self.current: str | None = None

    def start(self, kind: str, target, config: dict | None = None) -> str:
        with self.lock:
            if self.current and self.jobs[self.current]['state'] in ('running', 'cancelling'):
                # One message per state, because they ask different things of the
                # reader: wait, versus wait then retry. The old text said 'a index
                # job is still stopping' for both — wrong article, and 'stopping'
                # for a job that had not been asked to stop, which sends the
                # reader hunting a cancellation nobody requested.
                running = self.jobs[self.current]
                article = 'an' if running['kind'][0] in 'aeiou' else 'a'
                state = ('is still cancelling'
                         if running['state'] == 'cancelling'
                         else 'is already running')
                raise HTTPException(
                    409, f'{article} {running["kind"]} job {state} — '
                         'wait for it to finish, or cancel it first')
            job_id = uuid.uuid4().hex[:10]
            self.jobs[job_id] = {'id': job_id, 'kind': kind, 'state': 'running',
                                 'stage': 'starting', 'progress': 0.0,
                                 'detail': '', 'config': config,
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
            began = time.time()
            outcome = 'done'
            try:
                # Targets that make external calls receive a cancellation probe.
                # Keep one-argument targets working for small callers and tests.
                wants_cancel = len(inspect.signature(target).parameters) >= 2
                job['result'] = target(report, cancel.is_set) if wants_cancel else target(report)
                if cancel.is_set():
                    raise JobCancelled()
            except JobCancelled:
                outcome = 'cancelled'
                job['detail'] = 'stopped before the next model call'
            except Exception as error:              # surfaced, never swallowed
                outcome = 'error'
                job['error'] = f'{type(error).__name__}: {error}'
                job['traceback'] = traceback.format_exc()[-2000:]
            # The only duration a build or a retrieval has: neither produces a
            # `RunResult` to carry one.
            job['seconds'] = round(time.time() - began, 2)
            # Recorded here rather than in each route, so a route added later is
            # in the ledger by having been run — and recorded *before* the state
            # goes terminal, because both frontends and the Inspector poll a job
            # until it stops running: a row written after `state = 'done'` is a
            # row a follower can look for and miss.
            try:
                if self.record is not None:
                    self.record(job, outcome)
            except Exception as error:
                # A ledger records the work; it is never a condition of it. A
                # judged run costs hours, and a database that cannot be opened
                # must not turn one into an error over a result nobody can read.
                # Reported on the job rather than swallowed — a bookkeeper that
                # fails silently is worse than one that fails.
                job['ledger_error'] = f'{type(error).__name__}: {error}'
            job['state'] = outcome
            if outcome == 'done':
                job['progress'] = 1.0
                job['stage'] = 'done'
            elif outcome == 'cancelled':
                job['stage'] = 'cancelled'

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, 'unknown job')
        # The event is an implementation detail, not JSON the browser can read.
        return {key: value for key, value in job.items() if key != '_cancel'}

    def list(self) -> list[dict]:
        """Newest first, and deliberately thin: an index of what has run
        (id/kind/state/config) for a follower like the Inspector to scan, not
        a dump of every job's result or its traceback."""
        return [{'id': job['id'], 'kind': job['kind'], 'state': job['state'],
                 'config': job.get('config')}
                for job in reversed(list(self.jobs.values()))]

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
    # This service owns the ledger, so this is the one place a recorder is passed.
    jobs = Jobs(record=ledger.record)
    app = FastAPI(title='Lodestar RAG Lab')

    @app.get('/')
    def panel():
        return FileResponse(STATIC / 'index.html')

    @app.get('/sorttable.js')
    def sorttable():
        """The column sorter, the one file this panel and the Inspector share —
        see `static/sorttable.js`. The only static file this service serves
        separately: everything else about the panel is in the one page."""
        return FileResponse(STATIC / 'sorttable.js',
                            media_type='application/javascript')

    @app.get('/api/options')
    def options():
        """Everything the panel needs to render itself, including what is
        actually installed — a dropdown offering a reranker whose wheel is
        missing is a bug report waiting to happen."""
        return {
            'chunkers': list(CHUNKERS), 'embedders': list(EMBEDDERS),
            'retrievers': list(RETRIEVERS), 'rerankers': list(RERANKERS),
            'graders': list(GRADERS), 'answerers': list(ANSWERERS),
            'question_types': list(metrics.TYPES),
            'difficulties': list(DIFFICULTIES),
            # How a limited run picks its questions. Served because the sample is
            # part of the measurement: two rows scored on different samples are
            # not two results, and the panel has to be able to say which.
            'balances': list(BALANCES),
            # Which dependent controls are live under the defaults, and the
            # rule behind each. Served so both panels grey out the same
            # knobs for the same stated reason — a rule copied into two
            # frontends is a rule that will disagree with itself.
            'dependencies': DEPENDENCIES,
            'defaults': LabConfig().to_dict(),
            # The shipped Assistant's own settings, for the panel's one-click
            # preset. Served rather than written into the frontend for the
            # reason the mode dropdown is: a preset kept in a browser is a
            # preset that will drift from the brain it claims to mirror.
            'production': PRODUCTION_CONFIG,
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
            # The mode dropdown: local vs OpenRouter, each with the backend it
            # runs on and the exact per-stage preset picking it applies. Served
            # so neither panel keeps a preset of its own to drift.
            'modes': models.mode_catalogue(settings),
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
                # One per model backend, so the panel can say which of the two
                # can run right now instead of finding out during a build.
                'sentence_transformers': embedding.sentence_transformers_available(),
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
                # Where an experiment lives, and the two places its account
                # lands. Stated positively because the panel used to badge a
                # Chroma database here: a reader needs to know the index is
                # thrown away with the process, not merely that no service is
                # named.
                'storage': {'index': 'memory',
                            'runs': str(RUNS_DIR.relative_to(ROOT)),
                            # A third location, and the newest: one row per
                            # finished experiment. Named for the same reason the
                            # other two are — a place data is kept that the page
                            # does not name is a place nobody knows to look in,
                            # or to clear out.
                            'experiments': _relative(ledger.db_path())},
            },
            'indexes': registry.known(),
        }

    def _with_backend(cfg: LabConfig, run_settings) -> dict:
        """A job's config, plus the chat backend it will actually run on.

        The *resolved* provider, not the payload's request: '' means "follow the
        lab's boot backend", and a ledger row saying '' would leave the one fact
        that separates a measurement from a rehearsal unrecorded. `fake` answers
        and judges without ever failing, so a run on it produces a complete set
        of confident numbers that measured nothing — `sweep.py` refuses to start
        there; a record cannot refuse, so it names the backend."""
        return cfg.to_dict() | {'provider': run_settings.provider}

    def _accepted(job_id: str) -> JSONResponse:
        """202, not 200: the work was accepted, not done, so the body is a
        receipt rather than a result. Location points at the job, so no caller
        has to build the polling url by string concatenation — the one place
        that url is spelled is here."""
        return JSONResponse({'job_id': job_id}, status_code=202,
                            headers={'Location': f'/api/jobs/{job_id}'})

    @app.post('/api/indexes')
    def build_index(payload: dict):
        cfg = LabConfig.from_dict(payload)
        force = bool(payload.get('force'))

        def work(report, _cancelled):
            index = registry.get(cfg.index, progress=report, force=force)
            return {'collection': index.stats.collection,
                    'chunks': index.stats.chunks,
                    'avg_chars': index.stats.avg_chars,
                    'p95_chars': index.stats.p95_chars,
                    'embed_dim': index.stats.embed_dim,
                    'build_seconds': index.stats.build_seconds,
                    'reused': index.stats.reused, 'notes': index.stats.notes,
                    # So a follower (the Inspector, :9003) can render what an
                    # index job actually built without holding its own index.
                    'chunks_by_session': chunks_by_session(index)}

        return _accepted(jobs.start('index', work, config=cfg.to_dict()))

    @app.post('/api/evaluations')
    def start_evaluation(payload: dict):
        cfg = LabConfig.from_dict(payload)
        # The mode dropdown's backend override, applied before the screen so
        # the settings that refuse a model are the settings that would run it.
        run_settings = settings_for_provider(settings,
                                             payload.get('provider') or '')
        problems = cfg.validate() + models.provider_problems(cfg, run_settings)
        if problems:
            raise HTTPException(400, '; '.join(problems))

        def work(report, cancelled):
            def check_cancelled():
                if cancelled():
                    raise JobCancelled()
            result = evaluate.run_eval(
                registry, ground_truth, cfg, run_settings,
                types=payload.get('types') or None,
                difficulty=payload.get('difficulty') or None,
                limit=payload.get('limit') or None,
                balance=payload.get('balance') or 'stride',
                ragas_mode=payload.get('ragas_mode', 'offline'),
                ragas_limit=payload.get('ragas_limit') or None,
                workers=int(payload.get('workers', 1)), progress=report,
                # Always traced: the Inspector must never be blank after a run,
                # and the trace is a recording of the same retrieval — the same
                # Outcome reaches scoring either way, so no number can move.
                trace=True, cancelled=check_cancelled)
            # Both are added here rather than inside `as_dict`, which is what
            # `save_run` writes: the Inspector gets them over HTTP and the run
            # file stays the summary the leaderboard reads, with no trace and no
            # chunk text in it.
            return result.as_dict() | {
                'traces': result.traces,
                'chunks_by_session': result.chunks_by_session}

        return _accepted(jobs.start('run', work, config=_with_backend(cfg, run_settings)))

    @app.post('/api/retrievals')
    def start_retrieval(payload: dict):
        """Retrieval only, over the questions the eval card has selected.

        Its own route rather than a flag on `/api/evaluations`, because it
        answers a different question and costs a different amount: no model
        answers anything, nothing is judged, and no run file is written — so it
        is the step you can afford to repeat while moving one knob. It takes the
        same selection arguments as an evaluation on purpose; retrieval shown
        for questions the numbers were never about would mislead."""
        cfg = LabConfig.from_dict(payload)
        run_settings = settings_for_provider(settings,
                                             payload.get('provider') or '')
        problems = cfg.validate() + models.provider_problems(cfg, run_settings)
        if problems:
            raise HTTPException(400, '; '.join(problems))

        def work(report, cancelled):
            def check_cancelled():
                if cancelled():
                    raise JobCancelled()
            return evaluate.run_retrieval(
                registry, ground_truth, cfg, run_settings,
                types=payload.get('types') or None,
                difficulty=payload.get('difficulty') or None,
                limit=payload.get('limit') or None,
                balance=payload.get('balance') or 'stride',
                progress=report, cancelled=check_cancelled)

        return _accepted(jobs.start('retrieve', work,
                                    config=_with_backend(cfg, run_settings)))

    @app.get('/api/jobs')
    def list_jobs():
        """An index of every job this process has run — newest first, id/kind/
        state/config only — so a follower (the Inspector) can find the newest
        finished one of a kind without fetching every job's full result."""
        return {'jobs': jobs.list()}

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str):
        return jobs.get(job_id)

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: str):
        return jobs.cancel(job_id)

    @app.get('/api/evaluations')
    def evaluations(limit: int = 50):
        # `total` beside the rows, because this listing is bounded and the browser
        # cannot know how many runs it was not sent.
        return {'runs': evaluate.list_runs(limit),
                'total': evaluate.count_runs()}

    @app.get('/api/experiments')
    def experiments(limit: int = 200):
        """Everything this lab has ever finished, newest first.

        Beside the leaderboard rather than inside it, deliberately. The
        leaderboard ranks judged runs on a mean over questions, and an index
        build has no questions and no score — a numbered table it appeared in
        would be making a rank claim about work that measured nothing. Same rule
        `leaderboard.group` already keeps: group first, rank second, and never
        rank across kinds."""
        return {'experiments': ledger.experiments(limit)}

    @app.get('/api/experiments/{experiment_id}')
    def experiment_detail(experiment_id: str):
        found = ledger.experiment(experiment_id)
        if found is None:
            raise HTTPException(404, 'unknown experiment')
        return found

    @app.get('/api/evaluations/{run_id}')
    def evaluation_detail(run_id: str):
        data = evaluate.load_run(run_id)
        if data is None:
            raise HTTPException(404, 'unknown run')
        return data

    @app.post('/api/queries')
    def ad_hoc_query(payload: dict):
        """Run one question through the current settings and return every stage.
        The fastest way to understand *why* a config scores the way it does —
        but a job all the same: the index a query builds implicitly can outwait
        any HTTP timeout, and the panel needs a stage to watch, not a spinner.
        The preconditions still refuse synchronously, so a bad payload is a 400
        the panel shows at once, never a job that dies later."""
        cfg = LabConfig.from_dict(payload)
        question = (payload.get('question') or '').strip()
        if not question:
            raise HTTPException(400, 'question is required')
        # The same screen /api/evaluations applies. It used to be missing here,
        # so one route refused a model the backend does not serve while the
        # other ran it — and now that a dead grade stage raises instead of
        # scoring everything 0.5, the difference between the two routes would
        # be a 400 naming the model against a bare 500. The provider override
        # is applied the same way too, for the same reason.
        run_settings = settings_for_provider(settings,
                                             payload.get('provider') or '')
        problems = cfg.validate() + models.provider_problems(cfg, run_settings)
        if problems:
            raise HTTPException(400, '; '.join(problems))
        query_date = payload.get('query_date') or ground_truth['meta']['query_date']

        def work(report):
            # The implicit build is the long silent part — hand it the front of
            # the bar, or it all happens on 'starting 0%'.
            index = registry.get(
                cfg.index,
                progress=lambda stage, fraction, detail='':
                    report(stage, 0.7 * fraction, detail))
            llm = _lab_llm(run_settings)
            roles = models.resolve(cfg, run_settings)
            report('retrieving', 0.75, question[:80])
            # Traced rather than plain `retrieve`: the per-step ranks are what
            # the Inspector's followed retrieval table needs, and this is the
            # one place a followed run and a manual /api/trace one share ranks
            # at all.
            outcome, trace = pipeline.retrieve_traced(
                index, cfg.retrieval, question, query_date,
                llm=llm, models=roles)
            report('answering', 0.9)
            outcome = pipeline.answer(outcome, cfg.generation, llm=llm,
                                      models=roles)
            # Exact match only, never fuzzy: a question that happens to equal
            # a ground-truth one gets its gold marks, everything else is
            # plainly ungraded rather than guessed at.
            gt_question = next((q for q in ground_truth['questions']
                                if q['question_fa'] == question), None)
            if gt_question is not None:
                quotes = [ev['quote'] for ev in gt_question.get('evidence', [])]
                gold_flags = mark_gold(
                    [c['text'] for c in trace['candidates']], quotes)
                question_id = gt_question['id']
            else:
                gold_flags = [False] * len(trace['candidates'])
                question_id = None
            for candidate, gold in zip(trace['candidates'], gold_flags):
                candidate['gold'] = gold
                candidate['question_id'] = question_id
            return (outcome.as_dict()
                   | {'models': roles.as_dict(), 'trace': trace,
                      'question_id': question_id})

        return _accepted(jobs.start('query', work,
                                    config=_with_backend(cfg, run_settings)))

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

    # GradeUnavailable needs no handler any more: both routes that run the
    # pipeline are jobs, so the gate's refusal surfaces as the job's error —
    # named stage and all — rather than as an HTTP status.

    return app


app = create_app()
