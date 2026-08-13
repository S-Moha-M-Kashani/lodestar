"""The time language in a question, resolved to a date range.

Time words are the most selective filter available — a board holds a year of
similar cards, and a date range cuts the pool before ranking starts. One
`TimeScope` object serves both halves of a hybrid search: `matches` is the
in-process filter BM25 and the card index apply, `where_clause` is the store's
half. Both are derived from the same object on purpose — if the two could
drift, hybrid fusion would compare two different candidate pools and call the
result a ranking.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .. import textnorm

# The date fields `card_document` writes. A card matches a time scope if it was
# either created or last touched inside it.
DATE_FIELDS = ('created_day', 'updated_day')

# Jalali month → (start month/day, end month/day) in the Gregorian year holding
# the *start* of that month. Mapped directly rather than through a Jalali
# conversion library: the mapping drifts by a day across the years a board
# spans, and the brain's dependency budget is worth more than that day.
JALALI_MONTHS = {
    'فروردین': ((3, 21), (4, 20)), 'اردیبهشت': ((4, 21), (5, 21)),
    'خرداد': ((5, 22), (6, 21)), 'تیر': ((6, 22), (7, 22)),
    'مرداد': ((7, 23), (8, 22)), 'شهریور': ((8, 23), (9, 22)),
    'مهر': ((9, 23), (10, 22)), 'آبان': ((10, 23), (11, 21)),
    'آذر': ((11, 22), (12, 21)), 'دی': ((12, 22), (1, 20)),
    'بهمن': ((1, 21), (2, 19)), 'اسفند': ((2, 20), (3, 20)),
}
SEASONS = {
    'بهار': ((3, 21), (6, 21)), 'تابستان': ((6, 22), (9, 22)),
    'تابستون': ((6, 22), (9, 22)), 'پاییز': ((9, 23), (12, 21)),
    'زمستان': ((12, 22), (3, 20)), 'زمستون': ((12, 22), (3, 20)),
}
# Measured 2026-08-13 over 31 English time expressions, anchored at that date:
# the branches this half targets, the Farsi branches with no English mirror, and
# ordinary phrasings neither targets. The table is in
# `brain/tests/evals/test_timescope_english.py`, which prints it.
#
# As shipped that morning it resolved 15 of the 31 and 13 of those were right.
# One was wrong — "last summer" asked in August returned the summer we were
# standing in, because `_window` reads "most recent" as "already started" and
# nothing read the word "last"; that is the shift in `_english_scope` below, made
# after the measurement, and it takes the figure to 14 of 31. One is coarse:
# "March 2025" falls through to the bare-year branch and returns all of 2025.
# The 16 misses are calendar-relative ("this week/month/year", "earlier this
# week", "today"), Gregorian month names ("in June", "since April"), spans that
# are not "<digits> months ago" ("past 3 months", "the last 6 months", "the last
# 30 days", "two weeks ago", "a couple of weeks ago", "the last few weeks"),
# weekdays ("last Tuesday"), "in the past year" and "last night".
#
# Precision is the weaker half and is not counted in that fraction: bare season
# nouns match anywhere, so 3 of 5 control phrases with no time language at all
# ("buy a winter coat", "spring cleaning the flat", "did the summer camp invoice
# arrive") filter the board to a season. Only 'fall' carries the determiner
# guard. A false positive *removes* good candidates, so this is the worse half to
# be weak in — see the note at the foot of the file for why it is not a
# one-liner.
#
# The half is deliberately not a mirror of the Farsi one. «پارسال تابستون»
# shifts a further year because the Persian year turns at Nowruz, while "last
# summer" means the most recent summer, whichever Persian year it fell in.
ENGLISH_SEASONS = {'spring': ((3, 21), (6, 21)), 'summer': ((6, 22), (9, 22)),
                   'autumn': ((9, 23), (12, 21)), 'fall': ((9, 23), (12, 21)),
                   'winter': ((12, 22), (3, 20))}
LAST_YEAR = ('پارسال', 'سال پیش', 'سال گذشته', 'سال قبل')


def _searchable(names: dict) -> dict:
    """Match on normalised text but report the properly spelled name: a question
    typed «اذر» must resolve, while the label shown back must not look folded."""
    out: dict = {}
    for name, window in names.items():
        out.setdefault(textnorm.normalize(name), (name, window))
    return out


_MONTHS = _searchable(JALALI_MONTHS)
_SEASONS = _searchable(SEASONS)


@dataclass(frozen=True)
class TimeScope:
    """A resolved date range, as the two ints the metadata carries."""
    from_int: int
    to_int: int
    label: str
    kind: str

    def matches(self, metadata: dict, fields: tuple[str, ...] = DATE_FIELDS) -> bool:
        """The in-process half of the filter, used by BM25 and the card index.
        `where_clause` is the store's half, and both are derived from this one
        object: if the two could drift, hybrid fusion would compare two
        different candidate pools and call the result a ranking."""
        return any(self.from_int <= (metadata.get(field) or 0) <= self.to_int
                   for field in fields)

    def as_dict(self) -> dict:
        return {'from': _to_iso(self.from_int), 'to': _to_iso(self.to_int),
                'label': self.label, 'kind': self.kind}


def _to_int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def _to_iso(value: int) -> str:
    return f'{value // 10000:04d}-{(value // 100) % 100:02d}-{value % 100:02d}'


def _window(anchor: date, start: tuple[int, int],
            end: tuple[int, int]) -> tuple[date, date]:
    """The [start, end] window whose start precedes `anchor` — the one that has
    already happened. Handles windows crossing new year (دی, زمستان)."""
    first = date(anchor.year, *start)
    last = date(anchor.year + (1 if end < start else 0), *end)
    if first > anchor:
        first, last = date(first.year - 1, *start), date(last.year - 1, *end)
    return first, last


def _scope(first: date, last: date, label: str, kind: str) -> TimeScope:
    return TimeScope(_to_int(first), _to_int(last), label, kind)


def _previous_month(anchor: date) -> tuple[date, date]:
    end = anchor.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def resolve_time_scope(question: str, today: date | None = None) -> TimeScope | None:
    """A date range from the question's own time language, or None when it has
    none. Time words are the most selective filter available — a board holds a
    year of similar cards, and a date range cuts the pool before ranking starts.

    Returns the most recent matching window at or before `today`: «آذر» means
    the آذر that has already happened."""
    anchor = today or datetime.now(timezone.utc).date()
    text = textnorm.normalize(question)
    words = set(textnorm.tokens(text, drop_stopwords=False))
    shift_year = any(phrase in text for phrase in LAST_YEAR)

    def shifted(start, end, years=1):
        first, last = _window(anchor, start, end)
        return (date(first.year - years, *start), date(last.year - years, *end))

    for key, (label, (start, end)) in _SEASONS.items():
        if key in text:
            first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
            return _scope(first, last,
                          f'{label}{" پارسال" if shift_year else ""}', 'season')
    for key, (label, (start, end)) in _MONTHS.items():
        if key in words:
            first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
            return _scope(first, last, label, 'jalali-month')
    if 'نوروز' in text or 'عید' in words:
        start, end = (3, 18), (4, 4)
        first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
        return _scope(first, last, 'نوروز', 'holiday')

    months_back = re.search(r'(\d+)\s*ماه\s*(?:پیش|قبل|گذشته|اخیر)', text)
    if months_back:
        span = int(months_back.group(1)) * 30
        return _scope(anchor - timedelta(days=span), anchor,
                      f'{span} روز اخیر', 'relative')
    if re.search(r'(هفته|هفتهٔ)\s*(پیش|قبل|گذشته)', text):
        return _scope(anchor - timedelta(days=10), anchor, 'هفته گذشته', 'relative')
    if re.search(r'ماه\s*(پیش|قبل|گذشته)', text):
        return _scope(*_previous_month(anchor), 'ماه گذشته', 'relative')
    if 'دیروز' in words:
        return _scope(anchor - timedelta(days=1), anchor - timedelta(days=1),
                      'دیروز', 'relative')
    if any(phrase in text for phrase in ('اخیرا', 'این چند وقت', 'این روزا',
                                         'این مدت')):
        return _scope(anchor - timedelta(days=60), anchor, 'اخیرا', 'relative')
    if shift_year:
        # پارسال is Nowruz to Nowruz, not January to January.
        return _scope(date(anchor.year - 1, 3, 21), date(anchor.year, 3, 20),
                      'پارسال', 'relative')

    english = _english_scope(text.lower(), anchor)
    if english:
        return english
    explicit = re.search(r'\b(20\d\d)\b', text)
    if explicit:
        year = int(explicit.group(1))
        return TimeScope(year * 10000 + 101, year * 10000 + 1231, str(year),
                         'gregorian-year')
    return None


def _english_scope(text: str, anchor: date) -> TimeScope | None:
    """The English half. Same rule as the Farsi one — the most recent window
    that has already happened — with one guard: bare 'fall' is a verb often
    enough that it needs a determiner, and a false positive on a time filter
    *removes* good candidates, which is worse than missing the filter."""
    for name, (start, end) in ENGLISH_SEASONS.items():
        pattern = (r'\b(?:last|this|in|during)\s+(?:the\s+)?fall\b'
                   if name == 'fall' else rf'\b{name}\b')
        if re.search(pattern, text):
            first, last = _window(anchor, start, end)
            # "last summer" asked in August is not the summer we are standing
            # in. `_window` returns the window that has already *started*, which
            # in season is the current one, and "last" is the one word that says
            # it is not — so the shift applies only while the window is still
            # running (`last >= anchor`, and `_window` guarantees the other
            # side). Asked in March, "last summer" is already a finished summer
            # and nothing shifts, which is why the bug survived the unit test.
            if last >= anchor and re.search(rf'\blast\s+(?:the\s+)?{name}\b',
                                            text):
                return _scope(date(first.year - 1, *start),
                              date(last.year - 1, *end), f'last {name}',
                              'season')
            return _scope(first, last, name, 'season')
    if re.search(r'\byesterday\b', text):
        return _scope(anchor - timedelta(days=1), anchor - timedelta(days=1),
                      'yesterday', 'relative')
    months_back = re.search(r'\b(\d+)\s+months?\s+ago\b', text)
    if months_back:
        span = int(months_back.group(1)) * 30
        return _scope(anchor - timedelta(days=span), anchor,
                      f'last {span} days', 'relative')
    if re.search(r'\blast\s+week\b', text):
        return _scope(anchor - timedelta(days=10), anchor, 'last week', 'relative')
    if re.search(r'\blast\s+month\b', text):
        return _scope(*_previous_month(anchor), 'last month', 'relative')
    if re.search(r'\blast\s+year\b', text):
        # Unlike پارسال: an English year runs January to January.
        year = anchor.year - 1
        return TimeScope(year * 10000 + 101, year * 10000 + 1231, 'last year',
                         'relative')
    if re.search(r'\b(recently|lately)\b', text):
        return _scope(anchor - timedelta(days=60), anchor, 'recently', 'relative')
    return None


def where_clause(scope: TimeScope | None,
                 fields: tuple[str, ...] = DATE_FIELDS) -> dict | None:
    """The store's half of the filter, in Chroma's operator dialect. One clause
    per date field, OR'd: a card created before the window but updated inside it
    is a card the window is about."""
    if scope is None:
        return None
    clauses = [{'$and': [{field: {'$gte': scope.from_int}},
                         {field: {'$lte': scope.to_int}}]} for field in fields]
    return clauses[0] if len(clauses) == 1 else {'$or': clauses}


"""Alternatives considered

