"""One day of the user's life, read back from the data.

"What were my concerns and thoughts?" used to be answered from whatever
similarity search happened to surface — plausible and wrong. This tool answers
it from the records instead: the day's cards from the board (board.db, through
the Node API like every board read), the day's chunks from the chat index
(Chroma, by their created_day stamp), the day's message counts from the chat
record, and a model-written summary of what the *user* said that day.

One day is the default, not the limit: `days` widens the window backwards from
`day`, bounded at a week, so "the last 3 days" is one call whose window has a
start as well as an end — not three calls a model has to stitch together.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..board.client import BoardClient
from ..board.snapshot import BoardSnapshot, board_of
from ..retrieval import day_int
from ..retrieval.timescope import TimeScope
from .dual import with_sync_door


class DailyRecapArgs(BaseModel):
    day: Literal['yesterday', 'today'] = 'yesterday'
    # How far back the window reaches, ending at `day`. Bounded at a week: a
    # recap is a read-back of days, not analytics — wider questions belong to
    # retrieval's time language (timescope.py).
    days: int = Field(1, ge=1, le=7)


def _content(message) -> str:
    """A reply's content is a string, or parts when a provider streams them."""
    content = message.content
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


def make_recap_tool(board: BoardClient | BoardSnapshot, store=None, llm=None,
                    today: date | None = None) -> BaseTool:
    """`store` is anything with chunks_on(day_int) — a ChatStore, or None when
    Chroma is off, which costs the chunk count and never the recap. `today` is
    a test seam like resolve_time_scope's: it anchors which date 'yesterday'
    names, so a test cannot flake across a UTC midnight."""
    snapshot = BoardSnapshot.around(board)
    # The chat record is a different endpoint and is read straight from the
    # client: the snapshot is of the board, and this is the only tool that wants
    # the record, so there is nothing for a second one to share.
    client = snapshot.client

    @tool('daily_recap', args_schema=DailyRecapArgs)
    async def daily_recap(day: str = 'yesterday', days: int = 1,
                          config: RunnableConfig = None) -> dict:
        """What one day — or a short window of days — actually held: the cards
        created, the messages sent to the assistant, what the conversations
        were about, and a summary of the user's own words. Use it when asked
        about the user's concerns, thoughts or day — never answer that from
        memory. Covers one day by default; pass days=N (up to 7) for a wider
        window ending at `day` — "recap the last 3 days" is day='today',
        days=3.
        """
        anchor = today or datetime.now(timezone.utc).date()
        end = anchor - timedelta(days=1) if day == 'yesterday' else anchor
        start = end - timedelta(days=days - 1)

        # The same UTC day-ints the chunk metadata carries — see day_int.
        def as_int(d: date) -> int:
            return d.year * 10000 + d.month * 100 + d.day

        scope = TimeScope(as_int(start), as_int(end),
                          label=f'{days}d up to {day}', kind='relative')

        cards = [c for c in await snapshot.cards(config)
                 if scope.matches({'created_day': day_int(c.get('createdAt'))},
                                  ('created_day',))]
        titles = [c.get('title', '') for c in cards]

        # One chunks_on per day in the window (≤ 7 local gets). A range `get`
        # via scope.where_clause is the upgrade path if the bound ever grows.
        window_days = [as_int(start + timedelta(days=i)) for i in range(days)]
        chunks = ([meta for d in window_days for meta in store.chunks_on(d)]
                  if store is not None else [])
        # A label only when a chunk carries one — a blank for every unlabelled
        # chunk would describe the index, not the day.
        labels = [meta['label'] if 'label' in meta else meta['summary']
                  for meta in chunks if 'label' in meta or 'summary' in meta]

        messages = [m for m in await client.list_chat(board_of(config))
                    if scope.matches({'created_day': day_int(m.get('createdAt'))},
                                     ('created_day',))]
        user = [m for m in messages if m.get('role') == 'user']
        assistant = [m for m in messages if m.get('role') == 'assistant']

        titled = ', '.join(f'"{t}"' for t in titles)
        when = day if days == 1 else f'in the {days} days up to {day}'
        text = (f'{when} you made {len(cards)} cards'
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
            summary = _content(await llm.ainvoke(prompt))

        return {'day': end.isoformat(),
                'from': start.isoformat(), 'to': end.isoformat(),
                'cards': {'count': len(cards), 'titles': titles},
                'chunks': len(chunks), 'labels': labels,
                'user_messages': len(user),
                'assistant_messages': len(assistant),
                'text': text, 'summary': summary}

    return with_sync_door(daily_recap)
