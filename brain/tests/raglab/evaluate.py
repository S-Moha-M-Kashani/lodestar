"""Run one configuration over the ground-truth set and persist the result.

A run is: build (or reuse) the index → for every question, retrieve and
optionally answer → score deterministically → optionally score with RAGAS →
write a JSON file. Runs are kept on disk so the panel can show a leaderboard
across sessions; nothing here writes anywhere near board.db or the brain's own
in-memory index.
"""
import inspect
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import corpus, embedding, metrics, models, pipeline, ragas_eval
from .config import (BALANCES, DIFFICULTIES, RUNS_DIR, LabConfig, LabSettings)
from .index import IndexRegistry, _lab_llm
from .llm import lab_chat
from .present import (chunks_by_session, evidence_spans, gold_available,
                      mark_gold, normalised_chunks)

KEY_FACTS_PROMPT = (
    'You check whether an answer contains specific facts. The answer is in '
    'Persian; the facts are in English. For each numbered fact reply on its own '
    'line with "<number>: yes" if the answer states or clearly implies it, '
    'otherwise "<number>: no". Output nothing else.')


def judge_key_facts(llm, model: str, question: dict, answer: str) -> float:
    """Share of the ground truth's atomic key facts present in the answer.

    The key facts are the most valuable field in the ground truth and the only
    one no deterministic metric can use: they are written in English while the
    answers are Farsi, so lexical overlap is meaningless and a translating judge
    is the honest way to score them."""
    facts = question.get('key_facts') or []
    if not facts or not answer:
        return float('nan')
    listing = '\n'.join(f'{i + 1}. {fact}' for i, fact in enumerate(facts))
    try:
        turn = lab_chat(llm, [{'role': 'system', 'content': KEY_FACTS_PROMPT},
                              {'role': 'user',
                               'content': f'Answer:\n{answer}\n\nFacts:\n{listing}'}],
                        model)
        text = turn.content or ''
    except Exception:
        return float('nan')
    verdicts = {}
    for line in text.splitlines():
        match = re.match(r'\s*(\d+)\s*[:.\-]\s*(yes|no|true|false)', line.strip(),
                         re.IGNORECASE)
        if match:
            verdicts[int(match.group(1))] = match.group(2).lower() in ('yes', 'true')
    if not verdicts:
        return float('nan')
    return sum(1 for i in range(1, len(facts) + 1) if verdicts.get(i)) / len(facts)


def json_safe(value):
    """NaN → None, recursively.

    A metric that is *undefined* for a question (quote recall with no evidence,
    latest-state on a question whose facts never changed) is NaN internally so
    the aggregator can skip it. NaN is not JSON, and both the panel's responses
    and the saved run files are JSON — this converts at the boundary rather than
    forcing every metric to invent a placeholder number."""
    if isinstance(value, float):
        return None if value != value else value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


@dataclass
class RunResult:
    run_id: str
    label: str
    config: dict
    index: dict
    summary: dict
    rows: list = field(default_factory=list)
    ragas: dict = field(default_factory=dict)
    seconds: float = 0.0
    started_at: str = ''
    notes: list = field(default_factory=list)
    selection: dict = field(default_factory=dict)
    # Per-question retrieval traces, for the Inspector (:9003) to show why a
    # question scored the way it did. Deliberately **absent from `as_dict`**, so
    # `save_run` cannot write them: a run file is the leaderboard's durable
    # artifact, and 112 questions of full candidate text is megabytes of data no
    # score is computed from. They travel in the job's result and die with it.
    traces: list = field(default_factory=list)
    # The chunks this run retrieved *from*, for the same reason and with the same
    # rule: absent from `as_dict`, so no chunk text reaches a run file. A run
    # builds its index implicitly and creates no index job, so without this the
    # Inspector's chunks window can only show whatever index job was last
    # started — a different chunker beside these rankings, silently.
    chunks_by_session: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {'run_id': self.run_id, 'label': self.label, 'config': self.config,
                'index': self.index, 'summary': self.summary, 'rows': self.rows,
                'ragas': self.ragas, 'seconds': self.seconds,
                'started_at': self.started_at, 'notes': self.notes,
                'selection': self.selection}

    def brief(self) -> dict:
        """Leaderboard row — no per-question detail.

        `ragas_decision` is carried explicitly rather than left for the frontend
        to recompute from `ragas`: it is the number the architecture was chosen
        by, and a leaderboard that ranks on a figure it does not store cannot be
        checked against the run it came from."""
        return {'run_id': self.run_id, 'label': self.label,
                'started_at': self.started_at, 'seconds': self.seconds,
                'config': self.config, 'summary': self.summary,
                'ragas': self.ragas.get('metrics', {}),
                'ragas_decision': self.ragas.get('decision'),
                # The error travels with the mean it qualifies. These candidates
                # sit within 0.01 of one another, so a row without it cannot say
                # whether it beat the row below it.
                'ragas_decision_stderr': (self.ragas.get('decision_spread')
                                          or {}).get('stderr'),
                # Which sample and which judge, for the same reason: a decision
                # score is comparable only against rows that scored the same
                # questions with the same model, and the leaderboard is exactly
                # where that mistake gets made.
                'selection': {k: v for k, v in self.selection.items()
                              if k != 'question_ids'},
                'judge': self.ragas.get('judge') or {},
                'n_questions': self.summary.get('n_questions', 0)}


