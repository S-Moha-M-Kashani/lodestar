"""The board, embedded in this process and rebuilt when it changes.

`CardIndex.search` is the chosen architecture end to end — resolve the time
language, expand the query, retrieve both ways, fuse, rerank, and gate — over an
`InMemoryVectorStore` rather than a service, because the cards live in SQLite
and this index is derived from `/api/state`.
"""
import hashlib
from datetime import date

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from .chunking import card_document
from .expand import expand_queries
from .fusion import (CANDIDATES, TOP_K, RankBM25Retriever, hybrid_retriever,
                     rrf_fuse)
from .gate import GRADE_THRESHOLD, relevance_gate
from .rerank import lexical_rerank
from .timescope import resolve_time_scope


def _fingerprint(documents: list[Document]) -> str:
    """Identify a board by what would be indexed from it. Not the card count and
    not `updatedAt`: the point is that the fingerprint changes exactly when the
    embeddings would, so an unchanged board is never paid for twice."""
    digest = hashlib.blake2b(digest_size=16)
    for doc in documents:
        digest.update(doc.page_content.encode())
        digest.update(repr(sorted(doc.metadata.items())).encode())
        digest.update(b'\x00')
    return digest.hexdigest()


class CardIndex:
    """The board, embedded in this process and rebuilt when it changes.

    Deliberately not a Chroma collection. SQLite is the record for cards and
    this index is derived from `/api/state`, so persisting it would add a service
    dependency for a throwaway artifact plus a stale-row deletion problem. The
    fingerprint is what makes a rebuild-per-tool-call affordable: without it the
    board would be re-embedded on every question, which is free with hashing and
    very much not free with a real encoder."""

    def __init__(self, embeddings: Embeddings):
        self.embeddings = embeddings
        self.documents: list[Document] = []
        self.store: InMemoryVectorStore | None = None
        self.bm25 = RankBM25Retriever.from_documents([], k=CANDIDATES)
        self.fingerprint = ''

    def build(self, cards: list[dict]) -> bool:
        """Index the board unless this exact board is already indexed. Returns
        whether anything was rebuilt."""
        documents = [card_document(card) for card in cards]
        fingerprint = _fingerprint(documents)
        if self.store is not None and fingerprint == self.fingerprint:
            return False
        store = InMemoryVectorStore(self.embeddings)
        if documents:
            store.add_documents(documents)
        self.documents, self.store, self.fingerprint = documents, store, fingerprint
        # Tokenised once here rather than per search: `search` copies this
        # retriever to attach a time scope, which reuses the same index.
        self.bm25 = RankBM25Retriever.from_documents(documents, k=CANDIDATES)
        return True

    def search(self, query: str, k: int = TOP_K, today: date | None = None,
               time_filter: bool = True) -> list[Document]:
        """The chosen architecture up to the gate: resolve the time language,
        expand the query, retrieve both ways, fuse and rerank.

        Everything here is local and CPU-bound, so it stays synchronous. The gate
        is the one stage that calls a model, and it is `asearch` — a caller that
        wants it has to be somewhere it can wait.
        """
        if not self.documents or self.store is None:
            return []
        scope = resolve_time_scope(query, today) if time_filter else None
        search_kwargs: dict = {'k': CANDIDATES}
        if scope is not None:
            search_kwargs['filter'] = lambda doc: scope.matches(doc.metadata)
        hybrid = hybrid_retriever(
            self.store.as_retriever(search_kwargs=search_kwargs),
            self.bm25.model_copy(update={'scope': scope}))
        queries = expand_queries(query)
        fused = rrf_fuse([hybrid.invoke(variant) for variant in queries])
        # The reranker reads the expanded query: a card matched through a
        # synonym or another script must not be scored as covering nothing.
        return lexical_rerank(' '.join(queries), fused, self.bm25.idf, k=k)

    async def asearch(self, query: str, k: int = TOP_K, today: date | None = None,
                      llm=None, threshold: float = GRADE_THRESHOLD,
                      time_filter: bool = True) -> list[Document]:
        """`search`, and — when a model is given — the gate after it.

        The whole pipeline in one call for the one caller that has a model to
        spend: `find_related`, answering inside a turn. The search box
        (`/rag/recall`) passes no model deliberately, so it takes the sync door
        and waits for nothing.
        """
        ranked = self.search(query, k=k, today=today, time_filter=time_filter)
        if llm is None:
            return ranked
        return await relevance_gate(llm, query, ranked, threshold)


"""Alternatives considered

**"Why is the board's index in memory when chat memory is in Chroma?"**

*Short answer.* Because one is derived and the other is the record. Cards live in
SQLite and this index is rebuilt from `/api/state`; a transcript exists nowhere
but the store (`chat.py`).

*Why one store for both fails.* A Chroma collection of cards has to be kept in
step with a board that changes constantly, which means deleting stale vectors on
every save — and a vector for a card deleted from SQLite does not raise, it just
keeps being retrieved, so the agent recommends a card that no longer exists. It
would also make `find_related` depend on a running service to answer a question
about data the brain already has in hand.

*Why not the framework.* This one is barely ours: `InMemoryVectorStore` is
LangChain's, `langchain_chroma.Chroma` backs the chat store, and what is written
here is the fingerprint plus the assembly. The fingerprint is the part with no
framework equivalent — LangChain has no notion of "this corpus is the corpus I
already indexed".

*The libraries that would do it.* Chroma for both, with an explicit delete pass —
one code path, at the cost above. FAISS via `langchain-community` — fast and
persistable, with the same stale-row duty. Qdrant or Weaviate — real typed
metadata and server-side filtering, which would also fix the tag-joining
compromise in `chunking.py`; another service to run. `sqlite-vec` — genuinely the
most attractive on paper, since the board is already SQLite and the index would
travel with the data; it is ruled out by an invariant rather than by
performance, because the brain must never touch SQLite directly (all its writes
go through the Node API, which is what keeps the durability promise true for
agent edits).

*Why not adopted, and what would change it.* Board size, and it is measurable
rather than arguable: brute-force cosine over a few hundred short cards is
microseconds, and the fingerprint means an unchanged board costs nothing at all.
The crossover is when a fingerprint *miss* — re-embedding the whole board with a
real encoder — costs more than an incremental upsert would. On the order of tens
of thousands of cards, and the way to know is to time `build` on a real board,
not to reason about it.
"""
