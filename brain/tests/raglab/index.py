"""Building and holding one indexed configuration.

A LabIndex is the pair (Chroma collection, in-memory chunk table). Chroma owns
the vectors; the chunk table owns the text, which BM25, parent expansion and
every lexical metric need. Both are keyed by deterministic chunk ids, so a
rebuild upserts over the previous one instead of duplicating it, and a lab
restart can reload an index from Chroma without re-embedding a thing.

The collection name is IndexConfig.fingerprint(), so switching a chunker in the
panel builds a *new* collection and leaves the old one intact — sweeping back and
forth between two strategies costs one build each, not one per switch.
"""
import time
from dataclasses import dataclass, field

import numpy as np

from lodestar_brain.rag.chat_memory import ChromaChatMemory

from . import embedding, summarize
from .chunking import Chunk, chunk_session
from .config import IndexConfig, LabSettings

BATCH = 200


@dataclass
class IndexStats:
    collection: str = ''
    chunks: int = 0
    by_layer: dict = field(default_factory=dict)
    avg_chars: float = 0.0
    p95_chars: int = 0
    embed_dim: int = 0
    build_seconds: float = 0.0
    reused: bool = False
    summarizer_failures: int = 0
    notes: list = field(default_factory=list)


class LabIndex:
    def __init__(self, cfg: IndexConfig, embedder, store: ChromaChatMemory,
                 chunks: list[Chunk], stats: IndexStats):
        self.cfg = cfg
        self.embedder = embedder
        self.store = store
        self.chunks = chunks
        self.stats = stats
        self.by_id = {c.id: c for c in chunks}
        self.by_session: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            if chunk.session_id:
                self.by_session.setdefault(chunk.session_id, []).append(chunk)
        self._bm25 = None

    # --- building ---------------------------------------------------------

    @classmethod
    def build(cls, cfg: IndexConfig, diary: dict, settings: LabSettings,
              progress=None, force: bool = False) -> 'LabIndex':
        started = time.time()
        cfg = cfg.normalized()
        stats = IndexStats(collection=cfg.collection())
        note = stats.notes.append
        embedder = embedding.make_embedder(cfg.embedder, settings, cfg.embed_model)
        stats.embed_dim = getattr(embedder, 'dim', 0)
        store = ChromaChatMemory(settings.chroma_url, embedder,
                                 collection=cfg.collection(),
                                 database=settings.chroma_database)

        sessions = diary['sessions']
        if progress:
            progress('summarising', 0.05)
        summarizer = summarize.ExtractiveSummarizer(summarize.build_idf(sessions))
        if cfg.summarizer == 'llm':
            summarizer = summarize.LLMSummarizer(
                _lab_llm(settings),
                cfg.summarizer_model or settings.llm_model, summarizer)
        cache = summarize.SummaryCache()
        summaries = summarize.session_summaries(
            sessions, summarizer, cache,
            progress=(lambda i, n: progress('summarising', 0.05 + 0.35 * i / n))
            if progress else None)
        stats.summarizer_failures = getattr(summarizer, 'failures', 0)
        if stats.summarizer_failures:
            note(f'{stats.summarizer_failures} sessions fell back to extractive '
                 'summaries (LLM errors)')

        if progress:
            progress('chunking', 0.45)
        chunks: list[Chunk] = []
        if 'chunk' in cfg.layers:
            for session in sessions:
                chunks.extend(chunk_session(session, cfg, embedder,
                                            summaries[session['session_id']]))
        if 'session' in cfg.layers:
            chunks.extend(summarize.session_layer(sessions, summaries))
        if 'month' in cfg.layers:
            chunks.extend(summarize.month_layer(sessions, summaries))
        if 'thread' in cfg.layers:
            chunks.extend(summarize.thread_layer(sessions, summaries,
                                                 diary.get('threads', {})))
        if 'commitment' in cfg.layers:
            chunks.extend(summarize.commitment_layer(sessions))

        lengths = np.array([len(c.text) for c in chunks]) if chunks else np.array([0])
        stats.chunks = len(chunks)
        stats.avg_chars = round(float(lengths.mean()), 1)
        stats.p95_chars = int(np.percentile(lengths, 95))
        for chunk in chunks:
            stats.by_layer[chunk.layer] = stats.by_layer.get(chunk.layer, 0) + 1

        existing = store.collection.count()
        if existing == len(chunks) and not force:
            stats.reused = True
            note(f'reused an existing collection with {existing} records')
        else:
            if existing and existing != len(chunks):
                # Stale rows from an interrupted build would answer queries with
                # chunks the current config never produced.
                store.client.delete_collection(cfg.collection())
                store.collection = store.client.get_or_create_collection(
                    cfg.collection(), metadata={'hnsw:space': 'cosine'},
                    embedding_function=None)
                note(f'dropped {existing} stale records before rebuilding')
            for start in range(0, len(chunks), BATCH):
                batch = chunks[start:start + BATCH]
                vectors = embedder.embed([c.text for c in batch])
                if not np.any(vectors):
                    note('WARNING: this embedder produced all-zero vectors for '
                         'part of the corpus — it cannot represent this text')
                store.collection.upsert(
                    ids=[c.id for c in batch],
                    documents=[c.text for c in batch],
                    embeddings=[v.tolist() for v in vectors],
                    metadatas=[c.metadata() for c in batch])
                if progress:
                    done = (start + len(batch)) / max(1, len(chunks))
                    progress('embedding', 0.5 + 0.5 * done)

        stats.build_seconds = round(time.time() - started, 2)
        return cls(cfg, embedder, store, chunks, stats)

    # --- retrieval primitives --------------------------------------------

    @property
    def bm25(self):
        if self._bm25 is None:
            from .retrieval import BM25
            self._bm25 = BM25([c.text for c in self.chunks])
        return self._bm25

    def dense(self, query_vectors: np.ndarray, k: int,
              where: dict | None = None) -> list[tuple[str, float]]:
        """Nearest chunks for one or more query vectors, merged by best score."""
        count = self.store.collection.count()
        if not count:
            return []
        res = self.store.collection.query(
            query_embeddings=[v.tolist() for v in np.atleast_2d(query_vectors)],
            n_results=min(k, count), where=where or None)
        best: dict[str, float] = {}
        for ids, distances in zip(res['ids'], res['distances']):
            for chunk_id, distance in zip(ids, distances):
                score = 1.0 - float(distance)
                if score > best.get(chunk_id, -2.0):
                    best[chunk_id] = score
        return sorted(best.items(), key=lambda kv: -kv[1])

    def vectors_for(self, chunk_ids: list[str]) -> np.ndarray:
        """Stored vectors, for MMR. Read back from Chroma rather than
        re-embedded, so a slow embedder is not re-run per query."""
        if not chunk_ids:
            return np.zeros((0, 1), dtype=np.float32)
        got = self.store.collection.get(ids=chunk_ids, include=['embeddings'])
        order = {cid: i for i, cid in enumerate(got['ids'])}
        stacked = np.array(got['embeddings'], dtype=np.float32)
        return np.array([stacked[order[cid]] for cid in chunk_ids
                         if cid in order], dtype=np.float32)

    def neighbors(self, chunk: Chunk) -> list[Chunk]:
        """Chunks either side of this one inside the same session."""
        siblings = [c for c in self.by_session.get(chunk.session_id, [])
                    if c.layer == chunk.layer]
        if chunk not in siblings:
            return []
        i = siblings.index(chunk)
        return [c for c in siblings[max(0, i - 1):i + 2] if c is not chunk]

    def drop(self) -> None:
        self.store.drop()


