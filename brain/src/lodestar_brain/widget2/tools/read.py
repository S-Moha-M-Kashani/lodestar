"""`find_cards` — the whole board, asked one question at a time.

The owner's brief was one call rather than a family of narrow tools: *"there
must be one function which could accept all filters and then the outcome is the
cards"*, plus *"also keyword searches. like the search bar on the board"*. So
this module holds one tool taking every filter the board can express, all
optional and combined with AND, and the filter surface is
`tool-inventory-decision.md`'s table read off `js/core/cards.js` rather than
guessed at.

**Sixteen filters and no seventeenth.** Every one is either a rule the board's
own UI already applies (`matchesFilters`) or a field the board stores and can
therefore be asked about. What is deliberately absent: `effortSrc`,
`controlSrc` and `planSrc`, because provenance is *who chose* a value and not
what it is; `habitTimes` and `habitHistory`, because a schedule and a record
are read on a card rather than searched across cards. The vocabularies are
`fields.py`'s, so nothing here can offer the model a value `cleanCard` rejects,
and the enumerated filters read their card field through `BOARD_KEYS` — the one
snake_case → camelCase table — so a filter cannot come to name a field the
board does not have.

**The keyword search is the board's search box, not something like it.** One
haystack of `title + ' ' + notes + ' ' + tags.join(' ')`, lower-cased, and the
query trimmed and lower-cased at comparison time — `matchesFilters`' own two
lines. It matters that this is the *joined* text and not three separate
substring tests, which is what Widget 1's `list_cards` does: on the joined text
a query spanning the end of the title and the start of the notes matches, and
under three tests it does not. A search that is nearly the board's is worse
than one that is obviously different, because the near-miss is the one nobody
can explain.

**A stated total, always.** The answer says how many cards matched *before* any
cap, then lists up to `limit` of them, and says outright when it was cut short.
A partial board reported as the whole one is this tool's version of a
fabricated price: "all my important cards" answered with twenty of forty is a
sentence the model will go on to reason from.

**Sorting is named, and a card with no value for the field goes last.** A list
of eighty ordered by a field half of them lack is a list whose head is empty.
The default is `captured`, newest first, chosen because it is the one sort no
card can be missing — a default that pushed cards to the tail would be a
default hiding work.

**Naming a card is asking for it in full.** `card_id` is the id lookup the
decision document reserves ("a lookup, which the reader answers by taking ids
directly"), and it answers at `full` — the owner's second detail level, reached
from this one tool rather than from a second tool. Everything else answers at
`line`. Both go through `render_card`, which is the only place in `widget2/`
that turns a card into words.

**Proposals are invisible here, and not by a rule this module keeps.**
`readBoard` selects `pending = 0` (`server.js`), so a card awaiting the owner's
approval is absent from `/api/state` and therefore from every answer below. A
tool that reported one would be telling the owner about a card they have not
accepted yet.

**How the board is reached, and why not through `BoardSnapshot`.** One
`BoardClient.list_board` per call — the read that carries the cards *and* the
category registry in one response, which is what `render_card`'s `labels` needs
to say "Travel" instead of "travel". `BoardSnapshot` is the wrong instrument
here twice over: it caches `cards` alone, so a snapshot would still cost a
second fetch for the registry; and it is keyed on a `turn_id`, which Widget 2's
`_run_config` does not mint — deliberately, because until this tool arrived
there was nothing to share a fetch between. So the snapshot would degenerate to
exactly this one fetch while implying a sharing that does not happen. When
Widget 2 grows a second board-reading tool and mints a turn id, the snapshot is
where that belongs, and it will need a `board()` method rather than a
`cards()` one.

**`board_of(config)` and never an argument.** Which board the owner is looking
at is their choice in the picker, not something a tool call may name, mistype
or be argued out of. Note the live gap: Widget 2's `_run_config` does not carry
`board_id` into `configurable` either, so today this reads the board API's own
default board. That is `graph.py`'s to fix when the tool list is assembled, and
this end needs no change when it does.

The two doors are `tools/dual.py`'s, shared rather than copied: this tool is
written once as a coroutine, because a board read is almost entirely waiting,
and given a synchronous door because Widget 2's own `run` takes the sync path
and so do the evals and these tests. A coroutine-only tool would fail there as
a fenced `{'error': …}` the model reads as a broken board.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Literal, get_args

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ...board.client import BoardClient
from ...board.snapshot import board_of
from ...tools.dual import with_sync_door
from .fields import (BOARD_KEYS, Cadence, CardType, Column, Control, Effort,
                     Rank)
from .render import render_card
from .spans import SpanPreset, in_span, plan_in_span, span_of

#: How many cards an answer lists when nobody says. A `line` rendering runs to
#: roughly 150 characters, so twenty is ~3 000 — enough to read a week's work
#: off, and small beside the route's own 120 000-character refusal.
DEFAULT_LIMIT = 20
#: And the most it will list at once: ~15 000 characters, about an eighth of
#: that refusal, so a model asking for everything cannot spend a turn's whole
#: budget on one tool result. The stated total is what keeps the cap honest.
MAX_LIMIT = 100

#: The filters that are a plain equality against one card field. The board's
#: own spelling comes from `BOARD_KEYS`, so this list names the *filters* and
#: nothing here re-spells a field of the card.
EQUAL_FILTERS = ('column_id', 'type', 'category', 'importance', 'urgency',
                 'effort', 'control', 'habit_freq')

#: The five date filters, and the card field each one is about. `deadline` and
#: `plan` are writable and therefore in `BOARD_KEYS`; the three timestamps are
#: not written by anybody, which is why they are spelled here — and this is the
#: only place in `widget2/` that spells them.
SPAN_FIELDS = {'captured': 'createdAt', 'finished': 'doneAt',
               'updated': 'updatedAt', 'deadline': BOARD_KEYS['deadline'],
               'plan': BOARD_KEYS['plan']}

#: Which cards get a row of their own. `all` narrows nothing and is the default,
#: unlike the board's own `filters.nesting`: that default hides children because
#: they are already drawn inside their parent, which is a fact about a screen
#: and not about a question a model asked.
Scope = Literal['all', 'top', 'children']

Order = Literal['asc', 'desc']

#: The named sorts. The first five are `SPAN_FIELDS`' own keys — a test pins
#: that, so a sixth date filter cannot arrive without becoming sortable —
#: and `priority` is the board's derived one.
SortBy = Literal['captured', 'finished', 'updated', 'deadline', 'plan',
                 'priority']
SORT_BY: tuple[str, ...] = get_args(SortBy)


class Span(BaseModel):
    """One date filter: a preset, or two dates, or one date and open at the
    other end.

    A preset decides on its own when it is given. Both halves are here rather
    than in five triples of flat arguments, because five copies of one block is
    five places for a boundary rule to drift.
    """

    preset: SpanPreset | None = Field(
        None, description="a span relative to today: 'week' (this Monday to "
                          "Sunday), 'month' (this calendar month), 'days30' "
                          "(the last thirty days, today included), 'year' "
                          '(this calendar year)')
    start: str | None = Field(
        None, description="the first day to include, 'YYYY-MM-DD' — ignored "
                          'when a preset is given')
    end: str | None = Field(
        None, description="the last day to include, 'YYYY-MM-DD', whole day "
                          'included')


class FindCardsArgs(BaseModel):
    """Every filter the board can express, all optional, combined with AND.

    An absent filter narrows nothing. Where a vocabulary has an "unset" value
    the board itself uses `''`, so `importance=''` asks for the cards nobody has
    judged — which is a real question, and different from leaving the filter
    out.
    """

    keyword: str | None = Field(
        None, description="free text, matched exactly as the board's own search "
                          "box matches it: anywhere in the card's title, notes "
                          'or tags, ignoring case')
    column_id: Column | None = Field(
        None, description="where the card lives; 'answered' IS Done")
    type: CardType | None = None
    category: str | None = Field(
        None, description="a life area's id as the board spells it, or '' for "
                          'the cards filed under none')
    importance: Rank | None = Field(
        None, description="'high', 'low', or '' for cards nobody has judged")
    urgency: Rank | None = Field(
        None, description="'high', 'low', or '' for cards nobody has judged")
    effort: Effort | None = None
    control: Control | None = Field(
        None, description="how much of it is theirs to do: 'act', 'influence', "
                          "'none'")
    habit_freq: Cadence | None = Field(
        None, description="a habit's cadence; '' is every card that is not a "
                          'habit')
    tags_all: list[str] | None = Field(
        None, description='only cards carrying every one of these tags')
    tags_any: list[str] | None = Field(
        None, description='only cards carrying at least one of these tags')
    captured: Span | None = Field(None, description='when it was written down')
    finished: Span | None = Field(
        None, description='when it was finished — unfinished cards match no '
                          'such span at all')
    updated: Span | None = Field(None, description='when it was last changed')
    deadline: Span | None = Field(None, description='when it is due')
    plan: Span | None = Field(
        None, description='when they mean to get to it. A plan is the period it '
                          "names, so a card planned for '2027' matches a "
                          'question about any week of 2027')
    has_deadline: bool | None = Field(
        None, description='true for cards that have a due date at all, false '
                          'for cards that have none')
    scope: Scope = Field(
        'all', description="'top' for cards that are not part of another card, "
                           "'children' for cards that are, 'all' for both")
    child_of: str | None = Field(
        None, description='only the cards that are part of this one, by its id')
    card_id: str | None = Field(
        None, description='ask for one card by its id and get the full detail '
                          'of it. Every filter below and above is ignored — '
                          'naming a card is asking for that card')
    sort_by: SortBy = Field(
        'captured', description='which field orders the answer; cards with no '
                                'value for it come last either way')
    order: Order = Field(
        'desc', description="'desc' is newest or latest first; for `priority`, "
                            "'asc' puts the urgent and important first, the "
                            'way the board numbers them')
    limit: int = Field(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT,
        description='how many cards to list. The answer always states how many '
                    'matched in total, so a cap is visible rather than silent')


def keyword_haystack(card: dict) -> str:
    """The text the board's search box searches, lower-cased.

    `card.title + ' ' + card.notes + ' ' + card.tags.join(' ')`, exactly —
    `matchesFilters` in `js/core/cards.js`. One joined string and not three
    fields, so a query that runs from the last word of a title into the first
    word of the notes matches here as it matches there.
    """
    tags = ' '.join(str(tag) for tag in (card.get('tags') or []))
    return f'{card.get("title") or ""} {card.get("notes") or ""} {tags}'.lower()


def matches_keyword(card: dict, keyword: str | None) -> bool:
    """The board's search rule. An empty or blank query matches everything,
    which is `searchNeedle()` returning '' and the whole `if` being skipped."""
    needle = (keyword or '').strip().lower()
    return not needle or needle in keyword_haystack(card)


def priority_rank(card: dict) -> int:
    """The board's automatic priority: 1 urgent and important, 2 urgent only,
    3 important only, 4 neither — and 0 while either judgement is missing.

    `priorityOf` in `js/core/constants.js`, mirrored for the same reason
    `spans.py` mirrors `timespan.js`: the two ends cannot import each other, and
    a model asked for "my most important cards" has to get the order the board
    would have shown. Never stored, so it is derived here and nowhere else.
    """
    importance, urgency = card.get('importance'), card.get('urgency')
    if not importance or not urgency:
        return 0
    if urgency == 'high':
        return 1 if importance == 'high' else 2
    return 3 if importance == 'high' else 4


def _spans(args: FindCardsArgs, now: datetime
           ) -> dict[str, tuple[datetime, datetime]]:
    """The date filters that actually narrow something, computed once per call.

    `span_of` answers `None` for a filter that names neither a preset nor a
    date, so those drop out here rather than being asked of every card.
    """
    asked = {name: getattr(args, name) for name in SPAN_FIELDS}
    spans = {name: span_of(preset=given.preset, start=given.start,
                           end=given.end, now=now)
             for name, given in asked.items() if given is not None}
    return {name: span for name, span in spans.items() if span is not None}


def _matches_scope(card: dict, args: FindCardsArgs) -> bool:
    parent = (card.get('parentId') or '').strip()
    if args.child_of is not None and parent != args.child_of:
        return False
    if args.scope == 'top':
        return not parent
    if args.scope == 'children':
        return bool(parent)
    return True


def _matches_tags(card: dict, args: FindCardsArgs) -> bool:
    """All-of like the board's own tag filter, plus any-of, which is new.

    Compared lower-cased and trimmed on both sides: `sanitizeCard` already
    stores tags that way, and a model typing 'Autumn' means the tag the board
    calls 'autumn'.
    """
    carried = {str(tag).strip().lower() for tag in (card.get('tags') or [])}
    wanted_all = {t.strip().lower() for t in (args.tags_all or []) if t.strip()}
    wanted_any = {t.strip().lower() for t in (args.tags_any or []) if t.strip()}
    if wanted_all and not wanted_all <= carried:
        return False
    return not wanted_any or bool(wanted_any & carried)


def matches(card: dict, args: FindCardsArgs,
            spans: dict[str, tuple[datetime, datetime]]) -> bool:
    """Does one card pass every filter that was given? AND throughout.

    The enumerated filters are one loop rather than eight `if`s, which is what
    keeps `BOARD_KEYS` the only translation between a filter's name and a card's
    field — an eighth `if` spelling `habitFreq` by hand is how the two come to
    disagree.
    """
    for name in EQUAL_FILTERS:
        wanted = getattr(args, name)
        if wanted is not None and (card.get(BOARD_KEYS[name]) or '') != wanted:
            return False
    if not _matches_scope(card, args) or not _matches_tags(card, args):
        return False
    if args.has_deadline is not None:
        if bool((card.get('deadline') or '').strip()) is not args.has_deadline:
            return False
    for name, span in spans.items():
        value = card.get(SPAN_FIELDS[name])
        # A plan is a period and matches on overlap; every other field is a
        # moment. That is the one difference between the five, and it lives in
        # `spans.py` rather than here.
        passed = plan_in_span(value, span) if name == 'plan' else in_span(value, span)
        if not passed:
            return False
    return matches_keyword(card, args.keyword)


def _sort_value(card: dict, sort_by: str) -> object | None:
    """What the sort reads off a card, or `None` when the card has not got it.

    `None` is what sends a card to the tail, so the absences the board spells
    differently — `0` for a timestamp, `''` for a deadline or a plan, `0` for an
    unjudged priority — all become the one answer here.
    """
    if sort_by == 'priority':
        return priority_rank(card) or None
    raw = card.get(SPAN_FIELDS[sort_by])
    if isinstance(raw, str):
        # A partial plan sorts lexicographically, which is chronological for
        # this shape: '2027' < '2027-03' < '2027-03-04' < '2027-04'.
        return raw.strip() or None
    return raw if isinstance(raw, (int, float)) and raw > 0 else None


def _sorted(cards: list[dict], sort_by: str, order: str) -> list[dict]:
    """The matched cards in the order asked for, with the ones that have no
    value for the field at the end — in *both* directions, because a list whose
    head is eighty cards with no deadline is a list nobody reads.

    Python's sort is stable and `reverse=True` does not reverse ties, so cards
    holding the same value keep the board's own order either way.
    """
    def value(card: dict) -> object:
        return _sort_value(card, sort_by)

    present = [card for card in cards if value(card) is not None]
    missing = [card for card in cards if value(card) is None]
    present.sort(key=value, reverse=order == 'desc')
    return present + missing


def _direction(sort_by: str, order: str) -> str:
    if sort_by == 'priority':
        return ('most important and urgent first' if order == 'asc'
                else 'least important and urgent first')
    return 'earliest first' if order == 'asc' else 'latest first'


def _headline(total: int, shown: int, unvalued: int,
              args: FindCardsArgs) -> str:
    """What the answer says about itself, before any card.

    The total is the *matched* count and never the listed one, and the shortfall
    is named rather than implied: a model told "12 cards match" and shown five
    has been told the truth, where five lines on their own read as the whole
    board.
    """
    order = f'sorted by {args.sort_by} ({_direction(args.sort_by, args.order)})'
    tail = (f'; the {unvalued} with no {args.sort_by} are last' if unvalued
            else '')
    if shown == total:
        card = 'card' if total == 1 else 'cards'
        return f'{total} {card} match, {order}{tail}.'
    return (f'{total} cards match. Showing {shown} of them, {order}{tail} — '
            f'{total - shown} not listed, so this is not the whole answer: '
            f'narrow the filters or raise `limit` to see the rest.')


def make_find_cards(board: BoardClient, *,
                    clock: Callable[[], datetime] = datetime.now) -> BaseTool:
    """The one card-reading tool, over one board client.

    `clock` is the injectable now — a callable and not a moment, because a tool
    object outlives a day and a fixed `datetime` would freeze every relative
    span at import time. It is never a tool argument: a model that could name
    today could ask "what did I finish this week" about a week of its choosing.
    """

    @tool('find_cards', args_schema=FindCardsArgs)
    async def find_cards(config: RunnableConfig = None, **raw) -> str:
        """Find cards on the board. Every filter is optional and they combine:
        naming two of them asks for the cards that satisfy both.

        Use it for any question about what is on the board — "my important
        cards", "everything in Done last month", "what is due this week",
        "anything tagged berlin" — and for free-text search, which works exactly
        like the search box on the board itself.

        The answer states how many cards matched in total and lists up to
        `limit` of them, one line each. To see one card in full, call it again
        with that card's `card_id`.

        Cards awaiting the user's approval are not on the board and are never
        listed here.
        """
        args = FindCardsArgs(**raw)
        now = clock()
        # One read, and it carries the registry as well as the cards: a life
        # area has to be named "Travel" and not "travel", and that mapping
        # arrives in the same response rather than in a second round trip.
        document = await board.list_board(board_of(config))
        cards = document.get('cards') or []
        labels = {row.get('id'): row.get('label') or ''
                  for row in (document.get('categories') or []) if row.get('id')}
        # The link lives on the child, so a parent holds no list of its parts
        # and only the whole board can count them. Counted once here and handed
        # to `render_card`, which cannot ask.
        children = Counter((card.get('parentId') or '') for card in cards)

        def rendered(card: dict, level: str) -> str:
            # `.get` and not `children[...]`: a card with no id would otherwise
            # be handed the count of *top-level* cards, since that is the tally
            # the empty parent id keeps.
            return render_card({**card, 'children': children.get(card.get('id'), 0)},
                               level=level, labels=labels, now=now)

        if args.card_id is not None:
            found = next((c for c in cards if c.get('id') == args.card_id), None)
            if found is None:
                return (f'No card with id {args.card_id!r} on this board. Ledger '
                        'numbers like C-042 are not ids — find the card first '
                        'and use the id it comes back with.')
            return rendered(found, 'full')

        spans = _spans(args, now)
        matched = [card for card in cards if matches(card, args, spans)]
        if not matched:
            return 'No cards match those filters.'
        unvalued = sum(1 for card in matched
                       if _sort_value(card, args.sort_by) is None)
        listed = _sorted(matched, args.sort_by, args.order)[:args.limit]
        head = _headline(len(matched), len(listed), unvalued, args)
        return '\n'.join([head, *(rendered(card, 'line') for card in listed)])

    return with_sync_door(find_cards)


__all__ = ['DEFAULT_LIMIT', 'EQUAL_FILTERS', 'MAX_LIMIT', 'SORT_BY',
           'SPAN_FIELDS', 'FindCardsArgs', 'Order', 'Scope', 'SortBy', 'Span',
           'keyword_haystack', 'make_find_cards', 'matches', 'matches_keyword',
           'priority_rank']

"""Alternatives considered
========================

