"""A card in words a model can read — the one place that decides them.

Every tool in `widget2/` describes a card through `render_card` and nowhere
else, so a card cannot be described two ways: a list and a single card differ in
*how much* is said (`level`), never in what a field is called or in what an
absent one reads as. That is why the reader was built after this module rather
than beside it.

**Two levels, one function.** `line` is one line for a list — the ledger
number, the words, the type, the life area, the status, the priority, the dates,
the tags and the child count. `full` is a block for one card, adding effort,
control, both timestamps, the finished date, the cadence and the notes. One
function with an argument, because two functions are two places for a wording
to drift.

**Every absence reads as an absence.** No `None`, no `''`, no `0` and no blank
column reaches the model: a card with nothing due says `no deadline`, and a
proposal with no number yet says so rather than rendering `C-000` — which is
what `cardLabel` would produce and what `ensureNums` exists to stop reaching a
reader. The rule is `pricing.py`'s, one layer down: a figure nobody measured is
worse than no figure, because a model shown `deadline: 0` will eventually
report a card as due in 1970.

**The board's vocabulary is translated where the id is not self-explanatory,
and nowhere else.** The column id `answered` is **Done** to a reader, through
`COLUMN_LABELS` in `fields.py` rather than a second spelling here. A category id
renders as its registry label. `control: none` gets words for a sharper reason
than politeness — "none" as a *value* reads as "not set", so the one field whose
id would be mistaken for an absence is the one that must not be printed raw. A
type, an effort and a `high`/`low` rank say what they mean already, so they are
printed as they are.

**A plan is stated at the precision it was given** — `2027`, `2027-03`,
`2027-03-04` — and never widened to a false day. It is the one field the board
stores as a partial date, so the string *is* the precision and any prettier
rendering would have to invent the missing half.

**Two things the caller knows and this module cannot.**

- *The registry.* `labels` maps category id → the board's own label. `None`
  means no registry was supplied, and then the id is printed as it stands: it
  is the only true thing in hand, where `uncategorised` would deny a life area
  the card really carries. A registry that *was* supplied and lacks the id
  renders `uncategorised`, because that is the board's own answer — `cleanCard`
  blanks a category the registry no longer holds.
- *The child count.* The link lives on the child as `parentId`, so a parent
  holds no list of its parts and no card can be asked how many it has without
  the whole board. The caller therefore injects it as `children` on the dict it
  passes — a field the board's own JSON never carries — and the signature stays
  the one the writers and the reader were built against. **An uncounted card is
  not a childless card**: with `children` absent the count is simply not said,
  where `no child cards` would be a fabricated zero about the one fact this
  module cannot check. A `children` key is read and never a `parentId`: a raw
  parent id names nothing to a model, and its ledger number needs the board.

`effortSrc`/`controlSrc` are deliberately not read. Effort and control always
hold the scale's midpoint until somebody judges them, and the card dialog shows
that midpoint the same way, so a reader is told what the board says the card is
rather than who said it.

This module imports nothing of the agent's — one label table from `fields.py`,
which imports nothing itself — the `plan.js`/`nesting.js`/`timespan.js` rule.
It is what lets `test_widget2_render.py` pin every wording under plain pytest,
with no board, no model and no graph, and it is why the clock is an argument:
`now` is read from the machine only when a relative phrase actually needs one,
so a fixed moment gives a fixed string and "due in 6 days" cannot change the
suite's answer overnight.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal

from .fields import COLUMN_LABELS

Level = Literal['line', 'full']

#: How the board itself says these values, and the only fields that need it.
#: Keyed on `fields.py`'s own vocabularies — the values live there, the wording
#: lives here, the division `COLUMN_LABELS` already makes.
CONTROL_WORDS = {'act': 'I can act', 'influence': 'I can influence',
                 'none': 'out of my hands'}
IMPORTANCE_WORDS = {'high': 'important', 'low': 'not important'}
URGENCY_WORDS = {'high': 'urgent', 'low': 'not urgent'}


def _days(n: int) -> str:
    return f'{n} day' if n == 1 else f'{n} days'


def _ledger(card: dict) -> str:
    num = card.get('num')
    if isinstance(num, int) and num > 0:
        return f'C-{num:03d}'
    return '(no ledger number yet)'


def _area(card: dict, labels: Mapping[str, str] | None) -> str:
    cat = (card.get('category') or '').strip()
    if not cat:
        return 'uncategorised'
    if labels is None:
        return cat
    return labels.get(cat) or 'uncategorised'


def _priority(card: dict) -> str:
    imp = IMPORTANCE_WORDS.get(card.get('importance') or '')
    urg = URGENCY_WORDS.get(card.get('urgency') or '')
    if not imp and not urg:
        return 'no priority set'
    return f'{imp or "importance not set"}, {urg or "urgency not set"}'


def _due(card: dict, now: datetime | None) -> str:
    """The deadline and how far off it is, or '' when there is none."""
    due = (card.get('deadline') or '').strip()
    if not due:
        return ''
    try:
        day = date.fromisoformat(due)
    except ValueError:
        # The board's own validator guarantees 'YYYY-MM-DD' or ''. If anything
        # upstream ever breaks that, say the date rather than raise inside a
        # tool call the model would read as a broken board.
        return due
    gap = (day - (now or datetime.now()).date()).days
    if gap == 0:
        return f'{due} (today)'
    return f'{due} (in {_days(gap)})' if gap > 0 else f'{due} ({_days(-gap)} overdue)'


def _tags(card: dict) -> str:
    tags = card.get('tags')
    return ', '.join(str(t) for t in tags if str(t).strip()) if tags else ''


def _children(card: dict) -> str:
    """The count the caller injected, or '' when nobody counted."""
    n = card.get('children')
    if not isinstance(n, int) or n < 0:
        return ''
    return f'{n} child card' + ('' if n == 1 else 's') if n else 'no child cards'


def _cadence(card: dict) -> str:
    freq = (card.get('habitFreq') or '').strip()
    if not freq:
        return ''
    count = card.get('habitCount')
    return f'{count}× {freq}' if isinstance(count, int) and count > 0 else freq


def _day(stamp: object) -> str:
    """An epoch-milliseconds field as the calendar day it fell on, locally.

    Local, like `js/core/timespan.js`: this board is one person's life in one
    place, and a card captured at 23:30 was captured that evening and not the
    next morning.
    """
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return ''
    return datetime.fromtimestamp(stamp / 1000).date().isoformat()


def render_card(card: dict, *, level: Level = 'line',
                labels: Mapping[str, str] | None = None,
                now: datetime | None = None) -> str:
    """One card as text: a single line, or a block of one field per row.

    `labels` is the board's category registry (id → label); `None` means none
    was supplied. `now` dates the deadline's relative phrase and is read from
    the machine only if that phrase is needed. `card['children']` is a count
    the caller injects, since only the whole board can answer it.
    """
    title = (card.get('title') or '').strip() or '(untitled)'
    head = f'{_ledger(card)} “{title}”'
    status = COLUMN_LABELS.get(card.get('columnId') or '') or 'no status'
    due, tags, kids = _due(card, now), _tags(card), _children(card)

    if level == 'line':
        parts = [head, str(card.get('type') or 'no type'), _area(card, labels),
                 status, _priority(card),
                 f'due {due}' if due else 'no deadline',
                 f'plan {card["plan"]}' if card.get('plan') else 'no plan',
                 f'tags: {tags}' if tags else 'no tags']
        if kids:
            parts.append(kids)
        return ' · '.join(parts)

    # `full`: the same fields in the same order, one per row, plus the ones a
    # list has no room for. The notes go last so that a note of several lines
    # cannot be read as another field.
    rows = [f'type: {card.get("type") or "no type"}',
            f'life area: {_area(card, labels)}',
            f'status: {status}']
    if cadence := _cadence(card):
        rows.append(f'repeats: {cadence}')
    rows += [f'priority: {_priority(card)}',
             f'deadline: {due or "none"}',
             f'plan: {card.get("plan") or "none"}',
             f'effort: {card.get("effort") or "not set"}',
             f'control: {CONTROL_WORDS.get(card.get("control") or "") or "not set"}',
             f'tags: {tags or "none"}']
    # Absent rather than "no child cards", for the reason in the module note.
    if kids:
        rows.append(f'children: {kids}')
    rows += [f'captured: {_day(card.get("createdAt")) or "not recorded"}',
             f'updated: {_day(card.get("updatedAt")) or "not recorded"}']
    # A card that is not finished is not being asked when it was.
    if finished := _day(card.get('doneAt')):
        rows.append(f'finished: {finished}')
    rows.append(f'notes: {(card.get("notes") or "").strip() or "none"}')
    return '\n'.join([head, *rows])


__all__ = ['CONTROL_WORDS', 'IMPORTANCE_WORDS', 'URGENCY_WORDS', 'Level',
           'render_card']

"""Alternatives considered
========================

