"""An agent edit is a suggestion the user saves, not a write.

`create_card` proposes and `update_card` suggests. Neither reaches the board.
The tool behaviour — a suggestion is posted, a whole-board PUT is never called —
is pinned in `test_widget2_write.py`. What stays here is the claim no single
tool test can make: no tool mutates the board, and the fields a suggestion may
carry are the same set the board itself will accept.
"""
from __future__ import annotations

import re
from pathlib import Path

from lodestar_brain.widget2.tools.fields import EDITABLE_BOARD_KEYS

SERVER_JS = Path(__file__).resolve().parents[2] / 'server.js'


# This is a unit test.
def test_no_tool_mutates_the_board_any_more():
    """The consequence for the route's two flags, stated once.

    No tool changes the board, so nothing should tell the browser to adopt
    server state mid-conversation. Both writers propose, and the browser
    refreshes the suggestion list instead.
    """
    from lodestar_brain.server import MUTATING_TOOLS, PROPOSING_TOOLS
    assert PROPOSING_TOOLS == {'create_card', 'update_card'}
    assert MUTATING_TOOLS == set(), (
        'a tool that mutates the board is a tool that skipped the user')


def _server_allowlist():
    """`EDITABLE_FIELDS` as `server.js` declares it.

    Parsed, because it is a module-local const in a file with no exports and
    nothing to import it from — and this side is the parsed half deliberately:
    Python can *import* its own list, so exactly one of the two is read as text
    rather than as a value. The failure that matters is the two lists
    disagreeing, and a regex that stopped matching would report that as a
    missing declaration rather than passing on an empty set.
    """
    body = re.search(r'const EDITABLE_FIELDS = \[(.*?)\];', SERVER_JS.read_text(),
                     re.DOTALL)
    assert body, f'no EDITABLE_FIELDS declaration in {SERVER_JS}'
    return set(re.findall(r"'(\w+)'", body.group(1)))


# This is a configuration invariant: two lists in two languages that have to be
# one set, asserted rather than kept in step by hand.
def test_the_boards_allowlist_and_the_tool_schema_are_the_same_set():
    """The cure for the 2026-09-02 drops, applied at the seam that caused them.

    Twice a tool advertised a field `server.js` silently dropped, and both times
    the cause was two hand-written lists. So the set is declared once, in
    `widget2/tools/fields.py`, and asserted equal here **in both directions**.
    A subset check either way would wave one of those through.
    """
    assert _server_allowlist() == set(EDITABLE_BOARD_KEYS), (
        'server.js EDITABLE_FIELDS and widget2 EDITABLE_BOARD_KEYS have '
        f'drifted: server-only {_server_allowlist() - set(EDITABLE_BOARD_KEYS)}, '
        f'schema-only {set(EDITABLE_BOARD_KEYS) - _server_allowlist()}')

    # And what stays out stays out on *both* sides, stated as intent rather than
    # left as an absence: each has its own reason in fields.py's docstring, and
    # the equality above would be just as satisfied by adding them to both.
    for withheld in ('parentId', 'habitHistory', 'doneAt', 'planSrc',
                     'effortSrc', 'controlSrc', 'num'):
        assert withheld not in EDITABLE_BOARD_KEYS
        assert withheld not in _server_allowlist()
