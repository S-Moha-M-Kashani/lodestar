"""The board, embedded in this process and kept up to date card by card.

`CardIndex.search` is the chosen architecture end to end — resolve the time
language, expand the query, retrieve both ways, fuse, rerank, and gate — over an
`InMemoryVectorStore` rather than a service, because the cards live in SQLite
and this index is derived from `/api/state`.

**Maintenance is per card, not per board.** Every indexed card carries its own
fingerprint, so editing one card of sixty asks the embedder for one card's worth
of vectors; the other fifty-nine are reused. A change to the embedder, the
document schema or the index format changes the *namespace* instead, and that
throws everything away and rebuilds — the escape hatch the whole design leans
on, reachable by name as `rebuild`.
"""
import asyncio
import hashlib
from datetime import date

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from .chunking import (CARD_META_KEYS, CHUNK_OVERLAP, CHUNK_SIZE,
                       card_document)
from .expand import expand_queries
from .fusion import (CANDIDATES, TOP_K, RankBM25Retriever, hybrid_retriever,
                     rrf_fuse)
from .gate import GRADE_THRESHOLD, relevance_gate
from .rerank import Reranker, lexical_rerank
from .timescope import resolve_time_scope

# Bumped by hand when the *shape* of an indexed record changes — a new field on
# the document, a different text assembly, a different store. It is in the
# namespace so such a change invalidates a running process's vectors instead of
# mixing two shapes in one store. Nothing migrates: this index is derived from
# `/api/state` and lives in memory, so the version is a correctness switch
# rather than a schema history.
INDEX_FORMAT = 1


def _embedder_identity(embeddings: Embeddings) -> str:
    """What produced the vectors, as a string that changes when the vector space
    does: the class, the checkpoint it loads and the prefixes it prepends.

    Read out of the instance `__dict__` and never with `getattr`, deliberately.
    `SentenceTransformerEmbeddings.dim` is a property that *loads the model*, so
    asking an embedder for its dimension here would download 2.2 GB on the first
    board build — the one thing `_PrefixedEmbeddings` defers on purpose. Only
    values already set in `__init__` are read, which is every value that decides
    the vector space.
    """
    fields = vars(embeddings)
    parts = [f'{type(embeddings).__module__}.{type(embeddings).__name__}']
    for attr in ('model_name', 'query_prefix', 'passage_prefix', 'n'):
        value = fields.get(attr)
        if isinstance(value, (str, int, float, bool)):
            parts.append(f'{attr}={value}')
    return '|'.join(parts)


def _namespace(embeddings: Embeddings, corpus: str = '') -> str:
    """Everything that invalidates every vector at once, in one digest.

    The embedder's identity, the chunker's configuration, the metadata schema,
    the index format and — for a caller that keeps one index per board — the
    corpus it is over. A card fingerprint answers "has this card changed?"; this
    answers "is anything I already embedded still comparable?", and the two
    questions have to be kept apart or a model change reads as sixty edits and
    reuses vectors from the old space for the cards it thinks are unchanged.
    """
    digest = hashlib.blake2b(digest_size=8)
    for part in (str(INDEX_FORMAT), _embedder_identity(embeddings),
                 f'chunk={CHUNK_SIZE}/{CHUNK_OVERLAP}',
                 'meta=' + ','.join(CARD_META_KEYS), f'corpus={corpus}'):
        digest.update(part.encode())
        digest.update(b'\x00')
    return digest.hexdigest()


def _card_fingerprint(document: Document) -> str:
    """Identify one card by what would be indexed from it. Not `updatedAt`: the
    point is that the fingerprint changes exactly when the record would, so an
    untouched card is never embedded twice and a touched one is never missed."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(document.page_content.encode())
    digest.update(repr(sorted(document.metadata.items())).encode())
    return digest.hexdigest()


def _manifest(documents: list[Document]) -> dict[str, str]:
    """The board as indexed: one fingerprint per card, in board order.

    Keyed by the card id, which is what the store keys a record by. A document
    with no id falls back to its position — `InMemoryVectorStore` mints a random
    uuid for such a row, so it can never be recognised across builds anyway, and
    a positional key at least keeps "same place, same text" from re-embedding.
    """
    return {(document.id or f'@{position}'): _card_fingerprint(document)
            for position, document in enumerate(documents)}


def _index_fingerprint(namespace: str, manifest: dict[str, str]) -> str:
    """The whole index in one digest — the namespace plus every card's
    fingerprint, in order. Observable state, not a decision: `build` compares
    manifests, because a digest can only say *whether* something changed."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(namespace.encode())
    for key, fingerprint in manifest.items():
        digest.update(key.encode())
        digest.update(fingerprint.encode())
        digest.update(b'\x00')
    return digest.hexdigest()


