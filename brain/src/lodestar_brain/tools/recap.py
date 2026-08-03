"""One day of the user's life, read back from the data.

"What were my concerns and thoughts?" used to be answered from whatever
similarity search happened to surface — plausible and wrong. This tool answers
it from the records instead: the day's cards from the board (board.db, through
the Node API like every board read), the day's chunks from the chat index
(Chroma, by their created_day stamp), the day's message counts from the chat
record, and a model-written summary of what the *user* said that day.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from ..retrieval import day_int
from .board import BoardClient


class DailyRecapArgs(BaseModel):
    day: Literal['yesterday', 'today'] = 'yesterday'


def _content(message) -> str:
    """A reply's content is a string, or parts when a provider streams them."""
    content = message.content
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


def make_recap_tool(client: BoardClient, store=None, llm=None,
                    today: date | None = None) -> BaseTool:
    """`store` is anything with chunks_on(day_int) — a ChatStore, or None when
    Chroma is off, which costs the chunk count and never the recap. `today` is
    a test seam like resolve_time_scope's: it anchors which date 'yesterday'
    names, so a test cannot flake across a UTC midnight."""

    @tool('daily_recap', args_schema=DailyRecapArgs)
    def daily_recap(day: str = 'yesterday') -> dict:
        """What one day actually held: the cards created, the messages sent to
        the assistant, what the conversations were about, and a summary of the
        user's own words. Use it when asked about the user's concerns, thoughts
        or day — never answer that from memory."""
        anchor = today or datetime.now(timezone.utc).date()
        target = anchor - timedelta(days=1) if day == 'yesterday' else anchor
        # The same UTC day-int the chunk metadata carries — see day_int.
        wanted = target.year * 10000 + target.month * 100 + target.day

        cards = [c for c in client.list_cards()
                 if day_int(c.get('createdAt')) == wanted]
        titles = [c.get('title', '') for c in cards]

        chunks = store.chunks_on(wanted) if store is not None else []
        # A label only when a chunk carries one — a blank for every unlabelled
        # chunk would describe the index, not the day.
        labels = [meta['label'] if 'label' in meta else meta['summary']
                  for meta in chunks if 'label' in meta or 'summary' in meta]

        messages = [m for m in client.list_chat()
                    if day_int(m.get('createdAt')) == wanted]
        user = [m for m in messages if m.get('role') == 'user']
        assistant = [m for m in messages if m.get('role') == 'assistant']

        titled = ', '.join(f'"{t}"' for t in titles)
        text = (f'{day} you made {len(cards)} cards'
                + (f' with titles {titled}' if titled else '')
                + f' and you sent {len(user)} messages to the assistant.')
        if labels:
            text += ' The conversations were about: ' + ', '.join(labels) + '.'

        # The summary reads only what the user said — the assistant's replies
        # summarised back would be the model quoting itself as the user's mind.
        summary = ''
        if llm is not None and user:
            prompt = ("Summarize in a few sentences what was on the user's "
                      'mind in these messages, in their own terms:\n'
                      + '\n'.join('- ' + m.get('content', '') for m in user))
            summary = _content(llm.invoke(prompt))

        return {'day': target.isoformat(),
                'cards': {'count': len(cards), 'titles': titles},
                'chunks': len(chunks), 'labels': labels,
                'user_messages': len(user),
                'assistant_messages': len(assistant),
                'text': text, 'summary': summary}

    return daily_recap
