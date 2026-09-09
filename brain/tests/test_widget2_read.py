"""`find_cards` — the one call that reads the board.

What can go quietly wrong here answers the owner with a *wrong list* rather than
an error, which is why every test below is about a claim the answer makes rather
than about the code that makes it:

  1. **A filter the model is offered but nothing applies**, or one the board
     stores that nothing offers. The schema is what the model reads the board's
     capabilities off, so it is pinned as an exact set — the same rule
     `test_tool_schemas.py` keeps for Widget 1.
  2. **A keyword search that is nearly the board's.** The requirement was "like
     the search bar on the board", so the board's own rule is transliterated
     here and both are driven with the same queries. The test also proves it can
     tell a near-miss apart, or it would pass over the very bug it exists for.
  3. **A cap nobody mentioned.** Twenty of forty cards read as forty, and a
     model reasons on from there.
  4. **A sort whose head is empty**, because half the board has no deadline.

Every date is measured from one fixed moment, `datetime(2026, 9, 7, 10, 0)` —
the Monday `test_widget2_spans.py` measures from — so the suite cannot change
its answer overnight. `spans.py`'s own boundaries are pinned there and are not
re-tested here; what is tested is that this tool wires each date filter to the
card field it names.
"""
from __future__ import annotations

from datetime import datetime
from typing import get_args

import httpx
import respx

from lodestar_brain.board.client import BoardClient
from lodestar_brain.widget2.tools.fields import (Cadence, CardType, Column,
                                                 Control, Effort, Rank)
from lodestar_brain.widget2.tools.read import (SORT_BY, SPAN_FIELDS,
                                               FindCardsArgs, make_find_cards,
                                               matches_keyword)

BOARD = 'http://board.test'

#: The Monday every span below is measured from.
NOW = datetime(2026, 9, 7, 10, 0)

#: The board's own registry, as `/api/state` hands it over.
CATEGORIES = [{'id': 'travel', 'label': 'Travel', 'h': 200},
              {'id': 'home', 'label': 'Home', 'h': 90}]


def at(text: str) -> float:
    """A card timestamp: local epoch milliseconds, as server.js stores it."""
    return datetime.fromisoformat(text).timestamp() * 1000


def card(id: str, title: str = 'A card', **extra) -> dict:
    """One card as `cleanCard` returns it, with the fields a test names."""
    stamps = {'createdAt': at('2026-09-01T09:00'),
              'updatedAt': at('2026-09-01T09:00'), 'doneAt': 0}
    return {'id': id, 'num': 1, 'title': title, 'notes': '', 'type': 'task',
            'category': '', 'columnId': 'inbox', 'importance': '',
            'urgency': '', 'effort': 'medium', 'control': 'influence',
            'deadline': '', 'plan': '', 'tags': [], 'habitFreq': '',
            'habitCount': 1, 'parentId': '', **stamps, **extra}


def reader(cards: list[dict], categories: list | None = None):
    """The tool over a mocked board. Requires an active `respx.mock`."""
    respx.get(f'{BOARD}/api/state').mock(return_value=httpx.Response(200, json={
        'version': 1, 'cards': cards,
        'categories': CATEGORIES if categories is None else categories}))
    return make_find_cards(BoardClient(BOARD), clock=lambda: NOW)


def ids(answer: str) -> list[str]:
    """The cards an answer listed, by the words each line opens with.

    Read off the rendered text on purpose: the tool answers in words, and a test
    reading a structured payload would pass against a renderer nobody could
    read.
    """
    return [line.split('“')[1].split('”')[0]
            for line in answer.splitlines() if '“' in line]


def enum_of(field: str) -> list:
    """A field's enum, wherever pydantic put it: inline for a bare Literal,
    inside `anyOf` for an optional one."""
    prop = FindCardsArgs.model_json_schema()['properties'][field]
    for candidate in [prop, *prop.get('anyOf', [])]:
        if 'enum' in candidate:
            return candidate['enum']
    raise AssertionError(f'{field} is not an enumerated filter')


# Every filter in `tool-inventory-decision.md`'s table, by the name the model
# sees. Sixteen rows, two of which are a pair (tags, and the deadline's "has
# one"/"has none" beside its span) and one of which names a card (scope).
FILTERS = {'keyword', 'column_id', 'type', 'category', 'importance', 'urgency',
           'effort', 'control', 'habit_freq', 'tags_all', 'tags_any',
           'captured', 'finished', 'updated', 'deadline', 'plan',
           'has_deadline', 'scope', 'child_of'}
