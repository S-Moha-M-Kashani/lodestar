"""Query understanding: Farsi time expressions, query expansion, HyDE.

A diary is queried in relative time. Nobody asks "what happened between
2025-11-22 and 2025-12-21"; they ask «آذر چی شد» or «پارسال پاییز حالم چطور
بود». Those words are the most selective filter available — the corpus contains
a year of near-identical complaints, and a date range cuts the candidate pool by
90% before ranking even starts. Resolving them is therefore not a nicety; it is
the difference between a temporal question being answerable and not.

Persian calendar months are mapped to their Gregorian windows directly rather
than through a Jalali conversion library, because the mapping only drifts by a
day across the years this corpus spans and a dependency-free lab is worth more
than that day.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import textnorm

# Jalali month → (start month/day, end month/day) in the Gregorian year that
# contains the *start* of that month.
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


def _searchable(names: dict) -> dict:
    """Match on normalised text but report the properly spelled name.

    Matching has to happen after normalisation — a question typed «اذر» or
    «پائیز» must still resolve — but the label goes back to the panel, and
    showing the folded spelling there looks like a bug."""
    out: dict = {}
    for name, window in names.items():
        out.setdefault(textnorm.normalize(name), (name, window))
    return out


_MONTHS = _searchable(JALALI_MONTHS)
_SEASONS = _searchable(SEASONS)
LAST_YEAR = ('پارسال', 'پارسال', 'سال پیش', 'سال گذشته', 'سال قبل')

# Paraphrases the diarist and his questions genuinely alternate between. Used by
# deterministic multi-query expansion: cheap recall for lexical retrieval, which
# otherwise misses «همسرم» in a corpus that only ever writes «مهسا».
SYNONYMS = {
    'همسرم': ('مهسا',), 'زنم': ('مهسا',), 'مهسا': ('همسرم',),
    'مادرم': ('مامان',), 'مامان': ('مادرم',), 'پدرم': ('بابا',),
    'شغل': ('کار', 'جاب'), 'کار': ('شغل',), 'استخدام': ('آفر', 'قبول'),
    'بحث': ('دعوا',), 'دعوا': ('بحث', 'قهر'),
    'مالیات': ('اداره مالیات', 'جریمه'), 'ورزش': ('باشگاه',),
    'اپلای': ('درخواست', 'رزومه'), 'ریجکت': ('جواب رد', 'قبول نشدم'),
    'خونه': ('آپارتمان', 'اجاره'), 'خواب': ('بیخوابی', 'بی خوابی'),
}
QUESTION_WORDS = frozenset("""
چی چه چرا چطور چگونه کجا کِی کی چند چقدر آیا بگو بهم راجب درباره درمورد هست بود
شد کردم دادم گفتم میشه بود؟ کدوم کدام حالم وضعیت
""".split())


@dataclass(frozen=True)
class TimeScope:
    from_int: int
    to_int: int
    label: str
    kind: str

    def as_dict(self) -> dict:
        return {'from': _to_iso(self.from_int), 'to': _to_iso(self.to_int),
                'label': self.label, 'kind': self.kind}


def _to_int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def _to_iso(value: int) -> str:
    return f'{value // 10000:04d}-{(value // 100) % 100:02d}-{value % 100:02d}'


def _window(anchor: date, start: tuple[int, int], end: tuple[int, int],
            wrap_year: int | None = None) -> tuple[date, date]:
    """Build the [start, end] window whose start month/day precedes `anchor`,
    handling windows that cross new year (دی, زمستان)."""
    year = wrap_year if wrap_year is not None else anchor.year
    first = date(year, *start)
    last_year = year + 1 if (end[0], end[1]) < (start[0], start[1]) else year
    last = date(last_year, *end)
    if first > anchor:
        first = date(first.year - 1, *start)
        last = date(last.year - 1, *end)
    return first, last


def resolve_time_scope(question: str, query_date: str) -> TimeScope | None:
    """Extract a date range from Farsi time language, or None when the question
    is not time-scoped. Returns the most recent matching window at or before
    `query_date` — «آذر» means the آذر that has already happened."""
    text = textnorm.normalize(question)
    anchor = date(*(int(p) for p in query_date.split('-')))  # type: ignore[arg-type]
    words = set(textnorm.tokens(text, drop_stopwords=False))
    shift_year = any(phrase in text for phrase in LAST_YEAR)

    for key, (label, (start, end)) in _SEASONS.items():
        if key in text:
            first, last = _window(anchor, start, end)
            if shift_year:
                first, last = date(first.year - 1, *start), date(last.year - 1, *end)
            return TimeScope(_to_int(first), _to_int(last),
                             f'{label}{" پارسال" if shift_year else ""}', 'season')

    for key, (label, (start, end)) in _MONTHS.items():
        if key in words:
            first, last = _window(anchor, start, end)
            if shift_year:
                first, last = date(first.year - 1, *start), date(last.year - 1, *end)
            return TimeScope(_to_int(first), _to_int(last), label, 'jalali-month')

    if 'نوروز' in text or 'عید' in words:
        first, last = _window(anchor, (3, 18), (4, 4))
        if shift_year:
            first, last = date(first.year - 1, 3, 18), date(last.year - 1, 4, 4)
        return TimeScope(_to_int(first), _to_int(last), 'نوروز', 'holiday')

    months_back = re.search(r'(\d+)\s*ماه\s*(?:پیش|قبل|گذشته|اخیر)', text)
    if months_back:
        span = int(months_back.group(1)) * 30
        return TimeScope(_to_int(anchor - timedelta(days=span)), _to_int(anchor),
                         f'{span} روز اخیر', 'relative')
    if re.search(r'(هفته|هفتهٔ)\s*(پیش|قبل|گذشته)', text):
        return TimeScope(_to_int(anchor - timedelta(days=10)), _to_int(anchor),
                         'هفته گذشته', 'relative')
    if re.search(r'ماه\s*(پیش|قبل|گذشته)', text):
        first_of_month = anchor.replace(day=1)
        previous_end = first_of_month - timedelta(days=1)
        return TimeScope(_to_int(previous_end.replace(day=1)), _to_int(previous_end),
                         'ماه گذشته', 'relative')
    if 'دیروز' in words:
        yesterday = anchor - timedelta(days=1)
        return TimeScope(_to_int(yesterday), _to_int(yesterday), 'دیروز', 'relative')
    if any(w in text for w in ('اخیرا', 'این چند وقت', 'این روزا', 'این مدت')):
        return TimeScope(_to_int(anchor - timedelta(days=60)), _to_int(anchor),
                         'اخیرا', 'relative')
    if shift_year:
        return TimeScope(_to_int(date(anchor.year - 1, 3, 21)),
                         _to_int(date(anchor.year, 3, 20)), 'پارسال', 'relative')
    explicit = re.search(r'\b(20\d\d)\b', text)
    if explicit:
        year = int(explicit.group(1))
        return TimeScope(year * 10000 + 101, year * 10000 + 1231, str(year),
                         'gregorian-year')
    return None


def where_clause(scope: TimeScope | None, layers: tuple[str, ...],
                 all_layers: tuple[str, ...]) -> dict | None:
    """Chroma filter for a time scope and a layer selection.

    The date test is an *overlap* test, not containment: a thread rollup spans
    twelve months, and requiring span_from >= scope.from would throw away
    exactly the summary layer that the question needs."""
    clauses: list[dict] = []
    if scope:
        clauses.append({'span_from': {'$lte': scope.to_int}})
        clauses.append({'span_to': {'$gte': scope.from_int}})
    if layers and set(layers) != set(all_layers):
        clauses.append({'layer': {'$in': list(layers)}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {'$and': clauses}


def keyword_query(question: str) -> str:
    """Strip interrogatives so lexical retrieval scores content words only."""
    kept = [t for t in textnorm.tokens(question) if t not in QUESTION_WORDS]
    return ' '.join(kept) or question


def expand(question: str) -> list[str]:
    """Deterministic multi-query expansion: the question, its keyword form, and
    one synonym-substituted variant. No LLM, so it can always be on."""
    variants = [question]
    keywords = keyword_query(question)
    if keywords and keywords != question:
        variants.append(keywords)
    swapped = []
    for token in textnorm.tokens(question):
        swapped.extend(SYNONYMS.get(token, ()))
    if swapped:
        variants.append(f"{keywords} {' '.join(dict.fromkeys(swapped))}")
    return list(dict.fromkeys(variants))


HYDE_PROMPT = (
    'کاربر یک دفترچه روزانه فارسی دارد. برای سؤال زیر، یک پاراگراف کوتاه بنویس '
    'که *شبیه* یک یادداشت روزانه باشد و جواب احتمالی را در خودش داشته باشد. '
    'حدس زدن اشکالی ندارد؛ این متن فقط برای جستجو استفاده می‌شود.')


def hyde(llm, model: str, question: str) -> str:
    """Hypothetical Document Embeddings: search with a fake diary entry instead
    of the question, so the query vector lives in the same register as the
    corpus. Questions and diary entries word things very differently, and that
    mismatch is what dense retrieval is worst at."""
    try:
        turn = llm.chat([{'role': 'system', 'content': HYDE_PROMPT},
                         {'role': 'user', 'content': question}], model=model)
        return (turn.content or '').strip() or question
    except Exception:
        return question
