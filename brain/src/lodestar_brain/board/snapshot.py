"""One board fetch per turn, shared by every tool that reads it.

`list_cards` fetches `/api/state`; `find_related` fetches it again to rebuild its
index; `daily_recap` fetches it a third time; `update_card` fetches it a fourth
to look up the card it is about to suggest a change to. Four round trips for one
board that nothing changed in between, and the user waits for all four.

A `BoardSnapshot` fetches it once and hands the same list to all of them.

**What bounds the staleness, and why it is not a guess.** The window is one
*turn*, and the only writer who could race it is the user, editing their own
board in another tab during the seconds an answer takes. The agent cannot race it
at all: neither of its writing tools writes to `cards`. `create_card` files a
proposal (`pending = 1`, which `/api/state` does not return until the user
accepts it) and `update_card` files a suggested edit into a table of its own. So
a proposal or an edit made mid-turn is absent from a *fresh* fetch too, and this
copy cannot mask it — what the user sees waiting for them is the proposals list,
which the browser refreshes off the turn's `proposed` flag and never from here.
That is the same argument `middleware/cache.py` makes for its scope, and it is
made twice because the two caches are not the same cache: that one collapses the
*same tool asked the same question twice*, keyed on a named board rather than on
the board's contents, deliberately. Three different tools each needing the board
are three different keys there, and one key here.

**Outside a turn there is no snapshot.** `/rag/reindex`, `/rag/recall`, and any
tool a test or an eval calls directly carry no turn id, and every one of them
fetches exactly as often as it did before. A cache with no bound on how stale it
may get is not something this board should be storing a life in.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .client import BoardClient

# How many turns' boards to keep. Entries are keyed by turn, so ordinary use
# evicts itself and this is only what happens when it does not — a handful,
# because each entry is a whole board and one turn is the only one being served.
MAX_TURNS = 8


def board_of(config: Any) -> str:
    """Which board this call is about. '' means the board API's own default.

    From the run config, never from the model: the user chose a board in the
    picker, and that choice is not something a tool call should be able to name,
    mistype or be argued out of.
    """
    return (config or {}).get('configurable', {}).get('board_id') or ''


def turn_of(config: Any) -> str:
    """Which turn this call belongs to, or '' for a call outside one.

    Minted once per turn by `LodestarAgent._run_config`, so every tool call the
    model makes while answering one question carries the same id. A caller with
    no agent behind it — a route, an eval, a test — has no turn, and gets no
    snapshot.
    """
    return (config or {}).get('configurable', {}).get('turn_id') or ''


class BoardSnapshot:
    """The board as it was when this turn started.

    Per instance, not per module: two brains serve two boards, and a
    process-wide snapshot would answer one board's question with the other's
    cards. `client` stays public — `daily_recap` also reads the chat record,
    which is a different endpoint and has no reason to be snapshotted.
    """

    def __init__(self, client: BoardClient, max_turns: int = MAX_TURNS):
        self.client = client
        self.max_turns = max_turns
        self._cards: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
        # Which board each index was last built over, so a turn that reaches
        # `find_related` twice re-derives nothing. See `indexed`.
        self._built: dict[int, tuple[str, str]] = {}

    @classmethod
    def around(cls, board: BoardClient | BoardSnapshot) -> BoardSnapshot:
        """A snapshot around whatever the caller had.

        `create_app` builds one and hands it to all three tool factories — that
        sharing is the entire saving. A direct caller (an eval, a test, a curl)
        hands a bare client and gets a snapshot of its own, which without a turn
        id fetches exactly as often as the client did.
        """
        return board if isinstance(board, cls) else cls(board)

    async def cards(self, config: Any = None) -> list[dict]:
        """Every card on the board this turn is about, fetched at most once.

        The *same list object* each time within a turn, which is what lets
        `CardIndex.build` recognise an unchanged board by fingerprint instead of
        being handed a fresh copy that only looks the same.

        Deliberately no lock around the fetch. Two tools in one turn are run one
        after another by the graph, so the race needs two turns arriving at once
        on the same board — and what it costs is the fetch this exists to save,
        not a wrong answer.
        """
        turn, board = turn_of(config), board_of(config)
        if not turn:
            return await self.client.list_cards(board)
        key = (turn, board)
        if key in self._cards:
            self._cards.move_to_end(key)
            return self._cards[key]
        cards = await self.client.list_cards(board)
        self._cards[key] = cards
        while len(self._cards) > self.max_turns:
            self._cards.popitem(last=False)
        return cards

    async def indexed(self, index: Any, config: Any = None) -> list[dict]:
        """This turn's cards, with `index` built over them — once per turn.

        `index` is anything with `build(cards)`; `CardIndex` is the only one, and
        it already skips re-embedding a board whose blake2b fingerprint has not
        changed. That skip is what made a rebuild-per-tool-call affordable, and
        this is the other half of it: within a turn the documents are not even
        re-derived and the digest is not recomputed, because the board they would
        be derived from is the one already indexed.
        """
        cards = await self.cards(config)
        turn, board = turn_of(config), board_of(config)
        if turn and self._built.get(id(index)) == (turn, board):
            return cards
        index.build(cards)
        if turn:
            self._built[id(index)] = (turn, board)
        return cards


__all__ = ['MAX_TURNS', 'BoardSnapshot', 'board_of', 'turn_of']

"""Alternatives considered
========================

