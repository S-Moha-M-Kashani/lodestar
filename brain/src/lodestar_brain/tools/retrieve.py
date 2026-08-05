"""The agent's two ways into the board's own memory.

Thin on purpose: `retrieval.py` owns the pipeline, and these wrappers decide only
what the model is told. Each rebuilds from `/api/state` before answering, so a
card created a second ago is findable — the index fingerprint is what makes that
free on an unchanged board.
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..retrieval import GRADE_THRESHOLD, CardIndex, ChatStore
from .board import BoardClient


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


def make_retrieve_tool(index: CardIndex, client: BoardClient, llm=None,
                       threshold: float = GRADE_THRESHOLD,
                       memory: ChatStore | None = None) -> BaseTool:
    @tool('find_related', args_schema=FindRelatedArgs)
    def find_related(text: str, k: int = 5) -> list[dict]:
        """Find board cards related to a text, best first. Use it to answer from
        what is already on the board and to avoid creating a duplicate."""
        cards = client.list_cards()
        index.build(cards)     # the board is small; rebuilding keeps it fresh
        by_id = {card['id']: card for card in cards}
        hits = index.search(text, k=k, llm=llm, threshold=threshold)
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
            rows += [{'chat': {'text': hit['text'],
                               'role': hit['metadata'].get('role', '')},
                      'rank': rank}
                     for rank, hit in enumerate(
                         memory.search(text, k=k, evidence=False), start=1)]
        return rows

    if memory is not None:
        # Told only when true: without Chroma there is no chat half, and a
        # description promising one would send the model looking for snippets
        # this tool can never return.
        find_related.description += (
            ' Also returns snippets from past conversations on this board.')
    return find_related


def make_recall_tool(store: ChatStore) -> BaseTool:
    @tool('recall_chat', args_schema=RecallChatArgs)
    def recall_chat(text: str, k: int = 5,
                    config: RunnableConfig = None) -> list[dict]:
        """Recall relevant snippets from OTHER conversations on this board — the
        chat you are in now is already in front of you. Use it when the user
        refers to something discussed outside this conversation."""
        # Which chat we are in arrives through the run config, never as an
        # argument: the model must not be able to name — or spoof — it, and
        # `config` is excluded from the schema the model sees because
        # `args_schema` above declares the schema explicitly.
        current = (config or {}).get('configurable', {}).get('session_id')
        hits = store.search(text, k=k, exclude_session=current or None)
        # Dated, so a recalled line can be attributed instead of quoted as
        # though it were said now. The date is already in the chunk metadata;
        # the chat's title deliberately is not — a copy of it here would go
        # stale the moment the chat is renamed.
        return [hit | {'day': (hit.get('metadata') or {}).get('created_day')}
                for hit in hits]

    return recall_chat


__all__ = ['make_recall_tool', 'make_retrieve_tool']
