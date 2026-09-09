"""What `widget2/tools/render.py` says about a card — the wording is the API.

Every tool in `widget2/` describes a card through that module, so these are the
sentences a model reads. No board, no model and no graph: the formatter is a
leaf, which is what lets its whole vocabulary be pinned under plain pytest.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import get_args

from lodestar_brain.widget2.tools.fields import Control
from lodestar_brain.widget2.tools.render import CONTROL_WORDS, render_card

#: A fixed moment, so "in 4 days" cannot become "in 3 days" overnight.
NOW = datetime(2026, 9, 8, 12, 0)

#: Local midday, so the epoch converts back to the same calendar day in any
#: timezone the suite runs in — the formatter reads timestamps locally, like
#: `js/core/timespan.js`.
def stamp(y: int, m: int, d: int) -> float:
    return datetime(y, m, d, 12, 0).timestamp() * 1000


#: The board's own registry, as `/api/state` hands it over: id → label.
LABELS = {'travel': 'Travel', 'home': 'Home'}

#: Every field set, as `cleanCard` would return it, plus the `children` count
#: only a caller holding the whole board can inject.
FULL_CARD = {
    'id': 'id-1', 'num': 42, 'title': 'Plan a long weekend in the mountains',
    'notes': 'Two nights, somewhere with a train station.',
    'type': 'task', 'category': 'travel', 'columnId': 'in-progress',
    'importance': 'high', 'urgency': 'high', 'effort': 'medium',
    'control': 'act', 'effortSrc': 'user', 'controlSrc': 'user',
    'deadline': '2026-09-12', 'plan': '2027-03', 'planSrc': 'user',
    'habitFreq': '', 'habitCount': 1, 'habitTimes': [], 'habitHistory': {},
    'parentId': '', 'tags': ['mountains', 'autumn'],
    'createdAt': stamp(2026, 8, 30), 'updatedAt': stamp(2026, 9, 7),
    'doneAt': 0, 'children': 3,
}

#: What the board makes of `{title: 'Call the dentist'}` and nothing else: a
#: proposal with no ledger number, no category, no priority and no dates.
BARE_CARD = {
    'id': 'id-2', 'num': 0, 'title': 'Call the dentist', 'notes': '',
    'type': 'question', 'category': '', 'columnId': 'inbox',
    'importance': '', 'urgency': '', 'effort': 'medium', 'control': 'influence',
    'deadline': '', 'plan': '', 'planSrc': 'auto', 'habitFreq': '',
    'habitCount': 1, 'habitTimes': [], 'habitHistory': {}, 'parentId': '',
    'tags': [], 'createdAt': stamp(2026, 9, 8), 'updatedAt': stamp(2026, 9, 8),
    'doneAt': 0,
}


# This is a unit test.
def test_a_populated_card_at_both_levels():
    """`line` carries the list's nine facts; `full` adds the block's own."""
    line = render_card(FULL_CARD, level='line', labels=LABELS, now=NOW)
    assert line == (
        'C-042 “Plan a long weekend in the mountains” · task · Travel · '
        'In Progress · important, urgent · due 2026-09-12 (in 4 days) · '
        'plan 2027-03 · tags: mountains, autumn · 3 child cards')
    # One card per line, or a list of eighty is a wall of text.
    assert '\n' not in line

    block = render_card(FULL_CARD, level='full', labels=LABELS, now=NOW)
    assert block == '\n'.join([
        'C-042 “Plan a long weekend in the mountains”',
        'type: task',
        'life area: Travel',
        'status: In Progress',
        'priority: important, urgent',
        'deadline: 2026-09-12 (in 4 days)',
        'plan: 2027-03',
        'effort: medium',
        'control: I can act',
        'tags: mountains, autumn',
        'children: 3 child cards',
        'captured: 2026-08-30',
        'updated: 2026-09-07',
        'notes: Two nights, somewhere with a train station.',
    ])
    # A finished card states the day it was finished; this one is not finished,
    # so it is not asked. A habit's cadence is the same kind of row.
    done = render_card({**FULL_CARD, 'columnId': 'answered',
                        'doneAt': stamp(2026, 9, 5), 'habitFreq': 'weekly',
                        'habitCount': 3}, level='full', labels=LABELS, now=NOW)
    assert 'status: Done' in done and 'finished: 2026-09-05' in done
    assert 'repeats: 3× weekly' in done


# This is a unit test.
def test_a_nearly_empty_card_reads_every_absence_as_an_absence():
    """No `None`, no `0`, no blank: a model shown `deadline: 0` will
    eventually report a card as due in 1970."""
    line = render_card(BARE_CARD, level='line', labels=LABELS, now=NOW)
    assert line == (
        '(no ledger number yet) “Call the dentist” · question · uncategorised · '
        'Inbox · no priority set · no deadline · no plan · no tags')
    # `C-000` is the shape this guards: `cardLabel` would produce it for a
    # proposal, which is why `ensureNums` exists on the other side.
    assert 'C-0' not in line

    block = render_card(BARE_CARD, level='full', labels=LABELS, now=NOW)
    for absence in ('priority: no priority set', 'deadline: none',
                    'plan: none', 'tags: none', 'notes: none'):
        assert absence in block
    # Nobody counted the children, so nothing is claimed about them — an
    # uncounted card is not a childless card.
    assert 'child' not in block
    assert not re.search(r'None|null|undefined', block)
    # A standalone 0 or bare dash as a value; the digits inside a date are not.
    assert not re.search(r'(?<![\w-])[0-](?![\w-])', block)


# This is a unit test.
def test_the_boards_vocabulary_is_translated_where_an_id_would_mislead():
    """`answered` is Done, a category id is its label, a plan keeps its
    precision — and `control: none` is worded, being the one id a reader would
    take for an absence."""
    done = render_card({**FULL_CARD, 'columnId': 'answered'}, now=NOW)
    assert ' · Done · ' in done and 'answered' not in done

    # A category the registry no longer holds is uncategorised, never a raw id:
    # the board itself blanks one `cleanCard` cannot find.
    dropped = render_card(FULL_CARD, labels={'home': 'Home'}, now=NOW)
    assert ' · uncategorised · ' in dropped and 'travel' not in dropped
    # No registry supplied is not the same claim — the id is all there is.
    assert ' · travel · ' in render_card(FULL_CARD, now=NOW)

    # A plan is a partial date, and stating a year as a day would invent one.
    for given in ('2027', '2027-03', '2027-03-04'):
        assert f'plan {given} ·' in render_card({**FULL_CARD, 'plan': given},
                                                labels=LABELS, now=NOW)

    hands = render_card({**FULL_CARD, 'control': 'none'}, level='full',
                        labels=LABELS, now=NOW)
    assert 'control: out of my hands' in hands
    # A control value added to `fields.py` and left unworded would print raw.
    assert set(CONTROL_WORDS) == set(get_args(Control))


# This is a unit test.
def test_the_deadline_phrase_is_dated_by_the_clock_it_is_given():
    """The one relative phrase, and the only reason the clock is an argument."""
    card = {**FULL_CARD, 'deadline': '2026-09-08'}
    assert 'due 2026-09-08 (today)' in render_card(card, now=NOW)
    assert 'due 2026-09-08 (1 day overdue)' in render_card(
        card, now=datetime(2026, 9, 9, 0, 30))
    assert 'due 2026-09-08 (in 3 days)' in render_card(
        card, now=datetime(2026, 9, 5, 23, 30))
