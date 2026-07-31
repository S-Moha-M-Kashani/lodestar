"""Sweep candidate architectures and rank them on the four deciding metrics.

Run as a module in the lab's own environment (the one `npm run raglab` builds,
because RAGAS and sentence-transformers live there):

    npm run raglab:sweep                     # the candidate sweep
    npm run raglab:sweep -- --final A        # re-run one candidate over all 112

Why this exists rather than clicking the panel: the panel runs one job at a time
and a judged sweep is a couple of hours of model calls. This drives the same
`evaluate.run_eval`, writes the same run files into `.runs/`, and therefore lands
in the same leaderboard — it is the panel's runner without the panel.

**Every candidate changes exactly one thing against the baseline.** A sweep whose
rows differ in three knobs each cannot attribute a win to any of them, which is
the failure that makes most tuning folklore rather than measurement.

To run it on models on this machine instead of a paid API — which is the only way
the expensive candidates get measured at all, since F's relevance gate is *k* LLM
calls per question:

    RAGLAB_LLM=ollama npm run raglab:sweep -- --workers 2

`RAGLAB_LLM` is enough: the answerer/judge pins default per provider (`PAIRINGS`),
because a slug only means something to the backend that serves it. Override either
one with `RAGLAB_SWEEP_ANSWER_MODEL` / `RAGLAB_SWEEP_JUDGE_MODEL`.

Screen the judge first (`npm run raglab:judgescreen`). A judge that answers the
same way to every claim scores every candidate identically, and the sweep cannot
tell you that — its rows would simply look tied.
"""
import argparse
import json
import os
import sys
import time
from dataclasses import replace

from . import corpus, ragas_eval
from .config import (BALANCES, GenerationConfig, IndexConfig, LabConfig,
                     RetrievalConfig, RUNS_DIR, load_lab_settings)
from .evaluate import run_eval
from .index import IndexRegistry

# The corpus is a Farsi diary, so the embedder is Persian-tuned. Held fixed
# across every candidate: it is the one choice that decides whether anything
# else is measurable at all, and varying it alongside the knobs would make each
# row differ in two things.
EMBEDDER = 'sentence-transformers'
EMBED_MODEL = 'heydariAI/persian-embeddings'

# The answerer and the judge are deliberately different models. A model grading
# its own output is not evidence, and RAGAS's four judged metrics are the whole
# basis of the ranking here.
#
# One pairing per provider, because a model slug only means something to the
# backend that serves it: `openai/gpt-5-nano` is not a thing Ollama can load, and
# a default that crosses the two would trip `models.provider_problems` on every
# local run. The local pair is a small fast model answering and a bigger one
# judging — the right way round, since the judge is the expensive side (~12 calls
# per question against the answerer's 1) and the side the whole ranking rests on.
PAIRINGS = {'openrouter': {'answerer': 'openai/gpt-5-nano',
                           'judge': 'openai/gpt-5-mini'},
            # Measured, and it decided this pairing: `deepseek-r1:8b` is the more
            # independent judge but it is a *reasoning* model — ~2065 output
            # tokens per verdict against gemma's ~534, so 67s per call against
            # 8.7s, which is 8.9 hours per candidate against 1.2. At that price
            # the sweep does not finish, and a sweep that does not finish ranks
            # nothing. The cost of this choice is stated rather than hidden: the
            # answerer and the judge are the same family, so their agreement is
            # weaker evidence than two unrelated models would give, and that
            # belongs on the row (`report['judge']` carries both).
            'ollama': {'answerer': '4skl/gemma4-e2b-mtp',
                       'judge': 'gemma4:e2b'},
            'fake': {'answerer': 'openai/gpt-5-nano',
                     'judge': 'openai/gpt-5-mini'}}

# Which provider the pins default to is read at import, so the env that chooses
# the backend also chooses the pairing. Still individually overridable, because a
# screened judge is a per-machine fact.
_PAIR = PAIRINGS.get(os.environ.get('RAGLAB_LLM', '') or 'openrouter',
                     PAIRINGS['openrouter'])
ANSWER_MODEL = os.environ.get('RAGLAB_SWEEP_ANSWER_MODEL', _PAIR['answerer'])
JUDGE_MODEL = os.environ.get('RAGLAB_SWEEP_JUDGE_MODEL', _PAIR['judge'])

# Every candidate is measured on the same 30 questions — 10 easy, 10 medium, 10
# hard. The full 112 stay available for a final run, but a candidate sweep pays
# the judged cost per row, and a sample skewed toward medium (57 of the 112, so
# about half of any stride) measures the medium pipeline and reports it as the
# pipeline. 30 divides by three, so this sample needs no remainder rule at all.
SWEEP_LIMIT = 30
SWEEP_BALANCE = 'difficulty'

