"""The board, embedded in this process and rebuilt when it changes.

`CardIndex.search` is the chosen architecture end to end — resolve the time
language, expand the query, retrieve both ways, fuse, rerank, and gate — over an
`InMemoryVectorStore` rather than a service, because the cards live in SQLite
and this index is derived from `/api/state`.
"""
import asyncio
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
from .rerank import Reranker, lexical_rerank
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

    def __init__(self, embeddings: Embeddings,
                 rerank: Reranker = lexical_rerank):
        self.embeddings = embeddings
        # The reranker arrives already chosen (`make_reranker`, from
        # BRAIN_RERANKER in create_app), so a misconfigured one stops the boot
        # rather than being discovered on the first question. It defaults to the
        # shipped lexical one, which is what keeps every eval, tool test and
        # script that builds a CardIndex by hand on the measured pipeline — and
        # what makes varying only the reranker a one-argument experiment.
        self.rerank = rerank
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

        Everything here is local and CPU-bound with the default reranker, so it
        stays synchronous. The gate is the one stage that always calls a model,
        and it is `asearch` — a caller that wants it has to be somewhere it can
        wait.

        `BRAIN_RERANKER=openrouter` is the exception, and it is worth naming
        rather than discovering: the rerank is a blocking request inside this
        synchronous call. `asearch` therefore runs the whole of this method in a
        worker thread, so a `find_related` call under the hosted backend is
        *slow* — up to RERANK_BUDGET, bounded and fail-open — rather than
        stalling every other request in the process. Callers that reach this
        method directly from a coroutine still block the loop; `/rag/recall` is
        the one that does, and it is one more reason the hosted backend is not
        the default.
        """
        # Read the index once. `search` now runs in a worker thread while
        # `build` rebinds these three attributes from the event loop, and it
        # does so in more than one statement — so a search that re-read them as
        # it went could pair the new documents with the old `bm25.idf`. That is
        # an incoherent ranking, and it raises nothing. Bound here, a search
        # runs against one generation of the index and a concurrent rebuild
        # affects only the next one. No lock: serialising a search against a
        # rebuild that is itself doing embedder work costs more than the hazard.
        documents, store, bm25 = self.documents, self.store, self.bm25
        if not documents or store is None:
            return []
        scope = resolve_time_scope(query, today) if time_filter else None
        search_kwargs: dict = {'k': CANDIDATES}
        if scope is not None:
            search_kwargs['filter'] = lambda doc: scope.matches(doc.metadata)
        hybrid = hybrid_retriever(
            store.as_retriever(search_kwargs=search_kwargs),
            bm25.model_copy(update={'scope': scope}))
        queries = expand_queries(query)
        fused = rrf_fuse([hybrid.invoke(variant) for variant in queries])
        # The reranker reads the expanded query: a card matched through a
        # synonym or another script must not be scored as covering nothing.
        return self.rerank(' '.join(queries), fused, bm25.idf, k=k)

    async def asearch(self, query: str, k: int = TOP_K, today: date | None = None,
                      llm=None, threshold: float = GRADE_THRESHOLD,
                      time_filter: bool = True) -> list[Document]:
        """`search`, and — when a model is given — the gate after it.

        The whole pipeline in one call for the one caller that has a model to
        spend: `find_related`, answering inside a turn, and the only caller of
        this method. The search box (`/rag/recall`) passes no model
        deliberately, so it takes the sync door and never waits on one — which
        is not the same as waiting for nothing, since it calls `search` inline
        from a coroutine and so keeps the blocking-reranker problem this method
        no longer has.

        The sync half runs in a worker thread rather than inline. With
        BRAIN_RERANKER=openrouter the rerank stage is a blocking HTTP call, and
        one turn's re-ranking must not stop the process answering anything else
        for up to RERANK_BUDGET. `search` itself stays synchronous on purpose —
        the evals, `/rag/recall` and a dozen unit tests call it, and a
        coroutine-only version would break the tools quietly, as a fenced
        `{'error': …}` the model reads as a broken board.

        What this does *not* establish: whether
        `SentenceTransformerEmbeddings.embed_query` is safe under concurrent
        calls from several worker threads. `search` embeds the query, so two
        overlapping turns now reach the encoder from two threads. It is
        unmeasured here, not shown to be fine. The gate stays on the loop —
        `relevance_gate` is already awaited I/O and gains nothing from a thread.
        """
        ranked = await asyncio.to_thread(self.search, query, k, today,
                                         time_filter)
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