**Why did you write your own filter engine rather than query the board for
this?**

Because there is nothing to query. The board's API offers one board-shaped read
— `GET /api/state`, cards and registry — and no query surface at all: no
`?type=`, no sort, no page. The filtering the *board* does is in the browser,
over the whole list, in `matchesFilters`. So the choice was never "filter here
or filter there"; it was "filter here, or add a query API to `server.js`", and
this stage is explicitly one that adds no board route. Sixteen predicates over a
few hundred dicts is microseconds, and the board a person keeps their life on is
hundreds of cards, not millions.

**Why the obvious option fails.** The obvious option is SQL — the cards are in
SQLite, and `WHERE type = ? AND importance = ?` is what SQL is for. It fails on
invariant 2: the brain never opens the board's database, and `test_guardrails.py`
reads the source to keep it that way. That invariant is what makes the durability
promise checkable in one file, and trading it for a `WHERE` clause would put a
second reader — and one day a second writer — beside the only place a card can
be destroyed. Also, two of the filters are not expressible there: a partial
`plan` matches a span by *overlap* (`2027` answers a question about a week of
2027), and `priority` is derived from two columns and stored in none.

**Why not the framework.** LangChain has a real answer to the adjacent question
and not to this one. Its self-query retriever turns natural language into
metadata filters over a vector store, and the brain already runs that machinery
for *meaning* — `find_related`, dense + BM25 over embedded cards. What is wanted
here is enumeration, which is the opposite job: "every card with high
importance" must answer with all of them, in a stated total, and a retriever
answers with the top *k* by similarity and cannot tell you what it left out.
Pydantic — already a dependency — does the part that is genuinely hard to
hand-roll, which is the typed schema the model fills in; the sixteen predicates
under it are one line each.

