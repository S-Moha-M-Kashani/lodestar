"""Board tools. All writes go through the Node API so the soft-delete
durability guarantee holds. CRITICAL: PUT /api/state soft-deletes any card
omitted from the payload — every save must send the FULL card list."""
from typing import Literal

import httpx
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

COLUMNS = ['inbox', 'in-progress', 'answered']
TYPES = ['question', 'problem', 'task', 'idea', 'plan']
# Categories are a user-defined registry (add/remove in the app, stored by the
# Node server) — so 'category' is a plain string here, validated server-side:
# an id the registry doesn't know is stored as '' (uncategorized), never an error.
CATEGORY_HELP = ("a category id from the user's own registry (e.g. work, love, "
                 "health — list_questions shows what's in use), or '' for "
                 "uncategorized")

Column = Literal['inbox', 'in-progress', 'answered']
CardType = Literal['question', 'problem', 'task', 'idea', 'plan']
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


class ListQuestionsArgs(BaseModel):
    # '' has to stay legal: the old schema let the model omit the filter, and a
    # model passing it explicitly must get the unfiltered board, not an error.
    column_id: Literal['inbox', 'in-progress', 'answered', ''] = ''
    search: str = Field('', description='match in title/notes/tags')


class CreateQuestionArgs(BaseModel):
    title: str = Field(description="the card's text")
    notes: str = ''
    type: CardType = 'question'
    category: str = Field('', description=CATEGORY_HELP)
    column_id: Column = 'inbox'
    tags: list[str] = []


class UpdateQuestionArgs(BaseModel):
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
    @tool('list_questions', args_schema=ListQuestionsArgs)
    def list_questions(column_id: str = '', search: str = '') -> list[dict]:
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

    @tool('create_question', args_schema=CreateQuestionArgs)
    def create_question(title: str, notes: str = '', type: str = 'question',
                        category: str = '', column_id: str = 'inbox',
                        tags: list | None = None) -> dict:
        """Propose a new card (question, problem, task, idea or plan). The user
        must approve it before it appears on the board, so tell them you have
        proposed it — never claim it was added."""
        proposal = client.create_proposal(
            {'title': title, 'notes': notes, 'type': type,
             'category': category, 'columnId': column_id, 'tags': tags or []})
        # `pending` tells the model the card is not on the board yet, so it
        # reports a proposal instead of claiming it added something.
        return {**_brief(proposal), 'pending': True}

    @tool('update_question', args_schema=UpdateQuestionArgs)
    def update_question(id: str, title: str | None = None, notes: str | None = None,
                        type: str | None = None, category: str | None = None,
                        column_id: str | None = None,
                        importance: str | None = None, urgency: str | None = None,
                        tags: list | None = None) -> dict:
        """Update fields of an existing card (move columns, set type/category,
        importance/urgency, tags, or append findings to notes)."""
        cards = client.list_cards()
        target = next((c for c in cards if c['id'] == id), None)
        if target is None:
            return {'error': f'no card with id {id!r} — use list_questions first'}
        updates = {'title': title, 'notes': notes, 'type': type,
                   'category': category, 'columnId': column_id,
                   'importance': importance, 'urgency': urgency, 'tags': tags}
        for key, value in updates.items():
            if value is not None:
                target[key] = value
        client.save_cards(cards)  # full list — never partial
        return _brief(target)

    return [list_questions, create_question, update_question]