def _lab_llm(settings: LabSettings):
    """The production provider, or the offline fake when there is no key — the
    lab must remain runnable with no network at all."""
    from lodestar_brain.llm.fake import FakeProvider
    from lodestar_brain.llm.openrouter import OpenRouterProvider
    if not settings.openrouter_api_key:
        return FakeProvider()
    return OpenRouterProvider(api_key=settings.openrouter_api_key,
                              base_url=settings.openrouter_base_url,
                              default_model=settings.llm_model)


class IndexRegistry:
    """Process-lifetime cache of built indexes, keyed by fingerprint. Chroma
    keeps the vectors between restarts; this keeps the chunk table and BM25
    statistics, which are what make a sweep of retrieval settings instant."""

    def __init__(self, settings: LabSettings, diary: dict):
        self.settings = settings
        self.diary = diary
        self._indexes: dict[str, LabIndex] = {}

    def get(self, cfg: IndexConfig, progress=None, force: bool = False) -> LabIndex:
        key = cfg.normalized().fingerprint()
        if force or key not in self._indexes:
            self._indexes[key] = LabIndex.build(cfg, self.diary, self.settings,
                                                progress=progress, force=force)
        return self._indexes[key]

    def known(self) -> list[dict]:
        return [{'fingerprint': key, 'collection': ix.stats.collection,
                 'chunks': ix.stats.chunks, 'by_layer': ix.stats.by_layer,
                 'config': ix.cfg.__dict__ | {'layers': list(ix.cfg.layers)}}
                for key, ix in self._indexes.items()]