**Why did you write your own relative-date phrasing and card templating?**

Because neither is the hard part of this module. The hard part is deciding what
an *absent* field says, and no library has an opinion about that: the whole
value here is `no deadline` instead of a blank, `(no ledger number yet)`
instead of `C-000`, and `out of my hands` instead of a `control: none` a model
would read as unset. The date arithmetic under `due (in 4 days)` is four lines
of `date` subtraction, and the rendering is an f-string per field.

**Why the obvious option fails.** `humanize.naturaldelta` — the obvious
dependency for the relative phrase — is built for elapsed wall-clock time, not
for calendar days: it answers a `timedelta` of 20 hours as "20 hours", so a
deadline tomorrow morning renders as "in 20 hours" at lunchtime and "in 8
hours" at midnight, and the same card reads as a different urgency through the
day. What a deadline means here is a *day* — the board stores `YYYY-MM-DD` and
nothing finer — so the subtraction has to be date-to-date, which is the line of
code a library would be adopted to avoid writing.

**Why not the framework.** LangChain offers nothing here on purpose: a tool
returns a string or a structured payload and the framing is the caller's, which
is why `middleware/untrusted.py` exists to fence tool output rather than
`@tool` doing it. Pydantic is already a dependency and does serialise a model
to JSON — and JSON is precisely the wrong shape, because `"deadline": null` is
the failure this module was written to prevent, one absence away from a card
reported as due in 1970.

**The libraries that would do it.**

- `humanize` (4.x) — one call for the phrase, English plus locales. Wrong unit,
  as above, and would still need every absence rule written by hand.
- `babel.dates.format_timedelta` — correct pluralisation in many languages,
  ~30 MB of CLDR data, and the same wall-clock-not-calendar mismatch.
- `arrow` / `pendulum` — `humanize()` on a rich datetime type; adopting one
  means every timestamp in this package becomes that type or gets converted at
  the boundary, to shorten four lines.
- `jinja2` for the block — a real answer if these shapes ever grow branches or
  need a second audience. Today it moves the wording out of the module the
  tests read and into a template with no types.
- On a greenfield project with several locales, `babel`. With one reader in one
  language, none of them.

**What would change the decision.** A second audience for these strings — a
non-English rendering, or a shape a person reads rather than a model — makes
`babel` plus a template the right answer, because at that point pluralisation
and layout stop being four lines and start being a table. Until then the
dependency would buy a worse phrase.
"""
