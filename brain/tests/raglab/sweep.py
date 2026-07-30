"""Sweep candidate architectures and rank them on the four deciding metrics.

Run as a module in the lab's own environment (the one `npm run raglab` builds,
because RAGAS and sentence-transformers live there):

    npm run raglab:sweep                 # the candidate sweep
    npm run raglab:sweep -- --final <n>  # re-run the winner over n questions

Why this exists rather than clicking the panel: the panel runs one job at a time
and a judged sweep is a couple of hours of model calls. This drives the same
`evaluate.run_eval`, writes the same run files into `.runs/`, and therefore lands
in the same leaderboard — it is the panel's runner without the panel.

**Every candidate changes exactly one thing against the baseline.** A sweep whose
rows differ in three knobs each cannot attribute a win to any of them, which is
the failure that makes most tuning folklore rather than measurement.
"""
import argparse
import json
import sys
import time
from dataclasses import replace

from . import corpus, ragas_eval
from .config import (GenerationConfig, IndexConfig, LabConfig, RetrievalConfig,
                     RUNS_DIR, load_lab_settings)
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
ANSWER_MODEL = 'openai/gpt-5-nano'
JUDGE_MODEL = 'openai/gpt-5-mini'

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


def sweep(limit: int, workers: int, only: list[str] | None = None) -> list[tuple]:
    settings = load_lab_settings()
    if not settings.openrouter_api_key:
        sys.exit('OPENROUTER_API_KEY is required: the four deciding metrics are '
                 'judged, so there is nothing to rank without it')
    diary = corpus.load_diary()
    ground_truth = corpus.load_ground_truth()
    registry = IndexRegistry(settings, diary)

    picked = [c for c in candidates()
              if not only or c.label.split()[0] in only]
    print(f'{len(picked)} candidates · {limit} questions each · '
          f'{workers} workers · judge {JUDGE_MODEL} · answerer {ANSWER_MODEL}\n')
    scored = []
    for i, cfg in enumerate(picked, start=1):
        started = time.time()
        print(f'[{i}/{len(picked)}] {cfg.label} …', flush=True)
        result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                          ragas_mode='llm', ragas_limit=limit, workers=workers)
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


def final(limit: int | None, workers: int, label: str) -> None:
    """Re-run one candidate over the whole question set.

    The winner is decided on a subset for cost; the number that goes in the
    document is measured on everything, because a per-type breakdown over two
    habit questions is not a breakdown."""
    settings = load_lab_settings()
    diary = corpus.load_diary()
    ground_truth = corpus.load_ground_truth()
    registry = IndexRegistry(settings, diary)
    cfg = next(c for c in candidates() if c.label.split()[0] == label)
    cfg = replace(cfg, label=f'WINNER {cfg.label} · full set')
    n = limit or len(ground_truth['questions'])
    print(f'final run: {cfg.label} over {n} questions, {workers} workers',
          flush=True)
    result = run_eval(registry, ground_truth, cfg, settings, limit=limit,
                      ragas_mode='llm', ragas_limit=limit, workers=workers)
    print(line(cfg.label, result))
    print(f'run {result.run_id}')
    print(json.dumps({'decision': score(result),
                      'ragas': (result.ragas or {}).get('metrics'),
                      'overall': result.summary.get('overall'),
                      'by_type': result.summary.get('by_type')},
                     ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=24,
                        help='questions per candidate (strided across types)')
    parser.add_argument('--workers', type=int, default=6,
                        help='questions scored in parallel; the judged stages '
                             'are dominated by waiting on the model')
    parser.add_argument('--only', nargs='*',
                        help='candidate letters to run, e.g. --only A F')
    parser.add_argument('--final', metavar='LETTER',
                        help='re-run one candidate over the full question set')
    args = parser.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if args.final:
        final(None, args.workers, args.final)
    else:
        sweep(args.limit, args.workers, args.only)


if __name__ == '__main__':
    main()
