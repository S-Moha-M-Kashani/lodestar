"""Calibration for the English half of the time filter.

`timescope.py` said for months that its English half was "new and unmeasured".
This is the measurement that replaced that sentence: a phrase table, one anchor
date, and the verdict the shipped parser returns for each row.

    uv run --project brain pytest brain/tests/evals/test_timescope_english.py \\
      -v -m calibration -s

`-s` matters — the table printed at the end is the point of the run, not the
pass/fail. Unlike the drift calibration next door this needs no model, no key and
no extra: `resolve_time_scope` is a deterministic parser, so its correctness is
arithmetic over a table and a scripted stand-in would be standing in for nothing.

**How the phrases were chosen**, because a coverage number is only as honest as
its set. Three sources, and only the first is flattering: (1) the expressions the
English branches visibly target — seasons, "yesterday", "N months ago", "last
week/month/year", "recently"; (2) the Farsi branches that have no English mirror,
asked in English; (3) ordinary phrasings neither targets, written down before
running anything — calendar-relative ("this month"), Gregorian month names ("in
June", "since April"), spans that are not "<digits> months ago" ("past 3 months",
"two weeks ago"), weekdays, "today", "last night". Five controls carry no time
language at all, three of them containing a season noun used as an adjective,
because on a filter that *removes* candidates a false positive costs more than a
miss and a set with no controls cannot see one.

**The anchor is 2026-08-13 and it is load-bearing**, not just a defence against
rot. It is mid-summer, which is the only time of year at which "last summer" can
be told apart from "the most recent summer that has started" — the bug this run
found, fixed in the same change, invisible at the March anchor the unit test uses.

The measurement is in `timescope.py` beside `ENGLISH_SEASONS`, where the
"unmeasured" comment used to be. What is asserted here is every row of the table:
a change that resolves one of the sixteen misses, or loses one of the fourteen it
gets right, fails with the phrase named.
"""
from datetime import date

import pytest

from lodestar_brain.retrieval import resolve_time_scope

# Mid-summer, and fixed: a time parser tested against "now" measures nothing
# twice.
ANCHOR = date(2026, 8, 13)

# (phrase, the window a person typing it on ANCHOR means, verdict as measured).
# The verdict is what the parser does, not what it should do — 'miss' rows keep
# their intended window so that a branch added later is checked against a
# judgement made before it existed, rather than against itself.
#   exact  — resolves to the intended window
#   coarse — resolves to a strict superset of it (selectivity lost, nothing else)
#   wrong  — resolves to something else (evidence deleted before ranking)
#   miss   — no time scope at all
PHRASES = [
    # The branches the English half targets.
    ('how was last summer', (20250622, 20250922), 'exact'),
    ('what happened this spring', (20260321, 20260621), 'exact'),
    ('notes from last winter', (20251222, 20260320), 'exact'),
    ('anything from the autumn', (20250923, 20251221), 'exact'),
    ('what did I do in the fall', (20250923, 20251221), 'exact'),
    ('over the summer', (20260622, 20260922), 'exact'),
    ('what did I do yesterday', (20260812, 20260812), 'exact'),
    ('what changed 3 months ago', (20260515, 20260813), 'exact'),
    ('what happened last week', (20260803, 20260813), 'exact'),
    ('what did I do last month', (20260701, 20260731), 'exact'),
    ('how did last year go', (20250101, 20251231), 'exact'),
    ('what have I been up to recently', (20260614, 20260813), 'exact'),
    ('anything lately', (20260614, 20260813), 'exact'),
    ('anything from 2025', (20250101, 20251231), 'exact'),
    # Calendar-relative: the Farsi half has no mirror for these either.
    ("what's on this week", (20260810, 20260813), 'miss'),
    ('what did I do this month', (20260801, 20260813), 'miss'),
    ('what happened this year', (20260101, 20260813), 'miss'),
    ('earlier this week', (20260810, 20260813), 'miss'),
    ('what did I do today', (20260813, 20260813), 'miss'),
    ('what did I do last night', (20260812, 20260812), 'miss'),
    ('what happened last Tuesday', (20260811, 20260811), 'miss'),
    # Gregorian month names. The Jalali months are a table; these are not.
    ('what did I write in June', (20260601, 20260630), 'miss'),
    ('since April', (20260401, 20260813), 'miss'),
    # A month named beside a year reaches the bare-year branch, which answers
    # with the whole year — a superset, so it costs selectivity and no evidence.
    ('anything from March 2025', (20250301, 20250331), 'coarse'),
    # Spans. Only "<digits> months ago" is a branch; the rest of how a person
    # says the same thing is not.
    ('past 3 months', (20260515, 20260813), 'miss'),
    ('the last 6 months', (20260214, 20260813), 'miss'),
    ('the last 30 days', (20260714, 20260813), 'miss'),
    ('the last few weeks', (20260723, 20260813), 'miss'),
    ('two weeks ago', (20260730, 20260813), 'miss'),
    ('a couple of weeks ago', (20260730, 20260813), 'miss'),
    ('in the past year', (20250813, 20260813), 'miss'),
]

