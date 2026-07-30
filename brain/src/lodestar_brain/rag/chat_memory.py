"""Per-board chat memory: chunks of assistant-chat text recorded with their
embeddings in a Chroma collection.

Chroma runs as a *service* (Docker, localhost:8001) rather than an on-disk
store, so every board, brain and test shares one server. Real and non-real data
are kept in separate databases so all non-production memory drops in one call:

    database 'lodestar'       -> chat-board-3000    board.db, the real board
    database 'lodestar-test'  -> chat-board-3001    the paired test board
                              -> chat-test-<uuid>   pytest, dropped in teardown

`url` selects the backend, so nothing here is hardwired to a running server:
'' is off, 'memory' is an in-process EphemeralClient (offline unit tests and
e2e), and any http(s) URL is the real thing. One record holds the chunk *and*
its vector — Chroma only stores and searches vectors, keeping the Embedder
protocol substitutable."""
import json
import urllib.request
import uuid
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from .embedder import Embedder

# Sentinel url selecting the in-process client: no server, no disk, no network.
MEMORY_URL = 'memory'


class RecallChatArgs(BaseModel):
    text: str
    k: int = Field(5, ge=1, le=20)


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


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def flatten_metadata(metadata: dict) -> dict:
    """Chroma metadata values must be scalars or lists of scalars — a nested
    dict raises. Rather than drop the record, JSON-encode the nested value under
    '<key>_json' so the body survives. Anything you need to filter on has to be
    a scalar key of its own: you cannot query inside a JSON string."""
    flat: dict = {}
    for key, value in metadata.items():
        if _is_scalar(value):
            flat[key] = value
        elif isinstance(value, list) and all(_is_scalar(v) for v in value):
            flat[key] = value
        else:
            flat[f'{key}_json'] = json.dumps(value)
    return flat


class ChromaChatMemory:
    def __init__(self, url: str, embedder: Embedder, collection: str = 'chat',
                 database: str = 'lodestar'):
        import chromadb  # local import: heavy, only needed when memory is on
        self.embedder = embedder
        self.url = url
        self.database = database
        self.collection_name = collection
        settings = chromadb.Settings(anonymized_telemetry=False)
        if url == MEMORY_URL:
            self.client = chromadb.EphemeralClient(settings=settings)
        else:
            host, port, ssl = self.parse_url(url)
            # Chroma auto-creates collections but NOT databases, so a fresh
            # server would otherwise fail on the very first record().
            self.ensure_database(url, database)
            self.client = chromadb.HttpClient(host=host, port=port, ssl=ssl,
                                              database=database,
                                              settings=settings)
        self.collection = self.client.get_or_create_collection(
            collection, metadata={'hnsw:space': 'cosine'},
            embedding_function=None)  # vectors always come from the Embedder

    @staticmethod
    def parse_url(url: str) -> tuple[str, int, bool]:
        """Split a Chroma url into the (host, port, ssl) HttpClient wants."""
        parsed = urlparse(url)
        ssl = parsed.scheme == 'https'
        return parsed.hostname, parsed.port or (443 if ssl else 80), ssl

    @staticmethod
    def ensure_database(url: str, database: str,
                        tenant: str = 'default_tenant') -> None:
        """Create the database if it is missing. Idempotent: an existing
        database answers with an HTTP error we can ignore, so brains racing on
        startup are harmless. A *connection* failure is left to propagate — the
        caller decides whether to degrade."""
        endpoint = f'{url.rstrip("/")}/api/v2/tenants/{tenant}/databases'
        request = urllib.request.Request(
            endpoint, data=json.dumps({'name': database}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError:
            pass  # already exists

    def record(self, texts: list[str], metadata: dict | None = None) -> None:
        texts = [t for t in texts if t.strip()]
        if not texts:
            return
        vectors = self.embedder.embed(texts)
        flat = flatten_metadata(metadata) if metadata else None
        self.collection.add(
            ids=[uuid.uuid4().hex for _ in texts],
            documents=texts,
            embeddings=[v.tolist() for v in vectors],
            metadatas=[dict(flat)] * len(texts) if flat else None)

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

    def drop(self) -> None:
        """Delete this collection. Used by tests to clean up after themselves —
        never wired to anything a user can reach."""
        self.client.delete_collection(self.collection_name)


def make_recall_tool(memory: ChromaChatMemory) -> BaseTool:
    @tool('recall_chat', args_schema=RecallChatArgs)
    def recall_chat(text: str, k: int = 5) -> list[dict]:
        """Recall relevant snippets from past assistant conversations on this
        board. Use it to answer questions about things discussed before."""
        return memory.search(text, k=k)

    return recall_chat