Why did you write your own request cache?
-----------------------------------------

Because what has to be cached is *one turn's worth of one endpoint*, and the
lifetime is the part no library could have decided. A turn is not a duration, an
ETag or a number of seconds — it is "for as long as nothing the agent can do
changes the answer", which on this board is a provable window rather than a
guess, and the proof is in the module docstring above.

**Why the obvious option fails.** The obvious option is `functools.lru_cache` on
`BoardClient.list_cards`. It fails on the key: a module-level cache knows the
board id and nothing else, so the entry from the last question is served to the
next one, and the user who moved a card between two questions gets told it is
still in Inbox. Nothing raises and the answer stays plausible, which is the worst
shape a caching bug can take. Adding a turn to that key means reaching the run
config from inside the client — the client would have to learn what an agent turn
is, and that is the wrong direction for the one module that must stay a thin
transport.

**Why not the framework.** `middleware/cache.py` is already the framework's seam
(`awrap_tool_call`) doing the adjacent job, and its key is `(tool, arguments,
board)` — three *different* tools each fetching the board are three different
keys there, so it cannot collapse them and was never meant to. httpx has no cache
of its own; it delegates to the HTTP layer, and `/api/state` sends no
`Cache-Control` and no `ETag`, so there is nothing for an HTTP cache to
revalidate against.

**The libraries that would do it** (checked 2026-08-13):

- **`hishel`** — a real HTTP cache transport for httpx, RFC-9111 correct. The
  right answer if `/api/state` carried validators; today it would cache nothing,
  because a response with no freshness information is not cacheable.
- **`cachetools`** — `TTLCache`, eviction policy written for you. A TTL is a
  guess where the turn is a fact, and the guess is wrong in both directions: too
  long and a card the user just moved is invisible, too short and the three tools
  in one turn still fetch three times.
- **`requests-cache`** / **`aiohttp-client-cache`** — persistent HTTP caches, and
  persistence is the opposite of what is wanted: a board edited while the brain
  was down would come back cached.
- **`aiocache`** — decorator-based, async-first, and every backend it offers is a
  service this board does not run.
- Greenfield, on someone else's budget: still an `OrderedDict`, because the
  interesting part is the key and not the storage.

**Why they were not adopted, and what would change it.** Twenty lines and no
dependency, and none of them would have known what a turn is. What would change
it: an `ETag` on `GET /api/state` in `server.js`. Then the right shape is not a
cache at all but a conditional GET — `hishel` in front of the client, one 304 per
call instead of one full board — and the measurement that decides between them is
whether a turn's board cost is dominated by transfer or by round trips. On
localhost with a few hundred cards it is round trips, which is why this exists;
on a board large enough for the payload to matter, the conditional GET wins and
this module should be deleted rather than tuned.
"""