#: Not filters: what to answer with, and how much of it.
CONTROLS = {'card_id', 'sort_by', 'order', 'limit'}


# This is a unit test: the schema is what the model reads the board's own
# capabilities off, so it is API and pinned as an exact set.
def test_the_schema_offers_every_filter_and_nothing_that_is_a_record():
    """A filter the model is offered but nothing applies is a lie about the
    board; a field the board stores but nothing offers is a question the owner
    cannot ask. Both are caught by an exact set — a subset check would wave a
    seventeenth filter through, which is the change this has to notice.
    """
    props = FindCardsArgs.model_json_schema()['properties']
    assert set(props) == FILTERS | CONTROLS
    # The injected config is the framework's and never the model's.
    assert 'config' not in props and 'raw' not in props

    # Provenance is *who chose* a value, and a schedule and a history are read
    # on a card rather than searched across cards. None of the five may appear
    # under any spelling.
    assert not [name for name in props
                if name.endswith('_src') or 'history' in name
                or 'times' in name]

    # Each enumerated filter is typed to the board's own vocabulary, from
    # `fields.py` — so the tool cannot offer the model a value `cleanCard`
    # would silently replace.
    for field, vocabulary in (('column_id', Column), ('type', CardType),
                              ('importance', Rank), ('urgency', Rank),
                              ('effort', Effort), ('control', Control),
                              ('habit_freq', Cadence)):
        assert enum_of(field) == list(get_args(vocabulary)), field

    # And every date filter is sortable, so a sixth cannot arrive as a filter
    # the answer has no way to order by.
    assert set(SORT_BY) == set(SPAN_FIELDS) | {'priority'}


