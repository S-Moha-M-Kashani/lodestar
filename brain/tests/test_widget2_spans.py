"""The date-span maths behind the card reader's five date filters.

Three things here can go quietly wrong and each one answers the owner with a
wrong list rather than an error:

  1. **A boundary in the wrong time zone.** These are one person's local week,
     month and year. A week computed from a UTC midnight starts on Sunday
     evening for a reader in Europe, so "captured this week" would answer with
     Sunday's cards on a Monday morning — right shape, wrong week.
  2. **An end that stops at midnight.** A range ending today has to include
     what was finished an hour ago; an exclusive end drops the whole last day.
     And the mirror of it: an absent date must be *no* date, or a `doneAt` of
     `0` reports a card as finished in 1970.
  3. **A partial plan read as a point.** A board planned at year precision
     would answer "nothing planned" to every question about a month of that
     year — so a plan is the period it names, and it matches on overlap.

Every assert names one fixed moment, `datetime(2026, 9, 7, 10, 0)` — the
Monday `tests/timespan.test.js` measures from, chosen so the week, month and
year edges are all interesting — so this suite cannot change its answer
overnight. Card timestamps are built through `.timestamp()`, which reads the
machine's own zone exactly as the module does, so the local-time claim is
tested rather than the test's zone being assumed.

`spans.py` imports nothing of the agent's, which is what lets this run under
plain pytest with no board, model or graph.
"""
import pathlib
import re
from datetime import datetime

from lodestar_brain.widget2.tools.spans import (SPAN_PRESETS, in_span,
                                                plan_in_span, span_of)

#: The repo root, so the board's own preset list can be read rather than
#: retyped — `brain/tests/` is two levels down.
ROOT = pathlib.Path(__file__).resolve().parents[2]

# The Monday every span below is measured from.
NOW = datetime(2026, 9, 7, 10, 0)


def at(text: str) -> float:
    """A card timestamp: local epoch milliseconds, as server.js stores it."""
    return datetime.fromisoformat(text).timestamp() * 1000


# This is a unit test.
def test_each_preset_spans_its_own_local_period():
    # **Read off `js/core/timespan.js`, not written out here.** This module
    # claims to mirror that one, and the way a mirror breaks is a preset added
    # on one side — so asserting the Python list against a Python literal would
    # be a tautology dressed as a check. The two ids this tool has no use for
    # are excluded by name and for a reason each: `''` is the board's "any
    # time", which is the absence `span_of` answers `None` to, and `custom` is
    # the UI's word for two dates, which arrive here as `start`/`end`.
    block = re.search(r'RANGE_PRESETS = \[(.*?)\]',
                      (ROOT / 'js/core/timespan.js').read_text(), re.S)
    assert block, 'RANGE_PRESETS moved; this pin needs rewriting, not deleting'
    presets = re.findall(r"id: '([a-z0-9]*)'", block.group(1))
    assert set(SPAN_PRESETS) == set(presets) - {'', 'custom'}, (
        'the board offers a relative span this tool cannot answer')

    # This week: the local Monday, and NOW is that Monday.
    week = span_of(preset='week', now=NOW)
    assert week == (datetime(2026, 9, 7), datetime(2026, 9, 13, 23, 59, 59, 999999))
    # The boundary is the *local* Monday: at UTC+2 a week measured from a UTC
    # midnight would open at 02:00 local, and this card would fall outside the
    # week it was captured in.
    assert in_span(at('2026-09-07T00:30'), week)
    assert not in_span(at('2026-09-06T23:59'), week)

    assert span_of(preset='month', now=NOW) == (
        datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 59, 59, 999999))
    assert span_of(preset='year', now=NOW) == (
        datetime(2026, 1, 1), datetime(2026, 12, 31, 23, 59, 59, 999999))
    # Thirty whole days, today included — so it opens on the 9th of August,
    # twenty-nine days back, and closes at the end of today rather than at the
    # moment it was asked.
    assert span_of(preset='days30', now=NOW) == (
        datetime(2026, 8, 9), datetime(2026, 9, 7, 23, 59, 59, 999999))
    # A preset this build does not know is discarded rather than obeyed, and
    # the dates then decide — here there are none.
    assert span_of(preset='fortnight', now=NOW) is None


# This is a unit test.
def test_a_typed_range_takes_whole_days_and_either_end_may_be_open():
    # No preset and no dates narrows nothing: `None` and not an infinite span,
    # so the caller has one early return and the filter is inert by default.
    assert span_of(now=NOW) is None
    assert in_span(at('1999-01-01T00:00'), None)

    both = span_of(start='2026-03-01', end='2026-03-15', now=NOW)
    assert both == (datetime(2026, 3, 1), datetime(2026, 3, 15, 23, 59, 59, 999999))
    # The whole end day, which is what makes "up to today" usable at all.
    assert in_span(at('2026-03-15T23:00'), both)
    assert not in_span(at('2026-03-16T00:00'), both)
    # A deadline is a day, not a moment, and lands inside the day it names.
    assert in_span('2026-03-15', both)
    assert not in_span('2026-03-16', both)
    # An absent date is out of every span, never 1970: `doneAt` is 0 on a card
    # nobody has finished, and a deadline is '' on a card with none.
    assert not in_span(0, both)
    assert not in_span('', both)
    assert not in_span(None, both)
    assert not in_span('01/03/2026', both)

    # One end open — "since the first of March", "up to the first of March".
    since = span_of(start='2026-03-01', now=NOW)
    assert since[0] == datetime(2026, 3, 1) and since[1] == datetime.max
    until = span_of(end='2026-03-01', now=NOW)
    assert until[0] == datetime.min and until[1] == datetime(2026, 3, 1, 23, 59, 59, 999999)

    # A malformed or impossible date is no date, so a half-typed range narrows
    # nothing rather than narrowing to nonsense.
    assert span_of(start='2026-02-30', now=NOW) is None
    assert span_of(start='2026-3-1', now=NOW) is None

    # A start after its end is the contradiction the caller asked for, returned
    # as the empty span it is rather than silently swapped.
    back = span_of(start='2026-03-15', end='2026-03-01', now=NOW)
    assert back[0] > back[1]
    assert not in_span(at('2026-03-08T12:00'), back)


# This is a unit test.
def test_a_plan_matches_any_span_its_own_period_overlaps():
    march = span_of(start='2027-03-01', end='2027-03-15', now=NOW)
    # Year precision: the whole of 2027, so a question about one March week is
    # answered — the containment reading would answer "nothing planned" for a
    # board planned a year at a time.
    assert plan_in_span('2027', march)
    assert not plan_in_span('2028', march)
    # Month precision: March overlaps, February does not.
    assert plan_in_span('2027-03', march)
    assert not plan_in_span('2027-02', march)
    # Day precision: the day itself, and the boundary days are inside.
    assert plan_in_span('2027-03-15', march)
    assert not plan_in_span('2027-03-16', march)
    # And it holds the other way round: a span inside the plan's period, and a
    # plan inside the span's.
    assert plan_in_span('2027-03-04', span_of(preset='year', now=datetime(2027, 7, 1)))
    # No plan, or one too broken to read, is out of every span — the board's
    # own `planVal` has already coerced whatever it stored.
    assert not plan_in_span('', march)
    assert not plan_in_span(None, march)
    assert not plan_in_span('2027-02-30', march)
    assert plan_in_span('2027', None)
