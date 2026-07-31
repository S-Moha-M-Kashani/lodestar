"""The agent's two ways into the board's own memory.

Thin on purpose: `retrieval.py` owns the pipeline, and these wrappers decide only
what the model is told. Each rebuilds from `/api/state` before answering, so a
card created a second ago is findable — the index fingerprint is what makes that
free on an unchanged board.
"""
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
                       threshold: float = GRADE_THRESHOLD) -> BaseTool:
    @tool('find_related', args_schema=FindRelatedArgs)
    def find_related(text: str, k: int = 5) -> list[dict]:
        """Find board cards related to a text, best first. Use it to answer from
        what is already on the board and to avoid creating a duplicate."""
        cards = client.list_cards()
        index.build(cards)     # the board is small; rebuilding keeps it fresh
        by_id = {card['id']: card for card in cards}
        hits = index.search(text, k=k, llm=llm, threshold=threshold)
        # `rank`, not a score: RRF fuses ranks and exposes no fused score, and a
        # number invented from a position would look like a measurement.
        return [{'card': _brief(by_id[doc.metadata['id']]), 'rank': rank}
                for rank, doc in enumerate(hits, start=1)
                if doc.metadata.get('id') in by_id]

    return find_related


def make_recall_tool(store: ChatStore) -> BaseTool:
    @tool('recall_chat', args_schema=RecallChatArgs)
    def recall_chat(text: str, k: int = 5) -> list[dict]:
        """Recall relevant snippets from past assistant conversations on this
        board. Use it to answer questions about things discussed before."""
        return store.search(text, k=k)

    return recall_chat


__all__ = ['make_recall_tool', 'make_retrieve_tool']