def _stride(questions: list[dict], limit: int) -> list[dict]:
    """`limit` questions spread evenly across the list.

    Stride rather than truncate: the set is grouped by type, so the first N would
    be twenty single-hop questions and nothing else.

    Spread across the *whole* list rather than `questions[::step][:limit]`, which
    drops a tail whenever the count is not a multiple of the limit — at 112
    questions and a limit of 20 the step is 5, so it stopped at index 95 and the
    last sixteen were unreachable. Since new question types are appended, that
    silently excluded the newest type from every limited run."""
    total = len(questions)
    if limit >= total:
        return list(questions)
    return [questions[i * total // limit] for i in range(limit)]


def _balanced(questions: list[dict], limit: int) -> list[dict]:
    """`limit` questions with the difficulty bands as equal as the count allows.

    The natural distribution is lopsided — 29 easy, 57 medium, 26 hard — so a
    plain stride hands medium roughly half of every sample. That is a problem
    specifically for the four deciding metrics: they are means over questions, so
    a sample weighted toward one band measures that band's pipeline and reports
    it as the pipeline's score.

    An exactly equal split is only possible when `limit` divides by three, and
    the remainder has to land somewhere. It goes to the earlier bands in
    DIFFICULTIES order — deterministically, so two runs at the same limit sample
    the same questions and their rows are comparable. At limit=49 that is
    easy 17 / medium 16 / hard 16.

    Within each band the questions are strided, so the types stay spread too."""
    bands = [[q for q in questions if q['difficulty'] == name]
             for name in DIFFICULTIES]
    bands += [[q for q in questions
               if q['difficulty'] not in set(DIFFICULTIES)]]
    quotas = _quotas(limit, [len(band) for band in bands])
    picked_ids = {q['id'] for band, quota in zip(bands, quotas)
                  for q in _stride(band, quota)}
    # Returned in the ground truth's own order rather than band by band: the
    # per-question rows in a run file are then in the same order as the fixture,
    # which is what makes two runs diffable line by line.
    return [q for q in questions if q['id'] in picked_ids]


def _quotas(limit: int, sizes: list[int]) -> list[int]:
    """How many questions to take from each band.

    A band smaller than its share cannot make it up, so what it cannot supply is
    offered to the others rather than silently shrinking the sample — a run asked
    for 49 questions must produce 49 whenever the set holds that many."""
    quotas = [0] * len(sizes)
    remaining = min(limit, sum(sizes))
    while remaining > 0:
        hungry = [i for i, size in enumerate(sizes) if quotas[i] < size]
        if not hungry:
            break
        for i in hungry:
            if remaining == 0:
                break
            quotas[i] += 1
            remaining -= 1
    return quotas


def select_questions(ground_truth: dict, types: list[str] | None = None,
                     limit: int | None = None,
                     difficulty: list[str] | None = None,
                     balance: str = 'stride') -> list[dict]:
    """The questions one run is measured on.

    `balance='difficulty'` equalises the easy/medium/hard bands; 'stride' spreads
    across the set as it is. The default stays 'stride' so the twelve runs already
    in `.runs/` remain reproducible from their own config — a sampling rule that
    changed underneath the leaderboard would silently make old rows
    incomparable rather than merely old."""
    # Validated before anything else, and unconditionally: checked further down it
    # would pass silently whenever there was no limit to apply, so a typo in a
    # config would only raise on the runs where it happened to matter.
    if balance not in BALANCES:
        raise ValueError(f'unknown balance {balance!r}; expected one of '
                         + ', '.join(repr(name) for name in BALANCES))
    questions = ground_truth['questions']
    if types:
        questions = [q for q in questions if q['type'] in set(types)]
    if difficulty:
        questions = [q for q in questions if q['difficulty'] in set(difficulty)]
    if not limit or limit >= len(questions):
        return list(questions)
    if balance == 'difficulty':
        return _balanced(questions, limit)
    return _stride(questions, limit)


def selection_note(questions: list[dict], limit: int | None,
                   balance: str) -> dict:
    """What a run was actually measured on, saved onto the run itself.

    Two rows are only comparable if they scored the same questions, and neither
    the config nor the metric means say which those were. So the ids travel with
    the row — the sample is part of the measurement, not part of the invocation
    that produced it."""
    counts: dict[str, int] = {}
    for question in questions:
        counts[question['difficulty']] = counts.get(question['difficulty'], 0) + 1
    return {'balance': balance, 'limit': limit, 'n': len(questions),
            'by_difficulty': {name: counts.get(name, 0)
                              for name in DIFFICULTIES if counts.get(name)},
            'question_ids': [question['id'] for question in questions]}


def _question_note(done: int, questions: list[dict], difficulty: str) -> str:
    """"question 16/30 · hard". The band is there because a phase that is slow on
    hard questions is a different fact from one that is slow throughout."""
    return f'question {done}/{len(questions)} · {difficulty}'


def _reporter(progress):
    """Adapt a progress callback to `(stage, fraction, detail)` whether or not it
    accepts the detail.

    Arity is inspected once rather than caught per call: a `TypeError` swallowed
    around a user callback would also swallow a real error raised *inside* it, and
    a progress bar that hides exceptions is worse than no progress bar."""
    if progress is None:
        return lambda stage, fraction, detail='': None
    try:
        params = inspect.signature(progress).parameters
        takes_detail = (len(params) >= 3
                        or any(p.kind is p.VAR_POSITIONAL or p.kind is p.VAR_KEYWORD
                               for p in params.values()))
    except (TypeError, ValueError):         # a builtin or a C callable
        takes_detail = False
    if takes_detail:
        return lambda stage, fraction, detail='': progress(stage, fraction, detail)
    return lambda stage, fraction, detail='': progress(stage, fraction)


def trace_row(question: dict, trace: dict,
              gold_available: int | None = None) -> dict:
    """One question's retrieval trace, gold-marked, in the shape the Inspector
    renders a table from. Gold is the *ground truth's* verdict on a candidate,
    never the pipeline's: `kept` already carries what the pipeline decided, and
    keeping the two apart is the whole point of the table — a gold chunk that
    was dropped is exactly what you are looking for.

    `gold_available` is how many gold chunks existed to be found, which makes
    the count a recall statement rather than a bare tally. It is passed in
    because it is a property of the index, which this function does not hold;
    a caller without one gets `None`, and the view says "1 gold" rather than
    inventing a denominator."""
    quotes = [ev['quote'] for ev in question.get('evidence', [])]
    candidates = trace.get('candidates', [])
    for candidate, gold in zip(candidates,
                               mark_gold([c['text'] for c in candidates],
                                         quotes)):
        candidate['gold'] = gold
        # Where to paint the evidence green. Computed for every candidate rather
        # than only the gold ones, so a verbatim quote can never sit in a row
        # that was not marked — the two would disagree and the page would show
        # the disagreement instead of hiding it.
        candidate['gold_spans'] = evidence_spans(candidate['text'], quotes)
    return {'question_id': question['id'],
            'question_fa': question['question_fa'],
            'question_en': question.get('question_en', ''),
            'type': question['type'], 'difficulty': question['difficulty'],
            'answerable': bool(question.get('answerable')),
            'gold_available': gold_available,
            'trace': trace}


def run_retrieval(registry: IndexRegistry, ground_truth: dict, cfg: LabConfig,
                  settings: LabSettings, *, types: list[str] | None = None,
                  limit: int | None = None, difficulty: list[str] | None = None,
                  balance: str = 'stride', progress=None,
                  cancelled=None) -> dict:
    """Retrieval only, over the questions an experiment selected.

    The middle step the panel could not do alone: build (or reuse) the index,
    retrieve for each selected question with a full per-step trace, mark gold
    against the ground truth — and stop. Nothing is answered, nothing is scored
    and nothing is written to `.runs/`, because none of those is what this
    answers: it exists to show *what came back and where it ranked*, which is
    the loop you run twenty times while moving one knob.

    The selection is `select_questions` with the same arguments `run_eval`
    takes, deliberately: retrieval shown for questions the numbers were never
    about would be worse than showing nothing."""
    problems = cfg.validate() + models.provider_problems(cfg, settings)
    if problems:
        raise ValueError('; '.join(problems))
    report = _reporter(progress)
    check_cancelled = cancelled or (lambda: None)

    check_cancelled()
    index = registry.get(cfg.index, progress=lambda stage, f: report(stage, f * 0.4))
    questions = select_questions(ground_truth, types, limit, difficulty, balance)
    query_date = ground_truth['meta'].get('query_date', '2026-07-28')
    llm = _lab_llm(settings)
    roles = models.resolve(cfg, settings)

    # Normalised once for the whole run: `gold_available` counts gold over every
    # chunk in the index, and doing that per question would re-tokenise the
    # corpus once per question for no new information.
    norm_chunks = normalised_chunks(index)

    rows = []
    for i, question in enumerate(questions):
        check_cancelled()
        _outcome, trace = pipeline.retrieve_traced(
            index, cfg.retrieval, question['question_fa'],
            question.get('query_date', query_date), llm=llm, models=roles)
        quotes = [ev['quote'] for ev in question.get('evidence', [])]
        rows.append(trace_row(
            question, trace,
            gold_available=gold_available(index, quotes, norm_chunks)))
        report('retrieving', 0.4 + 0.6 * (i + 1) / len(questions),
               _question_note(i + 1, questions, question['difficulty']))
    report('done', 1.0, 'done')
    return {'selection': selection_note(questions, limit, balance),
            'index': {'collection': index.stats.collection,
                      'chunks': index.stats.chunks,
                      'reused': index.stats.reused},
            'config': cfg.to_dict(),
            'models': roles.as_dict(),
            # The chunks these rankings are over, reported by the run itself:
            # it built the index implicitly, so there is no index job the
            # Inspector could read them from.
            'chunks_by_session': chunks_by_session(index),
            'questions': rows}


def run_eval(registry: IndexRegistry, ground_truth: dict, cfg: LabConfig,
             settings: LabSettings, *, types: list[str] | None = None,
             limit: int | None = None, difficulty: list[str] | None = None,
             balance: str = 'stride',
             ragas_mode: str = 'offline', ragas_limit: int | None = None,
             workers: int = 1, trace: bool = False,
             progress=None, cancelled=None) -> RunResult:
    """`trace=True` records each question's per-step retrieval trace alongside
    its row, so the Inspector is not blank after an evaluation. It records the
    *same* retrieval: `retrieve_traced` fills a dict the plain path never looks
    at and returns the identical `Outcome`, so no score can move because
    tracing was asked for."""
    problems = cfg.validate() + models.provider_problems(cfg, settings)
    if problems:
        raise ValueError('; '.join(problems))
    started = time.time()
    # Both stamps come from the one clock read, so a field named for the start
    # cannot end up holding the finish — a ten-minute run whose start time is its
    # end time makes the leaderboard's timeline unreconstructable.
    clock = time.localtime(started)
    started_at = time.strftime('%Y-%m-%d %H:%M:%S', clock)
    run_id = time.strftime('%Y%m%d-%H%M%S', clock) + '-' + cfg.index.fingerprint()[:6]
    # The detail is what makes a long phase readable — "question 16/30 · hard"
    # rather than "0.66". It is passed positionally to a caller that wants it and
    # dropped for one that does not, because the panel's reporter predates it and
    # a run must not fail on its progress bar.
    report = _reporter(progress)
    check_cancelled = cancelled or (lambda: None)

    check_cancelled()
    index = registry.get(cfg.index, progress=lambda stage, f: report(stage, f * 0.4))
    check_cancelled()
    questions = select_questions(ground_truth, types, limit, difficulty, balance)
    selection = selection_note(questions, limit, balance)
    query_date = ground_truth['meta'].get('query_date', '2026-07-28')
    llm = _lab_llm(settings)
    roles = models.resolve(cfg, settings)
    # Only needed by the traced path, where every question's gold count is taken
    # over the whole index; normalised once so it is not re-tokenised per
    # question. Empty when untraced, and then nothing reads it.
    norm_chunks = normalised_chunks(index) if trace else []
    notes = list(index.stats.notes)
    # Which model ran which stage belongs in the run's own notes: comparing two
    # rows of the leaderboard without it compares two unknowns.
    notes.append(models.note_for(cfg, settings))
    # Same reason, one layer down: a row whose embedder could not represent the
    # corpus is not a result, and nothing else on the row would say so.
    notes.append(embedding.language_note(
        cfg.index.embedder,
        embedding.resolve_model(cfg.index.embedder, settings,
                                cfg.index.embed_model)))
    if not settings.llm_ready and (cfg.generation.answerer == 'llm'
                                            or cfg.retrieval.reranker == 'llm'
                                            or cfg.retrieval.grader == 'llm'
                                            or cfg.retrieval.hyde):
        notes.append('no OPENROUTER_API_KEY: LLM stages fell back to the offline '
                     'fake provider, so their numbers are meaningless')

    def handle(question: dict):
        check_cancelled()
        recorded = None
        if trace:
            outcome, tr = pipeline.retrieve_traced(
                index, cfg.retrieval, question['question_fa'],
                question.get('query_date', query_date), llm=llm, models=roles)
            quotes = [ev['quote'] for ev in question.get('evidence', [])]
            recorded = trace_row(
                question, tr,
                gold_available=gold_available(index, quotes, norm_chunks))
        else:
            outcome = pipeline.retrieve(index, cfg.retrieval,
                                        question['question_fa'],
                                        question.get('query_date', query_date),
                                        llm=llm, models=roles)
        check_cancelled()
        outcome = pipeline.answer(outcome, cfg.generation, llm=llm, models=roles)
        check_cancelled()
        row = metrics.score_question(question, outcome, cfg.retrieval.k)
        if (cfg.generation.key_facts_judge and outcome.answer
                and settings.llm_ready and question.get('answerable')):
            check_cancelled()
            row['key_fact_coverage'] = judge_key_facts(llm, roles.judge, question,
                                                       outcome.answer)
        return question, outcome, row, recorded

    pairs, rows, traces = [], [], []
    results: list = []
    if workers > 1:
        # Reported as each question lands, not after all of them: with LLM
        # answering this is the longest phase by far, and a progress bar that
        # sits at 40% for four minutes is indistinguishable from a hang.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(handle, q): i for i, q in enumerate(questions)}
            done_count = 0
            slots: list = [None] * len(questions)
            for future in as_completed(futures):
                landed = futures[future]
                slots[landed] = future.result()
                done_count += 1
                report('scoring', 0.4 + 0.5 * done_count / len(questions),
                       _question_note(done_count, questions,
                                      questions[landed]['difficulty']))
        results = [row for row in slots if row is not None]
    else:
        for i, question in enumerate(questions):
            results.append(handle(question))
            report('scoring', 0.4 + 0.5 * (i + 1) / len(questions),
                   _question_note(i + 1, questions, question['difficulty']))
    for question, outcome, row, recorded in results:
        pairs.append((question, outcome))
        rows.append(json_safe(row))
        if recorded is not None:
            traces.append(recorded)
    report('ragas', 0.92, 'judging')

    check_cancelled()
    summary = metrics.aggregate(rows)
    ragas_report: dict = {}
    if ragas_mode != 'off':
        sessions = corpus.sessions_by_id(registry.diary)
        references = {q['id']: corpus.evidence_texts(sessions, q) for q in questions}
        ragas_report = ragas_eval.run(pairs, settings, index.embedder,
                                      mode=ragas_mode, sample_limit=ragas_limit,
                                      reference_texts=references,
                                      judge_model=roles.ragas,
                                      progress=report, k=cfg.retrieval.k)
    report('done', 1.0, 'done')
    result = RunResult(run_id=run_id, label=cfg.label or cfg.index.chunker,
                       config=cfg.to_dict(),
                       index={'collection': index.stats.collection,
                              'chunks': index.stats.chunks,
                              'avg_chars': index.stats.avg_chars,
                              'p95_chars': index.stats.p95_chars,
                              'embed_dim': index.stats.embed_dim,
                              'build_seconds': index.stats.build_seconds,
                              'reused': index.stats.reused},
                       summary=summary, rows=rows, ragas=ragas_report,
                       seconds=round(time.time() - started, 2),
                       started_at=started_at, notes=notes,
                       selection=selection, traces=traces,
                       chunks_by_session=(chunks_by_session(index)
                                          if trace else []))
    save_run(result)
    return result


def save_run(result: RunResult) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f'{result.run_id}.json'
    # allow_nan=False on purpose: a NaN here would write a file that strict JSON
    # parsers reject, and the failure would surface much later as an unreadable
    # leaderboard rather than at the line that produced it.
    path.write_text(json.dumps(json_safe(result.as_dict()), ensure_ascii=False,
                               indent=1, allow_nan=False), encoding='utf-8')


def count_runs() -> int:
    """How many run files exist, whether or not they were listed.

    Served beside a limited listing because the panel asked `/api/evaluations`
    with no limit and showed the newest 50 of 164, calling that the leaderboard.
    On 2026-08-04 the same run ranked 2nd on the page and 4th over the whole
    directory and nothing on screen could explain the disagreement. A bounded
    view has to say what it left out."""
    if not RUNS_DIR.exists():
        return 0
    return sum(1 for _ in RUNS_DIR.glob('*.json'))


def list_runs(limit: int = 50) -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    out = []
    for path in sorted(RUNS_DIR.glob('*.json'), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if 'run_id' not in data:
            continue        # not a run: never assume a directory holds only ours
        ragas = data.get('ragas') or {}
        out.append({'run_id': data['run_id'], 'label': data.get('label', ''),
                    'started_at': data.get('started_at', ''),
                    'seconds': data.get('seconds', 0), 'config': data.get('config'),
                    'summary': data.get('summary', {}),
                    'ragas': ragas.get('metrics', {}),
                    # Absent on every run recorded before the score existed, and
                    # None on every run that could not measure all four — both
                    # read as "this row was not ranked", which is the truth.
                    'ragas_decision': ragas.get('decision'),
                    # None on every run recorded before the spread existed: those
                    # per-question composites are not recoverable, and `± 0`
                    # would claim more precision than the rows that measured it.
                    'ragas_decision_stderr': (ragas.get('decision_spread')
                                              or {}).get('stderr'),
                    # Which sample and which judge. `brief()` has carried these
                    # since the sweep started recording them and this list did
                    # not, so the panel's own leaderboard could not tell an
                    # incomparable row from a comparable one — which is the single
                    # mistake a leaderboard exists to prevent. The question ids
                    # stay in, unlike `brief()`: grouping needs them, and this
                    # list is read by code rather than rendered.
                    'selection': data.get('selection') or {},
                    'judge': ragas.get('judge') or {},
                    'n_questions': (data.get('summary') or {}).get('n_questions', 0)})
    return out


def load_run(run_id: str) -> dict | None:
    path = RUNS_DIR / f'{run_id}.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))
