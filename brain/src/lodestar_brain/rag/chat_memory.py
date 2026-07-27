"""Per-board chat memory: chunks of assistant-chat text recorded with their
embeddings in a persistent Chroma store. One store per board (chroma/board-3000
pairs with board.db, chroma/board-3001 with board-3001.db) so recall never
leaks across boards. Embeddings come from the Embedder protocol — Chroma only
stores and searches vectors, keeping the embedder substitutable."""
import uuid

from ..tools.base import Tool
from .embedder import Embedder


def chunk_text(text: str, max_chars: int = 500) -> list[str]:
    chunks: list[str] = []
    current = ''
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = f'{current} {word}' if current else word
    if current:
        chunks.append(current)
    return chunks


class ChromaChatMemory:
    def __init__(self, persist_dir: str, embedder: Embedder,
                 collection: str = 'chat'):
        import chromadb  # local import: heavy, only needed when memory is on
        self.embedder = embedder
        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=chromadb.Settings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(
            collection, metadata={'hnsw:space': 'cosine'})

    def record(self, texts: list[str], metadata: dict | None = None) -> None:
        texts = [t for t in texts if t.strip()]
        if not texts:
            return
        vectors = self.embedder.embed(texts)
        self.collection.add(
            ids=[uuid.uuid4().hex for _ in texts],
            documents=texts,
            embeddings=[v.tolist() for v in vectors],
            metadatas=[dict(metadata)] * len(texts) if metadata else None)

    def search(self, text: str, k: int = 5) -> list[dict]:
        count = self.collection.count()
        if count == 0:
            return []
        query_vec = self.embedder.embed([text])[0]
        res = self.collection.query(query_embeddings=[query_vec.tolist()],
                                    n_results=min(k, count))
        return [{'text': doc, 'score': float(1.0 - dist), 'metadata': meta or {}}
                for doc, meta, dist in zip(res['documents'][0],
                                           res['metadatas'][0],
                                           res['distances'][0])]


def make_recall_tool(memory: ChromaChatMemory) -> Tool:
    def recall_chat(text: str, k: int = 5) -> list[dict]:
        return memory.search(text, k=k)

    return Tool(
        'recall_chat',
        'Recall relevant snippets from past assistant conversations on this '
        'board. Use it to answer questions about things discussed before.',
        {'type': 'object', 'properties': {
            'text': {'type': 'string'},
            'k': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
         'required': ['text']},
        recall_chat)
