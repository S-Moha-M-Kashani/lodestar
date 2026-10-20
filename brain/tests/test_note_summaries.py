"""A shorter reading of long notes, written through the board, never onto the card."""
import asyncio
import re


THREE = 'Keep the hard part. Name the next step. Stop.'
FOUR = 'One idea. Two ideas. Three ideas. Four ideas.'


def _sized(marker, n):
    head = marker + ' '
    return head + ('a' * (n - len(head)))


def _card(card_id, notes):
    return {'id': card_id, 'columnId': 'inbox', 'title': card_id, 'notes': notes,
            'type': 'task', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1}


def _sentences(text):
    return [part for part in re.split(r'[.!?]+', text) if part.strip()]


def _prompt_text(prompt):
    if prompt is None:
        return ''
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, dict):
        return _prompt_text(prompt.get('content', ''))
    if isinstance(prompt, (list, tuple)):
        return '\n'.join(_prompt_text(part) for part in prompt)
    content = getattr(prompt, 'content', None)
    if content is not None and content is not prompt:
        return _prompt_text(content)
    return str(prompt)


class _Reply(str):
    """A string the producer can also read as a chat message."""

    @property
    def content(self):
        return str(self)


class CountingChat:
    """Returns a fixed string per call. FOURNOTE notes get four sentences."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def _take(self, prompt):
        self.calls += 1
        text = _prompt_text(prompt)
        self.prompts.append(text)
        reply = FOUR if 'FOURNOTE' in text else THREE
        return _Reply(reply)

    def invoke(self, prompt, *args, **kwargs):
        return self._take(prompt)

    async def ainvoke(self, prompt, *args, **kwargs):
        return self._take(prompt)


class MemoryBoard:
    """In-memory stand-in. Readings round-trip. Card notes are not a write path."""

    def __init__(self, cards):
        self.cards = [dict(card) for card in cards]
        self.by_model = {}

    async def list_board(self, board_id=''):
        return {'version': 1, 'cards': [dict(card) for card in self.cards],
                'categories': []}

    async def list_note_summaries(self, model, board_id=''):
        return [dict(row) for row in self.by_model.get(model, [])]

    async def write_note_summaries(self, model, summaries, board_id=''):
        current = {row['cardId']: dict(row) for row in self.by_model.get(model, [])}
        for row in summaries:
            if 'cardId' not in row or 'summary' not in row:
                raise AssertionError(
                    'a stored reading names cardId and summary, '
                    f'got {sorted(row)}')
            current[row['cardId']] = dict(row)
        self.by_model[model] = list(current.values())
        return {'written': len(summaries)}

    def readings(self):
        return [row for rows in self.by_model.values() for row in rows]


# This is a unit test.
def test_long_notes_get_one_reading_and_are_not_sent_again():
    long_notes = _sized('LONGNOTE', 600)
    short_notes = _sized('SHORTNOTE', 599)
    four_notes = _sized('FOURNOTE', 600)
    empty = ''
    assert len(long_notes) >= 600 and len(four_notes) >= 600
    assert len(short_notes) < 600 and empty == ''
    assert len(_sentences(THREE)) <= 3 and len(_sentences(FOUR)) == 4

    board = MemoryBoard([
        _card('long', long_notes),
        _card('short', short_notes),
        _card('empty', empty),
        _card('four', four_notes),
    ])
    originals = {card['id']: card['notes'] for card in board.cards}
    chat = CountingChat()

    from lodestar_brain.server import fill_note_summaries

    asyncio.run(fill_note_summaries(board, chat))

    stored = board.readings()
    assert [row['cardId'] for row in stored] == ['long']
    assert stored[0]['summary'] == THREE
    assert isinstance(stored[0]['summary'], str)
    assert len(_sentences(stored[0]['summary'])) <= 3
    assert {card['id']: card['notes'] for card in board.cards} == originals
    assert chat.calls == 2
    assert 'SHORTNOTE' not in '\n'.join(chat.prompts)
    asked = sum('LONGNOTE' in prompt for prompt in chat.prompts)
    assert asked == 1

    asyncio.run(fill_note_summaries(board, chat))

    assert sum('LONGNOTE' in prompt for prompt in chat.prompts) == asked
    assert [row['cardId'] for row in board.readings()] == ['long']
    assert {card['id']: card['notes'] for card in board.cards} == originals
