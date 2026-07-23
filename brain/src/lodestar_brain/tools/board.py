"""Board tools. All writes go through the Node API so the soft-delete
durability guarantee holds. CRITICAL: PUT /api/state soft-deletes any card
omitted from the payload — every save must send the FULL card list."""
import httpx

from .base import Tool

COLUMNS = ['inbox', 'in-progress', 'answered']
TYPES = ['question', 'problem', 'task', 'idea', 'plan']
CATEGORIES = ['work', 'love', 'family', 'health', 'mind', 'music', 'travel', 'home', 'money']


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


def _brief(c: dict) -> dict:
    return {'id': c['id'], 'title': c['title'], 'columnId': c['columnId'],
            'type': c.get('type', 'question'), 'category': c.get('category', ''),
            'importance': c.get('importance', ''),
            'urgency': c.get('urgency', ''), 'tags': c.get('tags') or [],
            'notes': c.get('notes', '')}


def make_board_tools(client: BoardClient) -> list[Tool]:
    def list_questions(column_id: str = '', search: str = '') -> list[dict]:
        cards = client.list_cards()
        if column_id:
            cards = [c for c in cards if c['columnId'] == column_id]
        if search:
            q = search.lower()
            cards = [c for c in cards
                     if q in c['title'].lower() or q in (c.get('notes') or '').lower()
                     or any(q in t for t in (c.get('tags') or []))]
        return [_brief(c) for c in cards]

    def create_question(title: str, notes: str = '', type: str = 'question',
                        category: str = '', column_id: str = 'inbox',
                        tags: list | None = None) -> dict:
        cards = client.list_cards()
        known = {c['id'] for c in cards}
        new_card = {'title': title, 'notes': notes, 'type': type,
                    'category': category, 'columnId': column_id, 'tags': tags or []}
        saved = client.save_cards(cards + [new_card])  # server assigns id/num
        created = [c for c in saved if c['id'] not in known]
        return _brief(created[0]) if created else {'error': 'card was not created'}

    def update_question(id: str, title: str | None = None, notes: str | None = None,
                        type: str | None = None, category: str | None = None,
                        column_id: str | None = None,
                        importance: str | None = None, urgency: str | None = None,
                        tags: list | None = None) -> dict:
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

    enum = {'column': {'type': 'string', 'enum': COLUMNS},
            'card_type': {'type': 'string', 'enum': TYPES},
            'category': {'type': 'string', 'enum': CATEGORIES + ['']}}
    return [
        Tool('list_questions',
             'List cards on the board, optionally filtered by column or free text.',
             {'type': 'object', 'properties': {
                 'column_id': enum['column'],
                 'search': {'type': 'string', 'description': 'match in title/notes/tags'}},
              'required': []},
             list_questions),
        Tool('create_question',
             'Add a new card (question, problem, task, idea or plan) to the board.',
             {'type': 'object', 'properties': {
                 'title': {'type': 'string', 'description': "the card's text"},
                 'notes': {'type': 'string'},
                 'type': enum['card_type'],
                 'category': enum['category'],
                 'column_id': enum['column'],
                 'tags': {'type': 'array', 'items': {'type': 'string'}}},
              'required': ['title']},
             create_question),
        Tool('update_question',
             'Update fields of an existing card (move columns, set type/category, '
             'importance/urgency, tags, or append findings to notes).',
             {'type': 'object', 'properties': {
                 'id': {'type': 'string'},
                 'title': {'type': 'string'},
                 'notes': {'type': 'string'},
                 'type': enum['card_type'],
                 'category': enum['category'],
                 'column_id': enum['column'],
                 'importance': {'type': 'string', 'enum': ['high', 'low', '']},
                 'urgency': {'type': 'string', 'enum': ['high', 'low', '']},
                 'tags': {'type': 'array', 'items': {'type': 'string'}}},
              'required': ['id']},
             update_question),
    ]
