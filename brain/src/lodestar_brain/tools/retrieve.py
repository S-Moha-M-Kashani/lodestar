"""The agent's two ways into the board's own memory.

Thin on purpose: `retrieval/` owns the pipeline, and these wrappers decide only
what the model is told. `find_related` rebuilds from `/api/state` before
answering, so a card created a second ago is findable — the turn's snapshot is
what makes the read free after the first tool has taken it, and the index
fingerprint is what makes the rebuild free on an unchanged board.
"""
from langchain.tools import ToolRuntime
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..board.client import BoardClient
from ..board.snapshot import BoardSnapshot, board_of
from ..retrieval import GRADE_THRESHOLD, CardIndex, ChatStore
from .dual import with_sync_door


class FindRelatedArgs(BaseModel):
    text: str
    k: int = Field(5, ge=1, le=20)


class RecallChatArgs(BaseModel):
    text: str
    k: int = Field(5, ge=1, le=20)


def _brief(card: dict) -> dict:
    """What the model sees of a card.

    Built from the board row rather than from index metadata: a store can only
    filter on scalars, so tags are space-joined in there, and handing that string
    back would silently turn two tags into one phrase."""
    return {'id': card['id'], 'title': card.get('title', ''),
            'columnId': card.get('columnId', ''), 'tags': card.get('tags') or []}


def make_retrieve_tool(index: CardIndex, board: BoardClient | BoardSnapshot,
                       llm=None, threshold: float = GRADE_THRESHOLD,
                       memory: ChatStore | None = None) -> BaseTool:
    snapshot = BoardSnapshot.around(board)

    @tool('find_related', args_schema=FindRelatedArgs)
    async def find_related(text: str, k: int = 5,
                           config: RunnableConfig = None) -> list[dict]:
        """Find board cards related to a text, best first. Use it to answer from
        what is already on the board and to avoid creating a duplicate."""
        board_id = board_of(config) or None
        # The board is small, and rebuilding keeps it fresh; within a turn the
        # snapshot means neither the fetch nor the rebuild happens twice.
        cards = await snapshot.indexed(index, config)
        by_id = {card['id']: card for card in cards}
        hits = await index.asearch(text, k=k, llm=llm, threshold=threshold)
        # `rank`, not a score: RRF fuses ranks and exposes no fused score, and a
        # number invented from a position would look like a measurement. Card
        # and chat rows each rank from 1 — two indexes, two rankings; a merged
        # ordering would compare scores that never met.
        rows = [{'card': _brief(by_id[doc.metadata['id']]), 'rank': rank}
                for rank, doc in enumerate(hits, start=1)
                if doc.metadata.get('id') in by_id]
        if memory is not None:
            # evidence=False: the lexical floor protects a human scanning the
            # recall box; in front of a model that judges relevance itself it
            # would silently drop the semantic matches that are the point of
            # having a dense index (see ChatStore.search).
            # `asearch`, not `search`: this is a coroutine, and the chat
            # half reads the whole collection and ranks it. Inline, one
            # find_related stops the process answering anything else for the
            # length of a recall (retrieval/offload.py).
            recalled = await memory.asearch(text, k=k, evidence=False,
                                            board_id=board_id)
            rows += [{'chat': {'text': hit['text'],
                               'role': hit['metadata'].get('role', '')},
                      'rank': rank}
                     for rank, hit in enumerate(recalled, start=1)]
        return rows

    if memory is not None:
        # Told only when true: without Chroma there is no chat half, and a
        # description promising one would send the model looking for snippets
        # this tool can never return.
        find_related.description += (
            ' Also returns snippets from past conversations on this board.')
    return with_sync_door(find_related)


def make_recall_tool(store: ChatStore) -> BaseTool:
    @tool('recall_chat', args_schema=RecallChatArgs)
    def recall_chat(text: str, k: int = 5,
                    runtime: ToolRuntime = None) -> list[dict]:
        """Recall relevant snippets from OTHER conversations on this board — the
        chat you are in now is already in front of you. Use it when the user
        refers to something discussed outside this conversation."""
        # Which chat we are in arrives through the run's typed context, never as
        # an argument: the model must not be able to name — or spoof — it.
        # `runtime` is a declared injection, so the framework strips it from the
        # schema the model sees rather than `args_schema` merely happening not to
        # mention it. Absent when a caller runs this tool on its own, which is
        # not an error: a recall that excluded nothing beats a 500.
        current = getattr(getattr(runtime, 'context', None), 'session_id', '')
        # The board still rides in `configurable`, shared with find_related.
        config = getattr(runtime, 'config', None) or {}
        board = config.get('configurable', {}).get('board_id')
        # The synchronous door, deliberately. This tool is synchronous, so
        # LangChain awaits it through `run_in_executor` — the work is already
        # off the event loop, and what it was missing was not the hop but the
        # lock, which `ChatStore` now takes whichever door is used. A coroutine
        # here would offload work that is already offloaded, and would break
        # every caller that invokes this tool synchronously.
        hits = store.search(text, k=k, exclude_session=current or None,
                            board_id=board or None)
        # Dated, so a recalled line can be attributed instead of quoted as
        # though it were said now. The date is already in the chunk metadata;
        # the chat's title deliberately is not — a copy of it here would go
        # stale the moment the chat is renamed.
        return [hit | {'day': (hit.get('metadata') or {}).get('created_day')}
                for hit in hits]

    return recall_chat


__all__ = ['make_recall_tool', 'make_retrieve_tool']
