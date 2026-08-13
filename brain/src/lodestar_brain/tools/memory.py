"""The one tool that writes to the agent's own long-term store.

A tool rather than a hook, so the write shows up in the turn's `steps` and the
user can see the agent deciding to remember something. The reading half is
`middleware/memory.py`, which also owns the namespace — one address, named once.

What this can reach is deliberately nothing else: not a card, not the chat
record, not SQLite. It is the agent's scratch pad about a board, and the
durability promise is untouched by it — losing this file costs the agent a note,
never a card and never a recorded turn.
"""
from hashlib import blake2b

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..middleware.memory import FACT_CHARS, facts_namespace


class RememberFactArgs(BaseModel):
    fact: str = Field(description=(
        'one durable fact, in a single sentence, worth having in a later '
        'conversation'))


def make_memory_tool() -> BaseTool:
    @tool('remember_fact', args_schema=RememberFactArgs)
    def remember_fact(fact: str, runtime: ToolRuntime = None) -> dict:
        """Save one durable fact about the user or this board to your own notes,
        so a later conversation can start from it.

        For things that stay true — how they like to work, a constraint they keep
        naming, a decision already taken. Not for what is on the board (cards are
        the record and find_related searches them), not for what was said in this
        chat, and never as a substitute for asking. Say that you noted it.
        """
        store = getattr(runtime, 'store', None)
        if store is None:
            # Honest rather than silent: the store is attached by the running
            # service, so an eval or a script has none, and a model told nothing
            # would report a memory that was never written.
            return {'error': 'long-term memory is not available here'}
        text = ' '.join(str(fact).split())[:FACT_CHARS]
        if not text:
            return {'error': 'a fact needs some text'}
        config = getattr(runtime, 'config', None) or {}
        board = config.get('configurable', {}).get('board_id') or ''
        # Keyed by the fact's own content, so noting the same thing twice
        # updates one entry instead of filling the pad with copies.
        key = blake2b(text.encode('utf-8'), digest_size=8).hexdigest()
        store.put(facts_namespace(board), key, {'fact': text})
        return {'remembered': text}

    return remember_fact


__all__ = ['RememberFactArgs', 'make_memory_tool']