# The board's own rule, transliterated from `matchesFilters` in js/core/cards.js:
#
#   const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
#   if (!haystack.includes(needle)) return false;      // needle = search.trim().toLowerCase()
def board_search(card: dict, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = (card['title'] + ' ' + card['notes'] + ' '
                + ' '.join(card['tags'])).lower()
    return needle in haystack


# What Widget 1's `list_cards` does instead — three separate substring tests.
# Here to prove this test can tell a near-miss apart, not as a rule to keep.
def per_field_search(card: dict, query: str) -> bool:
    needle = query.strip().lower()
    return (needle in card['title'].lower() or needle in card['notes'].lower()
            or any(needle in tag for tag in card['tags']))


CORPUS = [
    card('a', 'Renew the passport', notes='Photo booth first', tags=['admin']),
    card('b', 'GPU budget?', notes='Ask about the RAG evals', tags=['work']),
    card('c', 'Long weekend', notes='', tags=['mountains', 'autumn']),
    card('d', 'Descale the boiler', notes='Vinegar', tags=[]),
]
QUERIES = ['GPU', '  gpu  ', 'passport photo', 'autumn', 'mountains autumn',
           'boiler descale', 'the', '', '   ']


# This is a unit test: "like the search bar on the board" is the requirement, so
# the board's rule and this one are driven with the same queries.
@respx.mock
def test_the_keyword_search_is_the_boards_own_search_rule():
    """A search that is nearly the same is worse than one that is obviously
    different, because the near-miss is the one nobody can explain.

    Two claims. The rules agree on every query — including a trimmed one, a
    capitalised one, a tag and a query that runs across two fields. And the test
    can *tell them apart*: the per-field rule Widget 1 uses answers differently
    for at least one of these queries, so an implementation that drifted back to
    it would fail here rather than pass quietly.
    """
    for query in QUERIES:
        assert ([c['id'] for c in CORPUS if matches_keyword(c, query)]
                == [c['id'] for c in CORPUS if board_search(c, query)]), query

    # 'passport photo' spans the end of a title and the start of the notes: one
    # haystack matches it, three substring tests do not.
    assert [c['id'] for c in CORPUS if per_field_search(c, 'passport photo')] == []
    assert [c['id'] for c in CORPUS if matches_keyword(c, 'passport photo')] == ['a']

    # And the same query through the tool itself, so the rule is reached rather
    # than merely exported.
    answer = reader(CORPUS).run({'keyword': '  GPU  '})
    assert ids(answer) == ['GPU budget?']


# This is a unit test.
@respx.mock
def test_every_kind_of_filter_narrows_and_they_combine_with_and():
    """One assert per *kind* of rule rather than one per filter: an equality
    against a card field, a tag set, a date span, the deadline's presence, the
    nesting scope, and two of them together.

    The date spans are the interesting half, because `spans.py`'s boundaries are
    already pinned in their own suite and what is unproven here is the wiring —
    that `finished` reads `doneAt` and not `updatedAt`, and that `deadline` and
    `plan` are filterable at all, which the board's own range control cannot do.
    """
    cards = [
        card('a', 'Book the train', category='travel', importance='high',
             urgency='high', deadline='2026-09-10', tags=['travel', 'urgent']),
        card('b', 'Fix the tap', category='home', importance='low',
             columnId='answered', doneAt=at('2026-09-03T18:00'),
             tags=['home']),
        card('c', 'Old repair', category='home', columnId='answered',
             doneAt=at('2026-07-02T10:00')),
        card('d', 'Sabbatical', type='dream', plan='2027',
             tags=['travel', 'someday']),
        card('e', 'Pack the bags', parentId='a', tags=['travel']),
        card('f', 'Stretch', type='habit', habitFreq='daily'),
    ]
    tool = reader(cards)

    # An equality against a card field, through BOARD_KEYS.
    assert ids(tool.run({'importance': 'high'})) == ['Book the train']
    assert ids(tool.run({'habit_freq': 'daily'})) == ['Stretch']
    # '' is a real answer and not an absent filter: the cards nobody has judged.
    assert set(ids(tool.run({'importance': ''}))) == {
        'Old repair', 'Sabbatical', 'Pack the bags', 'Stretch'}

    # Tags, all-of like the board and any-of which is new.
    assert ids(tool.run({'tags_all': ['travel', 'urgent']})) == ['Book the train']
    assert set(ids(tool.run({'tags_any': ['home', 'someday']}))) == {
        'Fix the tap', 'Sabbatical'}

    # A finished span reads `doneAt`, so the card finished in July is out of
    # this month and the unfinished cards are out of every finished span.
    assert ids(tool.run({'finished': {'preset': 'month'}})) == ['Fix the tap']
    # A deadline span, and the plan at the precision it was given: a card
    # planned for '2027' answers a question about one week of 2027.
    assert ids(tool.run({'deadline': {'start': '2026-09-08',
                                      'end': '2026-09-30'}})) == ['Book the train']
    assert ids(tool.run({'plan': {'start': '2027-03-02',
                                  'end': '2027-03-08'}})) == ['Sabbatical']
    assert ids(tool.run({'has_deadline': False})) == [
        c['title'] for c in cards if not c['deadline']]

    # Scope: a child has a row of its own only when it is asked for.
    assert ids(tool.run({'scope': 'children'})) == ['Pack the bags']
    assert 'Pack the bags' not in ids(tool.run({'scope': 'top'}))
    assert ids(tool.run({'child_of': 'a'})) == ['Pack the bags']

    # AND: both rules, and the card that satisfies only one is gone.
    assert ids(tool.run({'category': 'home', 'column_id': 'answered',
                         'finished': {'preset': 'month'}})) == ['Fix the tap']
    assert tool.run({'category': 'travel', 'type': 'habit'}) == (
        'No cards match those filters.')


# This is a unit test: a partial board reported as the whole one is this tool's
# version of a fabricated price.
@respx.mock
def test_the_answer_states_the_true_total_and_says_it_was_cut_short():
    cards = [card(f'c{n}', f'Card {n}', importance='high') for n in range(12)]
    tool = reader(cards)

    answer = tool.run({'importance': 'high', 'limit': 5})
    assert len(ids(answer)) == 5
    head = answer.splitlines()[0]
    assert head.startswith('12 cards match.'), head
    assert '7 not listed' in head and 'not the whole answer' in head

    # And an answer that is whole says so without a shortfall to explain.
    whole = tool.run({'importance': 'high', 'limit': 20}).splitlines()[0]
    assert whole.startswith('12 cards match,') and 'not listed' not in whole


# This is a unit test: a list of eighty ordered by a field half of them lack is
# a list whose head is empty.
@respx.mock
def test_a_sort_puts_the_cards_with_no_value_last_in_both_directions():
    cards = [card('a', 'Soon', deadline='2026-09-09'),
             card('b', 'Undated'),
             card('c', 'Later', deadline='2026-12-01'),
             card('d', 'Also undated')]
    tool = reader(cards)

    assert ids(tool.run({'sort_by': 'deadline', 'order': 'asc'})) == [
        'Soon', 'Later', 'Undated', 'Also undated']
    assert ids(tool.run({'sort_by': 'deadline', 'order': 'desc'})) == [
        'Later', 'Soon', 'Undated', 'Also undated']
    # The two undated cards keep the board's own order in both directions:
    # reversing a stable sort must not reorder cards it cannot rank.
    head = tool.run({'sort_by': 'deadline', 'order': 'desc'}).splitlines()[0]
    assert 'the 2 with no deadline are last' in head, head

    # `priority` is the board's derived rank — urgent and important first, and
    # the cards nobody has judged last, which is `priorityOf` answering 0.
    judged = [card('x', 'Neither', importance='low', urgency='low'),
              card('y', 'Both', importance='high', urgency='high'),
              card('z', 'Unjudged')]
    assert ids(reader(judged).run({'sort_by': 'priority', 'order': 'asc'})) == [
        'Both', 'Neither', 'Unjudged']

    # The default, stated so it cannot drift: newest capture first, which is the
    # one sort no card can be missing.
    assert FindCardsArgs().sort_by == 'captured'
    assert FindCardsArgs().order == 'desc'


# This is a unit test: the owner chose two detail levels, and they are reached
# from one tool rather than two.
@respx.mock
def test_a_list_is_one_line_a_card_and_naming_one_gives_the_full_block():
    cards = [card('a', 'Book the train', notes='Zurich to Milan, direct.',
                  category='travel', columnId='answered', effort='low',
                  control='act', doneAt=at('2026-09-05T20:00'), num=42),
             card('b', 'Pack the bags', parentId='a')]
    tool = reader(cards)

    listed = tool.run({'column_id': 'answered'})
    assert len(listed.splitlines()) == 2, 'a headline and one line per card'
    line = listed.splitlines()[1]
    # The line carries the ledger number, the life area by its registry label,
    # the status in the board's own word, and the child count.
    assert line.startswith('C-042 “Book the train”')
    assert 'Travel' in line and 'Done' in line and '1 child card' in line
    assert 'Zurich to Milan' not in line, 'the notes belong to the full block'

    full = tool.run({'card_id': 'a'})
    assert full.startswith('C-042 “Book the train”')
    for row in ('effort: low', 'control: I can act', 'finished: 2026-09-05',
                'notes: Zurich to Milan, direct.'):
        assert row in full, row

    # An id the board does not hold is said plainly, and the ledger number is
    # named as the thing it is not — a model that guesses `C-042` as an id gets
    # told what to do instead of an empty list.
    missing = tool.run({'card_id': 'C-042'})
    assert 'No card with id' in missing and 'not ids' in missing


# This is a configuration invariant: the reader is a read, and a write
# reappearing on this surface must break a test rather than ship.
@respx.mock
def test_the_reader_only_ever_reads():
    """Two halves, because neither alone is the claim.

    Behaviourally: a whole call makes exactly one request, a GET of the board
    document, and no proposal, edit or whole-board save is even mocked — an
    attempt would raise inside the tool rather than reaching a route.

    And by reading the source, because a second write path is invisible until it
    is used: nothing in this module names a writing method of `BoardClient`, and
    nothing in it opens sqlite. `test_guardrails.py` makes the same claim for
    the client's own surface; this is the claim about the one caller Widget 2
    now has.
    """
    import lodestar_brain.widget2.tools.read as read

    from .conftest import code_of

    route = respx.get(f'{BOARD}/api/state').mock(
        return_value=httpx.Response(200, json={
            'version': 1, 'cards': [card('a', 'Only card')],
            'categories': CATEGORIES}))
    tool = make_find_cards(BoardClient(BOARD), clock=lambda: NOW)
    assert ids(tool.run({'keyword': 'only'})) == ['Only card']
    assert route.call_count == 1, 'one board read per call, and nothing else'
    assert len(respx.calls) == 1

    # The module's code, not its prose: this one carries an `Alternatives
    # considered` note that has to be free to say why an in-memory sqlite
    # mirror was the wrong answer, and a scan over the whole file cannot tell
    # that sentence from an import. It failed on its own argument.
    source = code_of(read)
    for writer in ('create_proposal', 'create_edit', 'write_vectors',
                   'record_chat', '_post(', '_put(', 'sqlite'):
        assert writer not in source, f'a read must not name {writer}'
