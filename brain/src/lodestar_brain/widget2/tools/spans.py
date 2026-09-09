"""The date-span maths behind the card reader's five date filters, mirroring
`js/core/timespan.js`.

The board's own range control offers a field, a preset and two dates, and
`spanOf` turns those into a span. This is that arithmetic, for the tool: a
question like "what did I finish this month" has to get the same answer from
the Assistant as it gets from the control, and the two ends cannot import each
other. Mirrored rather than shared, which is the call `server.js` makes for
`js/core/schedule.js`'s validator and `retrieval/projection.py` makes for
`js/lib/projection.js`, and for the same reason.

**Every span is local time, and every end is the end of its day.** The rule and
its argument are `timespan.js`'s: this board is one person's life in one place,
so a week that begins at a UTC midnight begins on Sunday evening for them, and
a range ending today that stops at midnight drops what was finished an hour
ago — which is the commonest thing anyone will ask of this filter. Every
datetime here is therefore naive and means local time, exactly as
`new Date(y, m, d)` is local by construction.

**A range that narrows nothing answers `None`**, never an infinite span, so the
caller has one early return and the filter is genuinely inert in its default
state — which is every call that does not mention a date. `in_span` and
`plan_in_span` take that `None` and pass everything, which is `matchesRange`'s
own two lines (`if (!span) return true; if (!when) return false;`): a card with
no finished date is outside every finished span, and that is not the same as
being outside a captured one.

**A partial plan is a period, not a point.** `2027` is the whole of that year,
`2027-03` the whole of that month, `2027-03-04` that day — `planStart` and
`planEnd` in `js/core/plan.js`. So a plan matches a span when the two
*overlap*, and a card planned for 2027 answers a question about any week of
2027. The alternative, comparing the plan's first day to the span, would answer
"nothing planned" for a board whose whole year is planned at year precision.
A plan too broken to read is not a match rather than an error, and it is not
repaired here: `server.js`'s own `planVal` has already coerced every stored
plan, so this end may be strict.

**The clock is injectable** (`now`), or the suite's answer changes overnight.

Three deliberate differences from `timespan.js`, each because the caller is
different:

- **The presets are the four real ones.** `''` and `custom` are positions of a
  dropdown; a tool argument says a preset *or* two dates, so the off position
  is an absent argument and `custom` is having named them.
- **A card's timestamp is epoch milliseconds, not a string.** `createdAt`,
  `updatedAt` and `doneAt` are `INTEGER` columns (`server.js`) and reach here
  as numbers; `deadline` and `plan` are the strings. `in_span` takes either,
  and `0` is the absence the board means by it — a `doneAt` of `0` is a card
  nobody has finished, not a card finished in 1970.
- **A day ends at `23:59:59.999999`**, the last microsecond rather than the
  last millisecond, because that is the end of the day at this language's
  resolution. Same instant to everything the board stores.

This module imports nothing of the agent's — the `plan.js` / `nesting.js` /
`timespan.js` rule, and what lets `test_widget2_spans.py` pin every boundary
under plain pytest with no board, model or graph.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Literal, get_args

#: The board's own relative spans, and the type a tool argument is declared as
#: so the model cannot name a fifth. `SPAN_PRESETS` is read off the type rather
#: than written beside it: two spellings of one list is the hazard `fields.py`
#: exists to end.
SpanPreset = Literal['week', 'month', 'days30', 'year']
SPAN_PRESETS: tuple[str, ...] = get_args(SpanPreset)

# ── Reading the board's two date shapes ─────────────────────────────────────
# Shape first, calendar second. The regex refuses what a lenient parser would
# accept ('20260301', '2026-3-1') and `fromisoformat` refuses the shape-valid
# impossibilities ('2026-02-30', '2026-13-01') — the same two-step round-trip
# `dayVal` uses in timespan.js, and for the same reason: a half-typed date must
# narrow nothing rather than narrow to nonsense.
_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_PLAN = re.compile(r'^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$')


def _day(text: object) -> date | None:
    """A calendar day from 'YYYY-MM-DD', or `None` for anything else."""
    if not isinstance(text, str) or not _DAY.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _opens(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def _closes(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 59, 59, 999999)


def plan_period(plan: object) -> tuple[date, date] | None:
    """The first and last day a plan covers — `planStart`/`planEnd` in
    js/core/plan.js. `None` when there is no plan to place.

    Public because `fields.py` needs the *first* day: the board refuses a plan
    whose window opens after the card was already due, and that refusal is an
    ordering test on this same value. Widget 1 computes the day again by hand
    (`_plan_error`, a dict keyed on the string's length), which is the second
    copy this module exists to avoid.
    """
    if not isinstance(plan, str):
        return None
    match = _PLAN.match(plan.strip())
    if not match:
        return None
    year, month, day = (int(part) if part else None for part in match.groups())
    if month is None:
        return date(year, 1, 1), date(year, 12, 31)
    if not 1 <= month <= 12:
        return None
    length = calendar.monthrange(year, month)[1]
    if day is None:
        return date(year, month, 1), date(year, month, length)
    if not 1 <= day <= length:
        return None
    return date(year, month, day), date(year, month, day)


def _moment(value: object) -> datetime | None:
    """A stored card date as a local moment, or `None` when the card has not
    got one. Epoch milliseconds for the three timestamps, 'YYYY-MM-DD' for a
    deadline — taken at the start of its day, which is the same answer as
    taking the whole day, since every span here is day-aligned."""
    if isinstance(value, str):
        day = _day(value)
        return _opens(day) if day else None
    if isinstance(value, (int, float)) and value:
        try:
            return datetime.fromtimestamp(value / 1000)
        except (OSError, OverflowError, ValueError):
            return None  # a clock value no calendar holds is no date at all
    return None


# ── The spans themselves ────────────────────────────────────────────────────

def span_of(*, preset: str | None = None, start: str | None = None,
            end: str | None = None, now: datetime | None = None
            ) -> tuple[datetime, datetime] | None:
    """The span a filter asks for, or `None` when it asks for nothing.

    A preset decides on its own; otherwise the two dates do, each read as a
    whole local day. An absent end is `datetime.min`/`datetime.max`, which is
    what makes "since the first of March" and "up to the first of March" both
    sayable — `timespan.js` says ±Infinity for the same reason.

    A start after its end comes back as the empty span it is: the caller asked
    a contradiction, and swapping the two would answer a question nobody asked.
    An unknown preset is discarded rather than obeyed, and the dates then
    decide — a span narrowed by a rule nothing on screen can name is worse than
    no span, and `SpanPreset` is what stops a model naming one at all.
    """
    today = (now or datetime.now()).date()

    if preset == 'week':
        # The local Monday, the ISO week the habits already count in.
        monday = today - timedelta(days=today.weekday())
        return _opens(monday), _closes(monday + timedelta(days=6))
    if preset == 'month':
        length = calendar.monthrange(today.year, today.month)[1]
        return _opens(today.replace(day=1)), _closes(today.replace(day=length))
    if preset == 'days30':
        # Thirty whole days with today the thirtieth, closing at the end of
        # today rather than at the moment it was asked: a card finished this
        # evening belongs to a span the reader thinks of as including today.
        return _opens(today - timedelta(days=29)), _closes(today)
    if preset == 'year':
        return _opens(date(today.year, 1, 1)), _closes(date(today.year, 12, 31))

    first, last = _day(start), _day(end)
    if first is None and last is None:
        return None
    return (_opens(first) if first is not None else datetime.min,
            _closes(last) if last is not None else datetime.max)


def in_span(value: object, span: tuple[datetime, datetime] | None) -> bool:
    """Does a card's own date fall inside the span?

    No span passes everything, and no date passes nothing — `matchesRange`'s
    two lines. "What did I finish in March" is a question about a moment the
    card either has or has not got.
    """
    if span is None:
        return True
    at = _moment(value)
    return at is not None and span[0] <= at <= span[1]


def plan_in_span(plan: object, span: tuple[datetime, datetime] | None) -> bool:
    """Does a card's plan fall inside the span?

    **Overlap, not containment**: a plan is the period it names, so it matches
    whenever any part of that period lies in the span. `2027` answers a
    question about a week of 2027, and a question about the whole of 2027 is
    answered by a card planned for one day of it.
    """
    if span is None:
        return True
    period = plan_period(plan)
    if period is None:
        return False
    return _opens(period[0]) <= span[1] and _closes(period[1]) >= span[0]


__all__ = ['SPAN_PRESETS', 'SpanPreset', 'in_span', 'plan_in_span',
           'plan_period', 'span_of']
