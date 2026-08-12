"""Board tools — and none of them writes a card.

`create_card` proposes one and `update_card` suggests a change to one; both wait
for the user, who applies them by saving the board themselves. So `BoardClient`
carries no whole-board PUT at all, which is what retires the old rule here about
never sending a partial card list: there is no list to send. The agent reads the
board, and asks.
"""
from typing import Literal

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

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


class BoardClient:
    """The board API, one board at a time.

    Every call takes an optional `board_id`, and an empty one is *omitted*
    rather than sent blank — the server has to be able to tell "no board named"
    (answer with the default board, which is what every caller written before
    boards existed relies on) from "a board named the empty string". The id
    itself never comes from the model: it rides the agent's run config.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    @staticmethod
    def _scope(board_id: str = '') -> dict:
        return {'board': board_id} if board_id else {}

    def list_cards(self, board_id: str = '') -> list[dict]:
        res = httpx.get(f'{self.base_url}/api/state',
                        params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()['cards']

    def list_chat(self, board_id: str = '') -> list[dict]:
        """The live chat record for one board, oldest first."""
        res = httpx.get(f'{self.base_url}/api/chat/messages',
                        params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def list_all_chat(self) -> list[dict]:
        """Every board's live messages, for maintaining the chat index.

        The index is one collection over the whole record and `prune` deletes
        chunks whose message is no longer live — so syncing it from a single
        board's messages would drop every other board out of recall. The only
        caller is index maintenance; everything a person reads is scoped.
        """
        res = httpx.get(f'{self.base_url}/api/chat/messages/all',
                        timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def record_chat(self, messages: list[dict],
                    session_id: str = '', board_id: str = '') -> list[dict]:
        """Append to the durable chat record (assistant.db) — through the Node
        API like every write, never SQLite directly. Returns the inserted rows
        with their ids, which is what the Chroma index chunks are keyed on.

        An empty `session_id` is omitted rather than sent as '': the server files
        an unnamed batch under its reserved 'adhoc' chat, and it can only do that
        if it can tell "no session named" from "a session named the empty
        string"."""
        payload: dict = {'messages': messages}
        if session_id:
            payload['sessionId'] = session_id
        # In the body, not the query string: this is the one chat route the
        # brain posts to, and the board travels beside the session it belongs
        # with rather than in a different part of the request.
        if board_id:
            payload['boardId'] = board_id
        res = httpx.post(f'{self.base_url}/api/chat/messages',
                         json=payload, timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def create_proposal(self, card: dict, board_id: str = '') -> dict:
        """Offer one card for the user's approval.

        On its own endpoint, never the whole-board PUT — which this client no
        longer has at all. Removing it is the guardrail: the brain cannot write a
        card even by mistake, so "never send a partial card list" stops being a
        rule anyone has to remember here.
        """
        res = httpx.post(f'{self.base_url}/api/proposals', json=card,
                         params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def create_edit(self, card_id: str, fields: dict) -> dict:
        """Offer a change to an existing card, for the user to review and save.

        The counterpart to create_proposal, and for the same reason: this cannot
        reach the whole-board PUT, so an agent edit is a note about a card rather
        than a write to one. Nothing on the board moves until the user saves.
        """
        res = httpx.post(f'{self.base_url}/api/edits',
                         json={'cardId': card_id, 'fields': fields},
                         timeout=self.timeout)
        res.raise_for_status()
        return res.json()


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


def make_board_tools(client: BoardClient) -> list[BaseTool]:
    # Which board these tools operate on. It comes from the run config, never
    # from the model: the user chose a board in the picker, and that choice is
    # not something a tool call should be able to name, mistype or be argued
    # out of. '' means the board API's own default.
    def board_of(config: RunnableConfig | None) -> str:
        return (config or {}).get('configurable', {}).get('board_id') or ''

    @tool('list_cards', args_schema=ListCardsArgs)
    def list_cards(column_id: str = '', search: str = '',
                   config: RunnableConfig = None) -> list[dict]:
        """List cards on the board, optionally filtered by column or free text."""
        cards = client.list_cards(board_of(config))
        if column_id:
            cards = [c for c in cards if c['columnId'] == column_id]
        if search:
            q = search.lower()
            cards = [c for c in cards
                     if q in c['title'].lower() or q in (c.get('notes') or '').lower()
                     or any(q in t for t in (c.get('tags') or []))]
        return [_brief(c) for c in cards]

    @tool('create_card', args_schema=CreateCardArgs)
    def create_card(title: str, notes: str = '', type: str = 'question',
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
        proposal = client.create_proposal(card, board_of(config))
        # `pending` tells the model the card is not on the board yet, so it
        # reports a proposal instead of claiming it added something.
        return {**_brief(proposal), 'pending': True}

    @tool('update_card', args_schema=UpdateCardArgs)
    def update_card(id: str, title: str | None = None, notes: str | None = None,
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
        cards = client.list_cards(board_of(config))
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
        suggestion = client.create_edit(id, fields)
        # `pending` is the same signal create_card sends, so the model reports a
        # suggestion rather than claiming the board changed.
        return {'id': suggestion.get('id', ''), 'cardId': id, 'fields': fields,
                'title': target['title'], 'pending': True}

    return [list_cards, create_card, update_card]