BASE = LabConfig(
    index=IndexConfig(embedder=EMBEDDER, embed_model=EMBED_MODEL),
    retrieval=RetrievalConfig(),
    # Judged faithfulness and answer relevancy score a *response*, so the sweep
    # has to generate one: with answerer='none' all four deciding metrics are
    # undefined and nothing can be ranked.
    generation=GenerationConfig(answerer='llm', model=ANSWER_MODEL,
                                ragas_model=JUDGE_MODEL),
    label='A baseline')


def candidates() -> list[LabConfig]:
    """One hypothesis per row, each a single change against the baseline."""
    out = [BASE]

    def variant(label, *, index=None, retrieval=None):
        cfg = BASE
        if index:
            cfg = replace(cfg, index=replace(cfg.index, **index))
        if retrieval:
            cfg = replace(cfg, retrieval=replace(cfg.retrieval, **retrieval))
        out.append(replace(cfg, label=label))

    # Is the summary hierarchy earning its keep, or would raw chunks alone do?
    variant('B raw chunks only',
            index={'layers': ('chunk',)}, retrieval={'search_layers': ('chunk',)})
    # k moves precision and recall in opposite directions; both are deciding
    # metrics, so this is the one knob whose optimum the four cannot agree on.
    variant('C tighter context k=5', retrieval={'k': 5})
    variant('D wider context k=12', retrieval={'k': 12})
    # Small-to-big: retrieve precisely, then hand over the surrounding day.
    variant('E parent expansion', retrieval={'parent_expansion': 'session'})
    # A gate is the only way an answer can be refused, and refusing instead of
    # inventing is what faithfulness rewards.
    variant('F llm relevance gate', retrieval={'grader': 'llm',
                                               'grade_threshold': 0.4,
                                               'grader_model': ANSWER_MODEL})
    # The rollups — including the habit ledger — compete with twenty raw chunks
    # from the same day unless they are boosted before the candidate cut.
    variant('G rollups boosted', retrieval={'rollup_boost': 1.4})
    # One chunker alternative: whole sessions, maximum fidelity per hit.
    variant('H session chunks', index={'chunker': 'session'})
    return out


def score(result) -> float | None:
    return (result.ragas or {}).get('decision')


def line(label: str, result) -> str:
    metrics = (result.ragas or {}).get('metrics', {})
    overall = result.summary.get('overall', {})
    parts = ' '.join(
        f'{name.split("_")[0][:5]}={metrics.get(name)!s:>6}'
        for name in ragas_eval.DECISION_METRICS)
    return (f'{label:24} decision={str(score(result)):>7}  {parts}  '
            f'headline={overall.get("headline")} '
            f'recall={overall.get("recall")} '
            f'quote={overall.get("quote_recall")}  {result.seconds}s')


def judged_settings():
    """Settings, or exit — every run here is ranked on judged metrics.

    With no backend the LLM stages fall back to the offline fake provider, which
    answers and judges without failing. That produces a leaderboard of confident
    meaningless numbers, so both entry points refuse rather than measure.

    The test is whether a *real model* can be reached, not whether a credential
    exists: a judge served by Ollama on this machine needs no key, and the guard
    used to send anyone without one away from a run they could have made."""
    settings = load_lab_settings()
    if not settings.llm_ready:
        sys.exit('no LLM backend: the four deciding metrics are judged, so there '
                 'is nothing to rank without one. Set OPENROUTER_API_KEY, or '
                 'RAGLAB_LLM=ollama with RAGLAB_SWEEP_ANSWER_MODEL / '
                 'RAGLAB_SWEEP_JUDGE_MODEL naming two models it serves.')
    if ANSWER_MODEL == JUDGE_MODEL:
        sys.exit(f'answerer and judge are both {ANSWER_MODEL!r}: a model grading '
                 'its own output is not evidence, and these four metrics are the '
                 'whole basis of the ranking')
    return settings


BAR_WIDTH = 28


def bar(label: str, stage: str, fraction: float, detail: str,
        started: float) -> str:
    """One line, rewritten in place: where this candidate is and how long it has
    been there.

    Elapsed is on it because the fraction alone cannot tell a slow phase from a
    stuck one — on a local judge a single call is a minute, so a bar that has not
    moved for two is normal and one that has not moved for twenty is not."""
    filled = int(round(BAR_WIDTH * min(1.0, max(0.0, fraction))))
    mins, secs = divmod(int(time.time() - started), 60)
    tail = f' · {detail}' if detail else ''
    return (f'\r  {label[:18]:18} [{"█" * filled}{"·" * (BAR_WIDTH - filled)}] '
            f'{fraction * 100:5.1f}% {mins:>3}m{secs:02d}s  {stage}{tail}')