# No time language at all. A scope here is a filter over a question that never
# asked for one, and it removes candidates rather than adding any — which is why
# the module guards 'fall' and why the other four season nouns being unguarded is
# the finding this list exists to record.
CONTROLS = [
    ('what should I work on', False),
    ('can I fall back on the savings plan', False),   # the determiner guard
    ('spring cleaning the flat', True),
    ('did the summer camp invoice arrive', True),
    ('buy a winter coat', True),
]


def _verdict(scope, intended):
    if scope is None:
        return 'miss'
    got, want = (scope.from_int, scope.to_int), intended
    if got == want:
        return 'exact'
    return 'coarse' if got[0] <= want[0] and got[1] >= want[1] else 'wrong'


# This is a calibration.
@pytest.mark.calibration
def test_the_english_half_resolves_fourteen_of_thirty_one_phrases():
    """Coverage and precision of the English branches, as a fraction.

    The finding: recall is the half that is merely thin — sixteen phrases return
    nothing, each costing selectivity and no correctness — while precision is the
    half that is wrong, because a bare season noun matches wherever it appears
    and three of five no-time controls filter the board to a season.
    """
    verdicts = [(phrase, want, recorded,
                 _verdict(resolve_time_scope(phrase, ANCHOR), want))
                for phrase, want, recorded in PHRASES]
    tally = {name: sum(1 for *_, got in verdicts if got == name)
             for name in ('exact', 'coarse', 'wrong', 'miss')}
    fired = [phrase for phrase, _ in CONTROLS
             if resolve_time_scope(phrase, ANCHOR) is not None]

    print(f'\n{len(PHRASES)} English time expressions, anchored {ANCHOR}')
    for phrase, want, recorded, got in verdicts:
        scope = resolve_time_scope(phrase, ANCHOR)
        shown = f'{scope.from_int}..{scope.to_int}' if scope else '—'
        print(f'  {got:6s} {phrase:35s} {shown:19s} '
              f'(meant {want[0]}..{want[1]})')
    print(f'  resolves {len(PHRASES) - tally["miss"]} of {len(PHRASES)}, '
          f'{tally["exact"]} of them right; {tally["coarse"]} coarse, '
          f'{tally["wrong"]} wrong, {tally["miss"]} unresolved')
    print(f'  controls: {len(fired)} of {len(CONTROLS)} questions with no time '
          f'language got a filter anyway — {", ".join(fired)}')

    # Every row, so a change is reported as the phrase it moved rather than as a
    # number that drifted. A miss turning into an exact is still a failure here:
    # it is a new measurement, and the figures in timescope.py have to follow it.
    drifted = [f'{phrase!r}: recorded {recorded}, measured {got}'
               for phrase, _, recorded, got in verdicts if recorded != got]
    assert not drifted, ('the English half no longer behaves as measured — '
                         'update timescope.py\'s figures with this run:\n  '
                         + '\n  '.join(drifted))

    # The headline, pinned separately from the rows because it is the sentence
    # written into the module: fourteen of thirty-one, and nothing wrong.
    assert (tally['exact'], tally['miss']) == (14, 16)
    assert tally['wrong'] == 0, (
        'a phrase now resolves to a window it does not mean, which deletes '
        'evidence before ranking — the worst failure this table can show')

    # And the precision gap, closed. A false positive on a filter that
    # *removes* candidates costs more than a miss: "buy a winter coat"
    # narrowing the board to December-March hides answers that exist, where an
    # unresolved phrase merely fails to narrow. The three flagged True above
    # are what fired when this was first measured, kept as the record of what
    # the guard fixed; the determiner guard 'fall' already carried belongs on
    # all five seasons, so the number asserted here is zero.
    assert not fired, (
        f'{len(fired)} of {len(CONTROLS)} questions with no time language got '
        f'a filter anyway: {", ".join(fired)}; measured 2026-08-13: '
        + ', '.join(phrase for phrase, spurious in CONTROLS if spurious))

    # And narrowing precision must not have cost coverage — every phrase that
    # resolved before still resolves. Pinned again here, beside the assertion
    # that motivates the edit, because the tempting way to pass the line above
    # is an alternation too narrow for "anything from the autumn".
    assert tally['exact'] == 14, (
        f'coverage moved: {tally["exact"]} of {len(PHRASES)} now exact')


if __name__ == '__main__':
    pytest.main([__file__, '-s'])
