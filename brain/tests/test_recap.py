"""The daily recap tool — what "what were my concerns and thoughts?" is
answered from.

Today the agent answers that question from whatever retrieval happens to
surface, which is how it answered it wrongly. The recap tool answers it from
the data instead, filtered to one day: cards created that day (board.db, read
through the Node API like every board read), that day's chunks in the chat
index (Chroma, via `created_day`), the day's message counts from the chat
record, and a summary of the day's *user* messages written by the chat model.
These tests pin the filtering and the shape of the result — never the prose.

The intended contract, for the reviewer:

    make_recap_tool(client: BoardClient, store=None, llm=None, today=None)

returns a `daily_recap` tool taking {'day': 'yesterday' | 'today'} and
answering a dict with:
    cards               {'count': int, 'titles': [str, ...]}   (that day only)
    chunks              int — chunks in Chroma stamped with that created_day
    labels              [str, ...] — each chunk's `label`/`summary` metadata,
                        when any chunk carries one; [] otherwise
    user_messages       int — messages the user sent to the assistant that day
    assistant_messages  int — the assistant's replies that day
    text                the minimum sentence: "yesterday you made N cards with
                        titles … and you sent M messages to the assistant."
    summary             the model's summary of that day's user messages
`today=` is a test seam mirroring resolve_time_scope's: it anchors which date
"yesterday" means, so the test cannot flake across a UTC midnight.
"""
from datetime import datetime, timedelta, timezone

import httpx
import respx

from lodestar_brain.llm import FakeChat
from lodestar_brain.retrieval import (MEMORY_URL, ChatStore,
                                      LexicalHashEmbeddings, split_text)
from lodestar_brain.tools.board import BoardClient
from lodestar_brain.tools.recap import make_recap_tool

# Anchored once, at import: the tool is handed ANCHOR.date() as `today`, so the
# fixtures and the filter always agree on which day "yesterday" names.
ANCHOR = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                            microsecond=0)
YESTERDAY = ANCHOR - timedelta(days=1)


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def card(id, title, created):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': [], 'createdAt': ms(created),
            'updatedAt': ms(created)}


# Two cards from yesterday, one from today, one older — the filter has three
# ways to leak and each would change a count below.
CARDS = [
    card('y1', 'Call the landlord about the lease', YESTERDAY),
    card('y2', 'Why am I sleeping so badly?', YESTERDAY),
    card('t1', 'Book the flights to Shiraz', ANCHOR),
    card('o1', 'Learn the santur', YESTERDAY - timedelta(days=30)),
]

# Yesterday: two user messages, two assistant replies. Today: one user message,
# which must reach neither yesterday's counts nor yesterday's summary.
MESSAGES = [
    {'id': 1, 'role': 'user', 'createdAt': ms(YESTERDAY),
     'content': 'I am worried about the lease renewal'},
    {'id': 2, 'role': 'assistant', 'createdAt': ms(YESTERDAY),
     'content': 'Let us look at your options together.'},
    {'id': 3, 'role': 'user', 'createdAt': ms(YESTERDAY),
     'content': 'Also I keep waking up at 4am'},
    {'id': 4, 'role': 'assistant', 'createdAt': ms(YESTERDAY),
     'content': 'That sounds rough — want a wind-down habit?'},
    {'id': 5, 'role': 'user', 'createdAt': ms(ANCHOR),
     'content': 'Plan the trip to Shiraz'},
]


def mock_board():
    respx.get('http://board.test/api/state').mock(
        return_value=httpx.Response(200, json={'version': 1, 'cards': CARDS}))
    respx.get('http://board.test/api/chat/messages').mock(
        return_value=httpx.Response(200, json={'messages': MESSAGES}))
    return BoardClient('http://board.test')


# This is an integration test (in-process Chroma, respx-mocked board API).
@respx.mock
def test_daily_recap_reports_yesterday_from_board_chroma_and_chat():
    client = mock_board()
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(), collection='recap')
    store.index_messages(MESSAGES)   # stamps created_day from each createdAt

    tool = make_recap_tool(client, store=store, llm=FakeChat(),
                           today=ANCHOR.date())
    assert tool.name == 'daily_recap'
    recap = tool.run({'day': 'yesterday'})

    # board.db, filtered to the day: yesterday's two cards and no others.
    assert recap['cards']['count'] == 2
    assert sorted(recap['cards']['titles']) == [
        'Call the landlord about the lease', 'Why am I sleeping so badly?']

    # Chroma, filtered to the day: exactly the chunks yesterday's four messages
    # produced — computed from split_text so the assert never encodes a chunk
    # size, and never counts today's message.
    expected = sum(len(split_text(m['content'])) for m in MESSAGES
                   if m['createdAt'] == ms(YESTERDAY))
    assert recap['chunks'] == expected

    # No chunk carries a label or summary, so the fallback is the counts.
    assert recap['labels'] == []
    assert recap['user_messages'] == 2
    assert recap['assistant_messages'] == 2

    # The minimum sentence, built from the data above — today's card must not
    # leak into it.
    text = recap['text'].lower()
    assert 'yesterday you made 2 cards' in text
    assert 'call the landlord about the lease' in text
    assert 'why am i sleeping so badly?' in text
    assert 'you sent 2 messages to the assistant' in text
    assert 'shiraz' not in text

    # The day's user messages — and only those — went to the chat model.
    # FakeChat echoes its prompt back, so the summary shows what it was sent.
    summary = recap['summary']
    assert 'lease renewal' in summary and 'waking up at 4am' in summary
    assert 'assistant' not in summary.lower() or 'options together' not in summary
    assert 'Shiraz' not in summary

    # The same tool answers for today, so the day filter is a parameter and
    # not a hard-coded "yesterday".
    today = tool.run({'day': 'today'})
    assert today['cards']['titles'] == ['Book the flights to Shiraz']
    assert today['user_messages'] == 1


class LabelledStore:
    """A chunk index whose chunks carry labels — the branch Chroma data can't
    reach yet, pinned through the same seam the real ChatStore will grow:
    chunks_on(day_int) -> that day's chunk metadata dicts."""

    def chunks_on(self, day):
        return [{'created_day': day, 'label': 'lease worries'},
                {'created_day': day, 'summary': 'sleep trouble'},
                {'created_day': day}]


# This is a unit test.
@respx.mock
def test_recap_gives_labels_and_summaries_when_the_chunks_carry_them():
    tool = make_recap_tool(mock_board(), store=LabelledStore(), llm=FakeChat(),
                           today=ANCHOR.date())
    recap = tool.run({'day': 'yesterday'})
    # Labels come from whichever key a chunk has; a chunk with neither adds
    # nothing rather than a blank.
    assert recap['labels'] == ['lease worries', 'sleep trouble']
    assert recap['chunks'] == 3
    # "only give their labels or summaries if they exist" — so the labels are
    # in the sentence; the counts stay in the structured result regardless.
    assert 'lease worries' in recap['text']
    assert recap['assistant_messages'] == 2