**"Why is the time filter yours? `dateparser` exists, and it speaks Farsi."**

*Short answer.* Because a retrieval filter needs a *range* and a date parser
returns a *point*, and because half of what has to be understood here
(«پارسال پاییز», «این چند وقت») is not a date at all.

*Why the obvious option fails.* `dateparser` genuinely handles Jalali dates and
Persian relative expressions, so this is not a coverage argument. It is a shape
argument: «آذر» has to become 2025-11-22 … 2025-12-21, and a parser that returns
one datetime leaves the caller to invent the granularity. Get that wrong and the
filter is a single-day window over a month-long question, which does not error —
it silently returns nothing and reads as "retrieval is broken".

*Why not the framework.* LangChain has `SelfQueryRetriever`, which asks an LLM
to write the metadata filter. That is the framework's answer to this problem and
it is a real one — but it costs a model call per query, it can emit a filter over
fields that do not exist, and a wrong filter deletes evidence before ranking
sees it. A deterministic resolver is testable and free; the seams where the
framework is taken as it comes are listed in `embeddings.py`.

*The libraries that would do it.* `dateparser` — the pick for absolute dates in
many formats, and worth adopting for that branch alone if a board starts
collecting them. `jdatetime` or `khayyam` — correct Jalali arithmetic, no
language understanding, which would replace the hard-coded month table and
nothing else. Facebook's `duckling` — best-in-class range extraction and it
returns grain, so it solves the actual problem; it is a JVM service, which is
the wrong deployment shape for a local-first single-user app. spaCy `DATE`
entities — no trained Persian pipeline.

