"""Leiden graph RAG over board cards: embed → kNN similarity graph → Leiden
communities. query() returns top-k cards annotated with their community so the
agent can surface 'these questions belong together'."""
import igraph as ig
import leidenalg
import numpy as np
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from ..tools.board import BoardClient
from .embedder import Embedder


class FindRelatedArgs(BaseModel):
    text: str
    k: int = Field(5, ge=1, le=20)


def card_text(card: dict) -> str:
    return ' '.join([card.get('title', ''), card.get('notes', ''),
                     ' '.join(card.get('tags') or [])])


def _brief(card: dict) -> dict:
    return {'id': card['id'], 'title': card['title'],
            'columnId': card['columnId'], 'tags': card.get('tags') or []}


class LeidenIndex:
    def __init__(self, embedder: Embedder, k_neighbors: int = 3, min_sim: float = 0.15):
        self.embedder = embedder
        self.k_neighbors = k_neighbors
        self.min_sim = min_sim
        self.cards: list[dict] = []
        self.vectors: np.ndarray | None = None
        self.membership: list[int] = []

    def build(self, cards: list[dict]) -> None:
        self.cards = list(cards)
        if not self.cards:
            self.vectors = None
            self.membership = []
            return
        self.vectors = self.embedder.embed([card_text(c) for c in self.cards])
        sims = self.vectors @ self.vectors.T
        n = len(self.cards)
        edges: list[tuple[int, int]] = []
        weights: list[float] = []
        seen: set[tuple[int, int]] = set()
        for i in range(n):
            order = np.argsort(sims[i])[::-1]
            picked = 0
            for j in order:
                j = int(j)
                if j == i:
                    continue
                if sims[i][j] < self.min_sim or picked >= self.k_neighbors:
                    break
                key = (min(i, j), max(i, j))
                if key not in seen:
                    seen.add(key)
                    edges.append(key)
                    weights.append(float(sims[i][j]))
                picked += 1
        graph = ig.Graph(n=n, edges=edges)
        if edges:
            partition = leidenalg.find_partition(
                graph, leidenalg.ModularityVertexPartition,
                weights=weights, seed=0)
            self.membership = list(partition.membership)
        else:
            self.membership = list(range(n))

    def query(self, text: str, k: int = 5) -> list[dict]:
        if self.vectors is None:
            return []
        query_vec = self.embedder.embed([text])[0]
        scores = self.vectors @ query_vec
        top = np.argsort(scores)[::-1][:k]
        return [{'card': _brief(self.cards[int(i)]), 'score': float(scores[int(i)]),
                 'community': int(self.membership[int(i)])} for i in top]

    def communities(self) -> list[dict]:
        groups: dict[int, list[int]] = {}
        for i, community in enumerate(self.membership):
            groups.setdefault(community, []).append(i)
        return [{'id': community, 'size': len(members),
                 'cards': [_brief(self.cards[i]) for i in members]}
                for community, members in sorted(groups.items())]


def make_retrieve_tool(index: LeidenIndex, client: BoardClient) -> BaseTool:
    @tool('find_related', args_schema=FindRelatedArgs)
    def find_related(text: str, k: int = 5) -> list[dict]:
        """Find board questions related to a text, with their Leiden community id.
        Same community = same theme; use it to point out duplicates/connections."""
        index.build(client.list_cards())  # board is small — rebuild keeps it fresh
        return index.query(text, k=k)

    return find_related