**The libraries that would do it** (checked 2026-09-08):

- **`pandas`** — a DataFrame, boolean masks, `sort_values(na_position='last')`
  for free, which is the one rule here that takes real care. ~60 MB and a C
  extension to filter a list of dicts, on a service whose brain suite is
  deliberately runnable with no extras.
- **`jmespath` / `jsonpath-ng`** — declarative queries over JSON. The filter
  expression would then be a string built at runtime, so a typo in a field name
  becomes a silent empty answer instead of a `KeyError` — the failure mode this
  module goes out of its way to avoid by resolving every field through
  `BOARD_KEYS`.
- **`sqlite3` over an in-memory mirror** — load the cards into a temp table and
  hand the model's filters to SQL. Real, and it buys indexes nobody needs at
  this size; it also puts a `CREATE TABLE` in a package whose one sqlite import
  is an asserted invariant.
- **`duckdb`** — the same idea done well, columnar, reads dicts directly.
  A dependency of that weight needs a board a thousand times this one's size.
- Greenfield, on someone else's data: `duckdb` the moment the board outgrows
  memory, and a real query API on `server.js` before that.

**What would change the decision.** A board large enough for a full read to be
the slow part of a turn — which is the same threshold `board/snapshot.py`'s own
note names for switching to a conditional GET. At that point the right change is
a filtered read on `server.js` and this module becomes the caller of it, because
the honest fix for "too many cards to fetch" is not a faster loop over all of
them. The number to watch is the board's card count against the turn's board
fetch: on a few hundred cards the fetch is round trips and the filtering does
not register.
"""
