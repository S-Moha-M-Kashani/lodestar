"""Board tools — and none of them writes a card.

`create_card` proposes one and `update_card` suggests a change to one; both wait
for the user, who applies them by saving the board themselves. So `BoardClient`
carries no whole-board PUT at all, which is what retires the old rule here about
never sending a partial card list: there is no list to send. The agent reads the
board, and asks.

The client itself lives in `board/client.py` and is imported here rather than
re-exported for old times' sake: `make_board_tools` takes one, and the reads all
go through a `BoardSnapshot` so that a turn reaching three tools fetches the
board once.
"""
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..board.client import BoardClient
from ..board.snapshot import BoardSnapshot, board_of
from .dual import with_sync_door

COLUMNS = ['inbox', 'in-progress', 'answered']
TYPES = ['question', 'problem', 'task', 'idea', 'plan', 'habit']
# A habit's cadence travels with the proposal; its *history* deliberately does
# not. There is no tool for ticking a habit — logging a repetition is the user's
# act, and a record an agent could write into is not a record of anything.
HABIT_FREQS = ['daily', 'weekly', 'monthly', 'yearly']
# Categories are a user-defined registry (add/remove in the app, stored by the
# Node server) — so 'category' is a plain string here, validated server-side:
# an id the registry doesn't know is stored as '' (uncategorized), never an error.
CATEGORY_HELP = ("a category id from the user's own registry (e.g. work, love, "
                 "health — list_cards shows what's in use), or '' for "
                 "uncategorized")
FREQUENCY_HELP = 'habits only: which calendar period the count applies to'

Column = Literal['inbox', 'in-progress', 'answered']
CardType = Literal['question', 'problem', 'task', 'idea', 'plan', 'habit']
Frequency = Literal['daily', 'weekly', 'monthly', 'yearly', '']
Rank = Literal['high', 'low', '']


def _brief(c: dict) -> dict:
    return {'id': c['id'], 'title': c['title'], 'columnId': c['columnId'],
            'type': c.get('type', 'question'), 'category': c.get('category', ''),
            'importance': c.get('importance', ''),
            'urgency': c.get('urgency', ''), 'tags': c.get('tags') or [],
            'notes': c.get('notes', '')}


class ListCardsArgs(BaseModel):
    # '' has to stay legal: the old schema let the model omit the filter, and a
    # model passing it explicitly must get the unfiltered board, not an error.
    column_id: Literal['inbox', 'in-progress', 'answered', ''] = ''
    search: str = Field('', description='match in title/notes/tags')


class CreateCardArgs(BaseModel):
    title: str = Field(description="the card's text")
    notes: str = ''
    type: CardType = 'question'
    category: str = Field('', description=CATEGORY_HELP)
    column_id: Column = 'inbox'
    frequency: Frequency = Field('', description=FREQUENCY_HELP)
    times_per_period: int = Field(1, ge=1, le=99, description=(
        'habits only: repetitions per period '
        '(2 with frequency "daily" means twice a day)'))
    tags: list[str] = []


class UpdateCardArgs(BaseModel):
    id: str
    title: str | None = None
    notes: str | None = None
    type: CardType | None = None
    category: str | None = Field(None, description=CATEGORY_HELP)
    column_id: Column | None = None
    importance: Rank | None = None
    urgency: Rank | None = None
    tags: list[str] | None = None


def make_board_tools(board: BoardClient | BoardSnapshot) -> list[BaseTool]:
    """The three board tools, reading through one snapshot.

    `create_app` hands in the snapshot it shares with `find_related` and
    `daily_recap`, which is what makes a turn's board one fetch. A caller with a
    bare client gets a snapshot of its own and the fetch-per-call it always had.
    """
    # Which board these tools operate on comes from the run config, never from
    # the model: the user chose a board in the picker, and that choice is not
    # something a tool call should be able to name, mistype or be argued out of.
    # '' means the board API's own default. See `board_of` in board/snapshot.py.
    snapshot = BoardSnapshot.around(board)
    client = snapshot.client

    @tool('list_cards', args_schema=ListCardsArgs)
    async def list_cards(column_id: str = '', search: str = '',
                         config: RunnableConfig = None) -> list[dict]:
        """List cards on the board, optionally filtered by column or free text."""
        cards = await snapshot.cards(config)
        if column_id:
            cards = [c for c in cards if c['columnId'] == column_id]
        if search:
            q = search.lower()
            cards = [c for c in cards
                     if q in c['title'].lower() or q in (c.get('notes') or '').lower()
                     or any(q in t for t in (c.get('tags') or []))]
        return [_brief(c) for c in cards]

    @tool('create_card', args_schema=CreateCardArgs)
    async def create_card(title: str, notes: str = '', type: str = 'question',
                        category: str = '', column_id: str = 'inbox',
                        tags: list | None = None, frequency: str = '',
                        times_per_period: int = 1,
                        config: RunnableConfig = None) -> dict:
        """Propose a new card (question, problem, task, idea, plan or habit).

        A habit is something repeated on a schedule — give it a frequency and
        how many times per period. The user must approve the card before it
        appears on the board, so tell them you have proposed it — never claim
        it was added.
        """
        card = {'title': title, 'notes': notes, 'type': type,
                'category': category, 'columnId': column_id, 'tags': tags or []}
        # A cadence only means something on a habit; sending one with a question
        # would leave a dormant "2× per day" on a card nobody repeats.
        if type == 'habit':
            card['habitFreq'] = frequency or 'daily'
            card['habitCount'] = times_per_period
        proposal = await client.create_proposal(card, board_of(config))
        # `pending` tells the model the card is not on the board yet, so it
        # reports a proposal instead of claiming it added something.
        return {**_brief(proposal), 'pending': True}

    @tool('update_card', args_schema=UpdateCardArgs)
    async def update_card(id: str, title: str | None = None, notes: str | None = None,
                        type: str | None = None, category: str | None = None,
                        column_id: str | None = None,
                        importance: str | None = None, urgency: str | None = None,
                        tags: list | None = None,
                        config: RunnableConfig = None) -> dict:
        """Suggest a change to an existing card (move columns, set type/category,
        importance/urgency, tags, or add findings to notes).

        The change is NOT applied. It goes to the user as a suggestion they open,
        adjust if they want, and save themselves — so say you have suggested an
        edit, and never that you made one.
        """
        # From the snapshot like every other read. A proposal or an edit filed
        # earlier in this turn is invisible to a fresh fetch too — neither writes
        # to `cards` — so there is nothing here a re-read could have found.
        cards = await snapshot.cards(config)
        target = next((c for c in cards if c['id'] == id), None)
        if target is None:
            return {'error': f'no card with id {id!r} — use list_cards first'}
        # Only the fields this call named. A suggestion carrying every field would
        # overwrite the untouched ones with values that were current when the
        # model looked, which is a stale-write dressed as an edit.
        named = {'title': title, 'notes': notes, 'type': type,
                 'category': category, 'columnId': column_id,
                 'importance': importance, 'urgency': urgency, 'tags': tags}
        fields = {key: value for key, value in named.items() if value is not None}
        if not fields:
            return {'error': 'name at least one field to change'}
        suggestion = await client.create_edit(id, fields)
        # `pending` is the same signal create_card sends, so the model reports a
        # suggestion rather than claiming the board changed.
        return {'id': suggestion.get('id', ''), 'cardId': id, 'fields': fields,
                'title': target['title'], 'pending': True}

    # Both doors, because the sync one is how the evals and the tests call these.
    return [with_sync_door(t) for t in (list_cards, create_card, update_card)]
