"""The agent's three ways into the board's own memory.

Thin on purpose: `retrieval.py` owns the pipeline, and these wrappers decide only
what the model is told. Each rebuilds from `/api/state` before answering, so a
card created a second ago is findable — the index fingerprint is what makes that
free on an unchanged board.

`find_related` and `group_cards` are separate tools because they make different
claims. "This card answers your query" is a ranking; "these cards belong
together" is a grouping, and it needs no query at all. The community id used to
ride along on every ranked hit, which read as though relevance and kinship were
the same measurement.
"""
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..retrieval import GRADE_THRESHOLD, CardIndex, ChatStore
from .board import BoardClient


class FindRelatedArgs(BaseModel):
    text: str
    k: int = Field(5, ge=1, le=20)


class GroupCardsArgs(BaseModel):
    min_size: int = Field(2, ge=1, le=50)


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


def make_group_tool(index: CardIndex, client: BoardClient) -> BaseTool:
    @tool('group_cards', args_schema=GroupCardsArgs)
    def group_cards(min_size: int = 2) -> list[dict]:
        """Group the board's cards by theme, largest group first. Same group =
        same subject, so use it to point out connections and likely duplicates.
        Takes no query: it describes the whole board."""
        cards = client.list_cards()
        index.build(cards)
        by_id = {card['id']: card for card in cards}
        groups: dict[int, list[dict]] = {}
        for doc, label in zip(index.documents, index.communities()):
            card = by_id.get(doc.metadata.get('id'))
            if card is not None:
                groups.setdefault(int(label), []).append(_brief(card))
        return [{'id': label, 'size': len(members), 'cards': members}
                for label, members in sorted(groups.items(),
                                             key=lambda kv: -len(kv[1]))
                if len(members) >= min_size]

    return group_cards


def make_recall_tool(store: ChatStore) -> BaseTool:
    @tool('recall_chat', args_schema=RecallChatArgs)
    def recall_chat(text: str, k: int = 5) -> list[dict]:
        """Recall relevant snippets from past assistant conversations on this
        board. Use it to answer questions about things discussed before."""
        return store.search(text, k=k)

    return recall_chat


__all__ = ['make_group_tool', 'make_recall_tool', 'make_retrieve_tool']