*Why not adopted, and what would change it.* The month table drifts by about a
day against a true Jalali conversion across the years a board spans, which is
inside the tolerance of a filter whose windows are 30 days wide — and
`jdatetime` would fix only that day. `duckling` is the one that would genuinely
be better, and what would change the decision is deployment: if the brain ever
runs beside other services anyway, its grain-aware ranges beat this module's
hand-written branches, and the English half is the first thing it should
replace — now with a number behind that sentence rather than an admission.

*The English half, measured.* 2026-08-13, anchored at that date, over 31 English
time expressions and 5 controls carrying no time language: the branches this half
targets, the Farsi branches it has no mirror for, and phrasings neither targets
(«this month», «in June», «past 3 months», «two weeks ago», «last Tuesday»). No
model and no key — a deterministic parser is checkable offline, so this is
arithmetic over a table rather than a proxy. Provenance and the misses are listed
beside `ENGLISH_SEASONS`; the run is
`brain/tests/evals/test_timescope_english.py`. **As shipped: 15 of 31 resolved,
13 of them right.** One wrong ("last summer" in August returned the summer in
progress) was fixed in the same change and is the difference between 13 and 14;
the other 16 return nothing, which costs selectivity and no correctness.

*What that says about which library to reach for.* The 16 misses are recall, and
recall here is cheap to buy by hand — each is one branch, and `dateparser` would
close most of them tomorrow for a dependency. The controls are the argument for
`duckling`: 3 of the 5 fired, because bare season nouns match anywhere and "buy a
winter coat" is a noun phrase, not a date. Requiring a determiner (the guard
'fall' already carries) is not the fix — «over the summer» and «notes from the
autumn» need one list of prepositions and «summer 2025» needs none — and a
keyword screen cannot tell a season used as a time from a season used as an
adjective. A grain-aware parse can. What would change the decision is a board
that types English as often as it types Farsi: today it does not, and the cost of
these false positives is paid on the English half of a mostly-Persian corpus."""
