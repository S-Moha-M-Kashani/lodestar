"""Run one configuration over the ground-truth set and persist the result.

A run is: build (or reuse) the index → for every question, retrieve and
optionally answer → score deterministically → optionally score with RAGAS →
write a JSON file. Runs are kept on disk so the panel can show a leaderboard
across sessions; nothing here writes anywhere near board.db or the brain's own
Chroma database.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import corpus, embedding, metrics, models, pipeline, ragas_eval
from .config import RUNS_DIR, LabConfig, LabSettings
from .index import IndexRegistry, _lab_llm

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
        turn = llm.chat([{'role': 'system', 'content': KEY_FACTS_PROMPT},
                         {'role': 'user',
                          'content': f'Answer:\n{answer}\n\nFacts:\n{listing}'}],
                        model=model)
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

    def as_dict(self) -> dict:
        return {'run_id': self.run_id, 'label': self.label, 'config': self.config,
                'index': self.index, 'summary': self.summary, 'rows': self.rows,
                'ragas': self.ragas, 'seconds': self.seconds,
                'started_at': self.started_at, 'notes': self.notes}

    def brief(self) -> dict:
        """Leaderboard row — no per-question detail."""
        return {'run_id': self.run_id, 'label': self.label,
                'started_at': self.started_at, 'seconds': self.seconds,
                'config': self.config, 'summary': self.summary,
                'ragas': self.ragas.get('metrics', {}),
                'n_questions': self.summary.get('n_questions', 0)}


def select_questions(ground_truth: dict, types: list[str] | None = None,
                     limit: int | None = None,
                     difficulty: list[str] | None = None) -> list[dict]:
    questions = ground_truth['questions']
    if types:
        questions = [q for q in questions if q['type'] in set(types)]
    if difficulty:
        questions = [q for q in questions if q['difficulty'] in set(difficulty)]
    if limit:
        # Stride rather than truncate: the set is sorted by type, so the first N
        # would be twenty single-hop questions and nothing else.
        step = max(1, len(questions) // limit)
        questions = questions[::step][:limit]
    return questions


def run_eval(registry: IndexRegistry, ground_truth: dict, cfg: LabConfig,
             settings: LabSettings, *, types: list[str] | None = None,
             limit: int | None = None, difficulty: list[str] | None = None,
             ragas_mode: str = 'offline', ragas_limit: int | None = None,
             workers: int = 1, progress=None) -> RunResult:
    problems = cfg.validate()
    if problems:
        raise ValueError('; '.join(problems))
    started = time.time()
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + cfg.index.fingerprint()[:6]
    report = lambda stage, fraction: progress(stage, fraction) if progress else None

    index = registry.get(cfg.index, progress=lambda stage, f: report(stage, f * 0.4))
    questions = select_questions(ground_truth, types, limit, difficulty)
    query_date = ground_truth['meta'].get('query_date', '2026-07-28')
    llm = _lab_llm(settings)
    roles = models.resolve(cfg, settings)
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
    if not settings.openrouter_api_key and (cfg.generation.answerer == 'llm'
                                            or cfg.retrieval.reranker == 'llm'
                                            or cfg.retrieval.grader == 'llm'
                                            or cfg.retrieval.hyde
                                            or cfg.index.summarizer == 'llm'):
        notes.append('no OPENROUTER_API_KEY: LLM stages fell back to the offline '
                     'fake provider, so their numbers are meaningless')

    def handle(question: dict):
        outcome = pipeline.retrieve(index, cfg.retrieval, question['question_fa'],
                                    question.get('query_date', query_date),
                                    llm=llm, models=roles)
        outcome = pipeline.answer(outcome, cfg.generation, llm=llm, models=roles)
        row = metrics.score_question(question, outcome, cfg.retrieval.k)
        if (cfg.generation.key_facts_judge and outcome.answer
                and settings.openrouter_api_key and question.get('answerable')):
            row['key_fact_coverage'] = judge_key_facts(llm, roles.judge, question,
                                                       outcome.answer)
        return question, outcome, row

    pairs, rows = [], []
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
                slots[futures[future]] = future.result()
                done_count += 1
                report('scoring', 0.4 + 0.5 * done_count / len(questions))
        results = [row for row in slots if row is not None]
    else:
        for i, question in enumerate(questions):
            results.append(handle(question))
            report('scoring', 0.4 + 0.5 * (i + 1) / len(questions))
    for question, outcome, row in results:
        pairs.append((question, outcome))
        rows.append(json_safe(row))
    report('ragas', 0.92)

    summary = metrics.aggregate(rows)
    ragas_report: dict = {}
    if ragas_mode != 'off':
        sessions = corpus.sessions_by_id(registry.diary)
        references = {q['id']: corpus.evidence_texts(sessions, q) for q in questions}
        ragas_report = ragas_eval.run(pairs, settings, index.embedder,
                                      mode=ragas_mode, sample_limit=ragas_limit,
                                      reference_texts=references,
                                      judge_model=roles.ragas)
    report('done', 1.0)
    result = RunResult(run_id=run_id, label=cfg.label or cfg.index.chunker,
                       config=cfg.to_dict(),
                       index={'collection': index.stats.collection,
                              'chunks': index.stats.chunks,
                              'by_layer': index.stats.by_layer,
                              'avg_chars': index.stats.avg_chars,
                              'p95_chars': index.stats.p95_chars,
                              'embed_dim': index.stats.embed_dim,
                              'build_seconds': index.stats.build_seconds,
                              'reused': index.stats.reused},
                       summary=summary, rows=rows, ragas=ragas_report,
                       seconds=round(time.time() - started, 2),
                       started_at=time.strftime('%Y-%m-%d %H:%M:%S'), notes=notes)
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
        out.append({'run_id': data['run_id'], 'label': data.get('label', ''),
                    'started_at': data.get('started_at', ''),
                    'seconds': data.get('seconds', 0), 'config': data.get('config'),
                    'summary': data.get('summary', {}),
                    'ragas': (data.get('ragas') or {}).get('metrics', {}),
                    'n_questions': (data.get('summary') or {}).get('n_questions', 0)})
    return out


def load_run(run_id: str) -> dict | None:
    path = RUNS_DIR / f'{run_id}.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))
