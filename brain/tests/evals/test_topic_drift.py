"""Calibration for the topic-change nudge's threshold.

`topics.py` ships `DRIFT_DISTANCE` as an admitted guess. This is the measurement
that replaces it: labelled pairs scored against the **real** embedder, reporting
where same-subject and new-subject messages actually fall.

    uv run --project brain --extra local-embeddings \\
      pytest brain/tests/evals/test_topic_drift.py -v -m calibration -s

`-s` matters — the report printed at the end is the point of the run, not the
pass/fail. Skipped without the extra, because the offline suite deliberately has
no torch (see the root CLAUDE.md); a calibration that quietly ran against
`LexicalHashEmbeddings` would be measuring a hash, which is the category error
the route already guards against.

**The number is not recorded yet.** When this has been run, write the observed
figures into `topics.py`'s `Alternatives considered` note — the whole point of
that note is that the threshold stops being taste.

What is asserted, deliberately loosely: openers are caught without any model, and
the two labelled classes are separable at *some* cut-off. The exact value is a
judgement made from the report, not from an assertion, because the cost of the
two error directions is not symmetric — a false positive nags, a false negative
merely leaves today's behaviour.
"""
import importlib.util
import os

import pytest

from lodestar_brain.config import Settings
from lodestar_brain.retrieval import make_embeddings
from lodestar_brain.topics import DRIFT_DISTANCE, detect_drift, is_opener

# The board holds a life, so the fixtures do too — and in both scripts it is
# actually used in, because the embedder is a Persian model and a threshold
# calibrated on English alone would be calibrated on half the input.
SAME_SUBJECT = [
    (['help me plan the berlin trip in october',
      'which hotel near mitte should i book'],
     'what about the trains from the airport'),
    (['i need to file the tax return before friday',
      'which receipts count as deductible'],
     'and what happens if i file it late'),
    (['دیشب با هم دعوامون شد سر برنامه آخر هفته',
      'چطور شروع کنم حرف زدن دربارش'],
     'اگر بازم بحث شد چی بگم'),
    (['my back hurts after sitting all day',
      'what stretches help with lower back pain'],
     'how often should i do them'),
    (['i want to learn jazz piano properly this year',
      'should i start with voicings or standards'],
     'how much practice a day is realistic'),
    (['the landlord has not fixed the boiler for three weeks',
      'what are my rights as a tenant here'],
     'do i have to keep paying full rent meanwhile'),
]

NEW_SUBJECT = [
    (['help me plan the berlin trip in october',
      'which hotel near mitte should i book'],
     'i need to file the tax return before friday'),
    (['i need to file the tax return before friday',
      'which receipts count as deductible'],
     'what stretches help with lower back pain'),
    (['دیشب با هم دعوامون شد سر برنامه آخر هفته',
      'چطور شروع کنم حرف زدن دربارش'],
     'قیمت دلار امروز چند بود'),
    (['my back hurts after sitting all day',
      'what stretches help with lower back pain'],
     'should i learn jazz piano voicings or standards first'),
    (['i want to learn jazz piano properly this year',
      'should i start with voicings or standards'],
     'the boiler has been broken for three weeks'),
    (['the landlord has not fixed the boiler for three weeks',
      'what are my rights as a tenant here'],
     'plan the berlin trip for october'),
]

OPENERS = ['hi', 'hello', 'hey there', 'سلام', 'yo', 'hello again']

_HAS_REAL_EMBEDDER = importlib.util.find_spec('sentence_transformers') is not None


def _report(name, scores):
    lo, hi = min(scores), max(scores)
    mean = sum(scores) / len(scores)
    print(f'  {name:14s} n={len(scores)}  min={lo:.3f}  mean={mean:.3f}  max={hi:.3f}')
    return lo, hi


# This is a calibration.
@pytest.mark.calibration
@pytest.mark.skipif(
    not _HAS_REAL_EMBEDDER,
    reason='calibration: needs the real embedder — run with --extra local-embeddings')
def test_the_threshold_separates_labelled_pairs():
    settings = Settings(llm_provider='fake', embedder='sentence-transformers',
                        board_api_url='http://board.test', chroma_url='')
    embeddings = make_embeddings(settings.embedder, settings, settings.embed_model)

    same = [detect_drift(recent, incoming, embeddings).score
            for recent, incoming in SAME_SUBJECT]
    new = [detect_drift(recent, incoming, embeddings).score
           for recent, incoming in NEW_SUBJECT]

    print(f'\ncosine distance, model={settings.embed_model}')
    same_lo, same_hi = _report('same subject', same)
    new_lo, new_hi = _report('new subject', new)

    false_positives = [s for s in same if s >= DRIFT_DISTANCE]
    missed = [s for s in new if s < DRIFT_DISTANCE]
    fp_rate = len(false_positives) / len(same)
    print(f'  at DRIFT_DISTANCE={DRIFT_DISTANCE}: '
          f'false positives {len(false_positives)}/{len(same)} ({fp_rate:.0%}), '
          f'missed {len(missed)}/{len(new)}')
    if same_hi < new_lo:
        print(f'  SEPARABLE — any cut-off in ({same_hi:.3f}, {new_lo:.3f}); '
              f'midpoint {(same_hi + new_lo) / 2:.3f}')
    else:
        print(f'  OVERLAPPING by {same_hi - new_lo:.3f} — no cut-off is clean; '
              f'if this persists, the LLM call topics.py rejected has earned it')

    # The bar the module's own note names: above roughly one in ten, the nudge is
    # nagging rather than helping, and a classifier is the honest next step.
    assert fp_rate <= 0.10, (
        f'{fp_rate:.0%} of same-subject messages would be nudged at '
        f'DRIFT_DISTANCE={DRIFT_DISTANCE} — see the report above and either move '
        f'the threshold or take the classifier decision')
    # Loose on the other side on purpose: a missed drift costs nothing beyond the
    # behaviour that shipped before the nudge existed.
    assert len(missed) < len(new), (
        'not one new subject was detected — the signal is doing nothing')


# This is a unit test.
def test_openers_need_no_model_at_all():
    # In this file rather than test_topics.py because it is the claim the
    # calibration rests on: whatever the distance signal turns out to be worth,
    # the case the user actually reported is caught without an embedder — which
    # is also what lets the offline e2e drive the nudge.
    assert all(is_opener(text) for text in OPENERS)
    for recent, _ in SAME_SUBJECT:
        assert detect_drift(recent, 'hi', None).reason == 'opener'
