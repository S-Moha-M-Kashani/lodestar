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

    RAGLAB_LLM=ollama \\
    RAGLAB_SWEEP_ANSWER_MODEL=gemma4:e2b \\
    RAGLAB_SWEEP_JUDGE_MODEL=qwen3.5:2b \\
    npm run raglab:sweep -- --workers 2

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
# Overridable so a local pairing can be named without editing this file, which is
# what makes the expensive candidates measurable: F's per-chunk gate is k calls
# per question on top of the run. The rule the override must keep is the one
# above — two different models, and the *stronger* one judging. With Ollama that
# is currently gemma4:e2b answering and a small fast model judging, because the
# judge is ~276 calls to the answerer's 49.
ANSWER_MODEL = os.environ.get('RAGLAB_SWEEP_ANSWER_MODEL', 'openai/gpt-5-nano')
JUDGE_MODEL = os.environ.get('RAGLAB_SWEEP_JUDGE_MODEL', 'openai/gpt-5-mini')

# Every candidate is measured on the same 49 questions, balanced across the three
# difficulty bands (17 easy / 16 medium / 16 hard — 49 does not divide by three,
# so the remainder goes to the earlier bands; `--limit 51` gives exactly 17 each).
# The full 112 stay available for a final run, but a candidate sweep pays the
# judged cost per row, and a sample skewed toward medium — which is 57 of the 112
# — measures the medium pipeline and reports it as the pipeline.
SWEEP_LIMIT = 49
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
          f'answerer {ANSWER_MODEL}\n')
    scored = []
    for i, cfg in enumerate(picked, start=1):
        started = time.time()
        print(f'[{i}/{len(picked)}] {cfg.label} …', flush=True)
        result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                          balance=balance, ragas_mode='llm', ragas_limit=limit,
                          workers=workers)
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
    print(f'final run: {cfg.label} over {n} questions, {workers} workers',
          flush=True)
    result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                      balance=balance, ragas_mode='llm', ragas_limit=limit,
                      workers=workers)
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
