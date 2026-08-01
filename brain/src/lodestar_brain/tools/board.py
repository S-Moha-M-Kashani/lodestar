"""Board tools. All writes go through the Node API so the soft-delete
durability guarantee holds. CRITICAL: PUT /api/state soft-deletes any card
omitted from the payload — every save must send the FULL card list."""
from typing import Literal

import httpx
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
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def list_cards(self) -> list[dict]:
        res = httpx.get(f'{self.base_url}/api/state', timeout=self.timeout)
        res.raise_for_status()
        return res.json()['cards']

    def save_cards(self, cards: list[dict]) -> list[dict]:
        res = httpx.put(f'{self.base_url}/api/state',
                        json={'version': 1, 'cards': cards}, timeout=self.timeout)
        res.raise_for_status()
        return res.json()['cards']

    def create_proposal(self, card: dict) -> dict:
        """Offer one card for the user's approval.

        Deliberately NOT save_cards: a proposal is a single card on its own
        endpoint, so it never travels through the whole-board PUT and the
        "always send the full list" contract above stays intact.
        """
        res = httpx.post(f'{self.base_url}/api/proposals',
                         json=card, timeout=self.timeout)
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
    @tool('list_cards', args_schema=ListCardsArgs)
    def list_cards(column_id: str = '', search: str = '') -> list[dict]:
        """List cards on the board, optionally filtered by column or free text."""
        cards = client.list_cards()
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
                        times_per_period: int = 1) -> dict:
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
        proposal = client.create_proposal(card)
        # `pending` tells the model the card is not on the board yet, so it
        # reports a proposal instead of claiming it added something.
        return {**_brief(proposal), 'pending': True}

    @tool('update_card', args_schema=UpdateCardArgs)
    def update_card(id: str, title: str | None = None, notes: str | None = None,
                        type: str | None = None, category: str | None = None,
                        column_id: str | None = None,
                        importance: str | None = None, urgency: str | None = None,
                        tags: list | None = None) -> dict:
        """Update fields of an existing card (move columns, set type/category,
        importance/urgency, tags, or append findings to notes)."""
        cards = client.list_cards()
        target = next((c for c in cards if c['id'] == id), None)
        if target is None:
            return {'error': f'no card with id {id!r} — use list_cards first'}
        updates = {'title': title, 'notes': notes, 'type': type,
                   'category': category, 'columnId': column_id,
                   'importance': importance, 'urgency': urgency, 'tags': tags}
        for key, value in updates.items():
            if value is not None:
                target[key] = value
        client.save_cards(cards)  # full list — never partial
        return _brief(target)

    return [list_cards, create_card, update_card]