class _VectorCache(Embeddings):
    """The embedder with the vectors it has already produced kept beside it.

    This is what makes a single-card edit cost a single embedding while the
    store is still assembled in board order from a fresh `InMemoryVectorStore`:
    the store asks for sixty vectors, this hands back fifty-nine it already has
    and calls the real embedder once, for one text. Rebuilding the store rather
    than mutating it is what keeps a concurrent `search` coherent (see the note
    in `search`) and what keeps insertion order — and therefore tie-breaking —
    identical to a full rebuild's.

    Keyed by text, because text is what an embedder sees. A card whose *metadata*
    moved (inbox → done) is a changed record and an unchanged vector, and this
    is where that costs nothing. Two cards with the same text share one call,
    exactly as `embed_documents` over a list with a repeat always did.

    `embed_query` is never cached: queries are unbounded user input, so a cache
    of them is a leak with no hit rate to show for it.
    """

    def __init__(self, embeddings: Embeddings):
        self.embeddings = embeddings
        self.vectors: dict[str, list[float]] = {}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # dict.fromkeys de-duplicates and keeps order, so the real embedder sees
        # each new text once and in the order it was asked for.
        fresh = [text for text in dict.fromkeys(texts)
                 if text not in self.vectors]
        if fresh:
            for text, vector in zip(fresh, self.embeddings.embed_documents(fresh)):
                self.vectors[text] = vector
        return [self.vectors[text] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embeddings.embed_query(text)

    def clear(self) -> None:
        self.vectors.clear()

    def keep(self, texts: set[str]) -> None:
        """Drop the vectors of text no card holds any more.

        Pruned to exactly the live board, so the cache costs one extra copy of
        the vectors and nothing that grows with the edit history. The cost is
        that re-adding a card that was deleted pays one embedding again; an LRU
        of a few hundred would buy that back, and there is no measurement saying
        it is worth the machinery.
        """
        for text in [text for text in self.vectors if text not in texts]:
            del self.vectors[text]


class CardIndex:
    """The board, embedded in this process and maintained card by card.

    Deliberately not a Chroma collection. SQLite is the record for cards and
    this index is derived from `/api/state`, so persisting it would add a service
    dependency for a throwaway artifact plus a stale-row deletion problem. The
    manifest is what makes a rebuild-per-tool-call affordable: an unchanged board
    costs one digest per card and no embedding at all, and a changed one costs
    the cards that changed.

    Rebuild state is public, because "the index is current" is a claim someone
    can act on: `current` is only true after a build finished, `generation`
    counts the builds that completed, and `namespace`/`records` say what was
    indexed and by what. A build that raises part-way leaves `current` false —
    never a stale index reporting itself up to date.
    """

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
        self.namespace = ''
        self.records: dict[str, str] = {}
        self.generation = 0
        self._cache = _VectorCache(embeddings)
        self._current = False

    @property
    def current(self) -> bool:
        """Whether a build has finished and left something searchable. False
        before the first build and false after one that raised, which is the
        whole reason it is a separate flag from `store is not None`."""
        return self._current and self.store is not None

    def build(self, cards: list[dict], corpus: str = '',
              force: bool = False) -> bool:
        """Bring the index up to date with this board. Returns whether anything
        was rebuilt.

        Three paths, in order of what they cost:

        1. **Nothing changed** — the manifest matches card for card, in order.
           No embedding, no store, no BM25. This is the per-tool-call case.
        2. **Some cards changed** — the store is reassembled in board order and
           the embedder is asked only for the text it has not seen. `records`
           says which cards those are.
        3. **The namespace changed** (embedder, schema, index format, corpus) or
           `force` — every vector is discarded first, because a vector from
           another embedder is not wrong-looking, it is silently incomparable.

        Order is part of the identity: a reordered board takes path 2 with zero
        embeddings, so the store's insertion order — and therefore how it breaks
        score ties — stays exactly what a full rebuild would produce.
        """
        documents = [card_document(card) for card in cards]
        namespace = _namespace(self.embeddings, corpus)
        wanted = _manifest(documents)
        if force or namespace != self.namespace:
            # Dropped *before* the work, not after it. A rebuild that raises
            # half way must not leave the old vector space searchable under the
            # new embedder: the query would be embedded by one model and scored
            # against another's vectors, which raises nothing and ranks noise.
            self._current = False
            self._cache.clear()
            self.documents, self.store, self.records = [], None, {}
            self.bm25 = RankBM25Retriever.from_documents([], k=CANDIDATES)
            self.namespace, self.fingerprint = '', ''
        elif (self.current
                and list(wanted.items()) == list(self.records.items())):
            return False
        self._current = False
        store = InMemoryVectorStore(self._cache)
        if documents:
            store.add_documents(documents)
        self.documents, self.store = documents, store
        # Tokenised once here rather than per search: `search` copies this
        # retriever to attach a time scope, which reuses the same index. Whole
        # corpus every time, unavoidably — `idf` is a statistic *of* the corpus,
        # so one card's edit moves every term weight and there is no incremental
        # version of it to want.
        self.bm25 = RankBM25Retriever.from_documents(documents, k=CANDIDATES)
        self._cache.keep({document.page_content for document in documents})
        self.records, self.namespace = wanted, namespace
        self.fingerprint = _index_fingerprint(namespace, wanted)
        self.generation += 1
        self._current = True
        return True

    def rebuild(self, cards: list[dict], corpus: str = '') -> bool:
        """Discard everything and index the board from scratch.

        The escape hatch, by name. Incremental maintenance is only ever as good
        as its invalidation, so the one operation that trusts none of it stays a
        single call — for an operator who suspects the index, and for a test that
        needs the full-rebuild path without changing the embedder to get it.
        """
        return self.build(cards, corpus, force=True)

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
        # affects only the next one. This is also why an incremental build
        # assembles a *new* store instead of deleting and adding inside the live
        # one: a mutated store has no generation to bind to, and a search
        # iterating it while a card is replaced sees a corpus that never
        # existed. No lock: serialising a search against a rebuild that is
        # itself doing embedder work costs more than the hazard.
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

**"Why is the board's index in memory, and why is its invalidation yours?"**

*Short answer.* Because one is derived and the other is the record. Cards live in
SQLite and this index is rebuilt from `/api/state`; a transcript exists nowhere
but the store (`chat.py`). And because no vector store here has a notion of "this
card is the card I already embedded" — the manifest and the namespace are that
notion, and they are the only part of this module that is not the framework's.

*Why the obvious option fails.* The obvious option was here until 2026-09-02: one
digest over the whole corpus, rebuild everything on a miss. It is four lines and
it makes the common case the expensive one — editing one card of sixty asked the
embedder for sixty vectors (measured: 60, against 1 now), and a board of a few
thousand cards would spend seconds of encoder time to record that a title lost a
typo. The equally obvious fix — deleting and adding inside the live
`InMemoryVectorStore` — fails differently: `search` runs in a worker thread, so a
store mutated in place has no generation for a search to bind to, and insertion
order stops matching board order, which silently changes how score ties break.

*Why not the framework.* Most of this module *is* the framework:
`InMemoryVectorStore`, `Document`, `Embeddings`, the retrievers. What LangChain
does not offer is incremental maintenance of a store from a source of truth —
`add_documents` embeds everything it is handed, `delete` takes ids and no
fingerprints, and there is no upsert-if-changed anywhere in `langchain_core`. The
one framework answer that exists is `langchain.indexes.SQLRecordManager` +
`index()`, which is exactly this idea (a hash per document, a namespace, add/skip
/delete) and is discussed below. `_VectorCache` is a plain `Embeddings`
implementation for the same reason the embedder backends are: the seam is the
framework's, the twenty lines behind it are ours.

*The libraries that would do it.* `langchain.indexes.index()` with a
`SQLRecordManager` — the closest fit by a distance, and it brings the `langchain`
package plus a SQLAlchemy database whose only job is to remember hashes for a
corpus that is re-read from `/api/state` on every turn anyway; its
`cleanup='full'` scan is also the stale-row problem this module avoids by
throwing the store away. Chroma or FAISS with an explicit delete pass — persistent
and incremental, at the cost of a service (or a file) to keep in step with a board
that changes constantly, and a vector for a deleted card does not raise, it just
keeps being retrieved. Qdrant or Weaviate — real typed metadata and server-side
filtering, which would also fix the tag-joining compromise in `chunking.py`;
another service to run. `sqlite-vec` — genuinely the most attractive on paper,
since the board is already SQLite and the index would travel with the data; ruled
out by an invariant rather than by performance, because the brain must never
touch SQLite directly (all its writes go through the Node API, which is what
keeps the durability promise true for agent edits).

*Why they were not adopted, and what would change it.* Decisively: a record
manager is a database for state that is already free. The manifest is a dict of
sixty digests, rebuilt from the board the index is derived from, and it dies with
the process exactly like the vectors it describes — so there is nothing to
migrate, nothing to reconcile, and no way for the record of what is indexed to
outlive the index. What is accepted in exchange is named rather than hidden: the
cache holds a second copy of the live board's vectors (~4 MB per thousand cards
at 1024 dimensions), the BM25 index is still rebuilt whole on any change because
`idf` is a statistic of the corpus, and a re-added card pays one embedding
because the cache is pruned to the live board. What would change the decision is
persistence: the day this index has to survive a restart — a board large enough
that a cold build is a visible wait, which on measured timings is tens of
thousands of cards — the namespace stops being an in-memory string and becomes a
row somebody has to reconcile, and that is `SQLRecordManager`'s job rather than
this module's.
"""