def live(label: str, started: float, stream=sys.stdout):
    """A progress callback that rewrites one terminal line.

    Padded to a fixed width and flushed per update: without the padding a short
    detail leaves the tail of a longer one behind it, which reads as a stale
    number rather than as a redraw artefact."""
    def report(stage: str, fraction: float, detail: str = '') -> None:
        stream.write(f'{bar(label, stage, fraction, detail, started):<118}')
        stream.flush()
    return report


def sweep(limit: int, workers: int, only: list[str] | None = None,
          balance: str = SWEEP_BALANCE) -> list[tuple]:
    settings = judged_settings()
    diary = corpus.load_diary()
    ground_truth = corpus.load_ground_truth()
    registry = IndexRegistry(settings, diary)

    picked = [c for c in candidates()
              if not only or c.label.split()[0] in only]
    print(f'{len(picked)} candidates · {limit} questions each ({balance}) · '
          f'{workers} workers · {settings.provider} · judge {JUDGE_MODEL} · '
          f'answerer {ANSWER_MODEL}')
    # What this is going to cost, before it starts rather than after. Context
    # precision is one judge call per retrieved chunk, so k moves this more than
    # anything else on the row.
    per_row = ragas_eval.expected_judge_calls(limit, BASE.retrieval.k)
    print(f'~{per_row} judge calls per candidate, ~{per_row * len(picked)} in '
          f'total — an estimate: RAGAS retries malformed output\n')
    scored = []
    for i, cfg in enumerate(picked, start=1):
        started = time.time()
        stage = cfg.label.split()[0]
        print(f'[{i}/{len(picked)}] {cfg.label} …', flush=True)
        result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                          balance=balance, ragas_mode='llm', ragas_limit=limit,
                          workers=workers,
                          progress=live(f'Stage {stage}', started))
        print()                              # close the rewritten bar line
        scored.append((score(result), cfg.label, result))
        print('   ' + line(cfg.label, result))
        print(f'   run {result.run_id} · {round(time.time() - started)}s\n',
              flush=True)

    ranked = sorted(scored, key=lambda row: (row[0] is None, -(row[0] or 0)))
    print('\n=== ranked by the RAGAS decision score '
          f'({", ".join(ragas_eval.DECISION_METRICS)}) ===')
    for value, label, result in ranked:
        print('   ' + line(label, result))
    return ranked


def final(limit: int | None, workers: int, label: str,
          balance: str = SWEEP_BALANCE) -> None:
    """Re-run one candidate over the whole question set.

    The winner is decided on a subset for cost; the number that goes in the
    document is measured on everything, because a per-type breakdown over two
    habit questions is not a breakdown."""
    settings = judged_settings()
    diary = corpus.load_diary()
    ground_truth = corpus.load_ground_truth()
    registry = IndexRegistry(settings, diary)
    cfg = next(c for c in candidates() if c.label.split()[0] == label)
    cfg = replace(cfg, label=f'WINNER {cfg.label} · full set')
    n = limit or len(ground_truth['questions'])
    started = time.time()
    print(f'final run: {cfg.label} over {n} questions, {workers} workers',
          flush=True)
    result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                      balance=balance, ragas_mode='llm', ragas_limit=limit,
                      workers=workers,
                      progress=live(f'Final {label}', started))
    print()
    print(line(cfg.label, result))
    print(f'run {result.run_id}')
    print(json.dumps({'decision': score(result),
                      'ragas': (result.ragas or {}).get('metrics'),
                      'overall': result.summary.get('overall'),
                      'by_type': result.summary.get('by_type')},
                     ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=SWEEP_LIMIT,
                        help='questions per candidate (default %(default)s, '
                             'balanced across the difficulty bands)')
    parser.add_argument('--balance', default=SWEEP_BALANCE, choices=BALANCES,
                        help='"difficulty" equalises easy/medium/hard; "stride" '
                             'samples the set as it is, which is what the runs '
                             'before 2026-07-31 used')
    parser.add_argument('--workers', type=int, default=6,
                        help='questions scored in parallel; the judged stages '
                             'are dominated by waiting on the model. Drop this '
                             'to 2–3 for a local model, which serves far fewer '
                             'concurrent requests than a remote API')
    parser.add_argument('--only', nargs='*',
                        help='candidate letters to run, e.g. --only A F')
    parser.add_argument('--final', metavar='LETTER',
                        help='re-run one candidate over the full question set')
    args = parser.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if args.final:
        final(None, args.workers, args.final, args.balance)
    else:
        sweep(args.limit, args.workers, args.only, args.balance)


if __name__ == '__main__':
    main()
