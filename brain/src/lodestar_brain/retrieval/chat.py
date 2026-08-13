"""Per-board chat memory in Chroma: the half of retrieval that is a record.

Unlike `CardIndex`, which is derived from `/api/state` and can be thrown away,
the transcript is stored nowhere else — losing it is losing the data. The
argument for one store here and an in-process index there is written out in
`cards.py`.
"""
import json
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from .. import textnorm
from .chunking import day_int, flatten_metadata, split_text
from .expand import QUESTION_WORDS, expand_queries
from .fusion import CANDIDATES, RECALL_WEIGHTS, RRF_K, RankBM25Retriever
from .timescope import TimeScope, _to_int, where_clause

# Sentinel url selecting an in-process client: no server, no disk, no network.
MEMORY_URL = 'memory'


def parse_chroma_url(url: str) -> tuple[str, int, bool]:
    """Split a Chroma url into the (host, port, ssl) HttpClient wants."""
    parsed = urlparse(url)
    ssl = parsed.scheme == 'https'
    return parsed.hostname, parsed.port or (443 if ssl else 80), ssl


def ensure_database(url: str, database: str,
                    tenant: str = 'default_tenant') -> None:
    """Create the database if it is missing. Chroma auto-creates collections but
    not databases, so a fresh server would otherwise fail on the first record.
    Idempotent: an existing database answers with an HTTP error we ignore, so
    brains racing on startup are harmless. A *connection* failure propagates —
    the caller decides whether to degrade."""
    endpoint = f'{url.rstrip("/")}/api/v2/tenants/{tenant}/databases'
    request = urllib.request.Request(
        endpoint, data=json.dumps({'name': database}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError:
        pass   # already exists


# The board a chunk written before boards existed belongs to. Matches the
# server's own column default, so a chunk indexed then and a session migrated
# then agree about where they are.
LEGACY_BOARD = 'main'


def _on_board(metadata: dict | None, board_id: str | None) -> bool:
    """Whether a chunk belongs to the board being searched.

    True whenever `board_id` is None, so a caller that names no board — an
    eval, a curl, the boot sync — sees everything, exactly as before boards
    existed. A chunk carrying no board_id is one indexed before they did, and
    those live on the default board.
    """
    if not board_id:
        return True
    return (metadata or {}).get('board_id', LEGACY_BOARD) == board_id


def _in_session(metadata: dict | None, session_id: str | None) -> bool:
    """Whether a chunk belongs to the session being excluded.

    False whenever `session_id` is None, so a caller that names no session — an
    eval, a curl, the recall box — sees everything, exactly as before sessions
    existed.
    """
    if not session_id:
        return False
    return (metadata or {}).get('session_id') == session_id


class ChatStore:
    """Per-board chat memory in Chroma, through langchain-chroma.

    Chroma rather than an in-process index — unlike `CardIndex` — because the
    transcript is stored nowhere else: losing it is losing the data, not losing a
    cache. It runs as a *service* shared by every board, brain and test, so real
    and non-real memory are separated by **database**, not by collection."""

    def __init__(self, url: str, embeddings: Embeddings, collection: str = 'chat',
                 database: str = 'lodestar'):
        import chromadb   # local import: heavy, and only needed when memory is on
        from langchain_chroma import Chroma
        settings = chromadb.Settings(anonymized_telemetry=False)
        if url == MEMORY_URL:
            client = chromadb.EphemeralClient(settings=settings)
        else:
            host, port, ssl = parse_chroma_url(url)
            ensure_database(url, database)
            client = chromadb.HttpClient(host=host, port=port, ssl=ssl,
                                         database=database, settings=settings)
        self.url, self.database = url, database
        self.collection_name = collection
        self.client = client
        # Cosine, stated: the default is L2, and a relevance score of 1 - distance
        # only means what it says if the space is cosine.
        self.store = Chroma(client=client, collection_name=collection,
                            embedding_function=embeddings,
                            collection_metadata={'hnsw:space': 'cosine'})

    def record(self, texts: list[str], metadata: dict | None = None) -> None:
        """Chunk and store. Chunking lives here rather than at the call site so
        one place decides how a transcript is cut."""
        chunks = [chunk for text in texts for chunk in split_text(text)]
        if not chunks:
            return
        payload = {'created_day': _to_int(datetime.now(timezone.utc).date())}
        payload |= flatten_metadata(metadata or {})
        self.store.add_texts(chunks, metadatas=[dict(payload)] * len(chunks))

    def index_messages(self, rows: list[dict]) -> None:
        """Chunk recorded messages (assistant.db rows, via the Node API) into
        the index. Chunk ids derive from the message id, so Chroma *upserts*:
        indexing the same row twice never duplicates it — what makes a rebuild
        safe to run at every boot. created_day comes from the message's own
        createdAt, never from today, so time-scoped recall works on imported
        history too."""
        texts, ids, metadatas = [], [], []
        for row in rows:
            payload = {'message_id': int(row['id']), 'role': row.get('role', ''),
                       'created_day': day_int(row.get('createdAt')),
                       # Which conversation this came from, so recall can skip
                       # the one the model is already reading.
                       'session_id': row.get('sessionId') or '',
                       # And which board, so it cannot reach into another one.
                       'board_id': row.get('boardId') or LEGACY_BOARD}
            for n, chunk in enumerate(split_text(row.get('content', ''))):
                texts.append(chunk)
                ids.append(f"{row['id']}:{n}")
                metadatas.append(dict(payload))
        if texts:
            self.store.add_texts(texts, metadatas=metadatas, ids=ids)

    def sync(self, rows: list[dict]) -> int:
        """Index every recorded message this index does not know yet; returns
        how many were missing. The rebuild path: a turn recorded while Chroma
        was down becomes recallable the next time a brain boots and syncs."""
        known = set()
        for chunk_id in self.store.get()['ids']:
            head, sep, _ = chunk_id.partition(':')
            if sep and head.isdigit():   # pre-record chunks keep uuid ids
                known.add(int(head))
        missing = [row for row in rows if int(row['id']) not in known]
        self.index_messages(missing)
        return len(missing)

    def prune(self, rows: list[dict]) -> int:
        """Drop chunks whose message is no longer in the live record; returns how
        many messages were dropped.

        The missing half of a derived index. `sync` only ever adds, so deleting a
        chat left its chunks answering `recall_chat` forever — a conversation the
        user deleted resurfacing in an answer, which is the worst possible
        version of a history feature. Called wherever `sync` is (boot, and
        /rag/chat/reindex, which the browser fires right after a delete).

        Chunks with a uuid id are left alone. Those predate the record and carry
        no message_id, so "not in the live record" is not a claim that can be
        made about them — treating them as orphans would silently wipe the
        chat memory of any board that has been running since before Stage 2.
        """
        live = {int(row['id']) for row in rows}
        doomed, dropped = [], set()
        for chunk_id in self.store.get()['ids']:
            head, sep, _ = chunk_id.partition(':')
            if not (sep and head.isdigit()):
                continue
            if int(head) not in live:
                doomed.append(chunk_id)
                dropped.add(int(head))
        if doomed:
            self.store.delete(ids=doomed)
        return len(dropped)

    def chunks_on(self, day: int) -> list[dict]:
        """Every chunk stamped with that created_day, as its metadata dict.
        Not a search: the recap tool reports what a day holds, and a query
        would decide relevance where the question was only 'how much'."""
        got = self.store.get(where={'created_day': day})
        return [dict(meta) for meta in got.get('metadatas') or []]

    def search(self, text: str, k: int = 5,
               scope: TimeScope | None = None, *,
               evidence: bool = True, exclude_session: str | None = None,
               board_id: str | None = None) -> list[dict]:
        """Hybrid, BM25-heavy: dense from Chroma, BM25 over the same chunks,
        each fused across the expanded queries (synonyms, cross-script), then
        combined by weighted RRF (RECALL_WEIGHTS). Fused inline rather than
        via `hybrid_retriever` because `EnsembleRetriever` discards the fused
        score, and this result carries one all the way to the UI.

        `evidence` keeps or drops the lexical floor below. It exists for who
        is reading: the recall box keeps it, because a human scanning a result
        list cannot tell an unexplained hit from a broken search; the agent's
        find_related turns it off, because a model judges relevance itself and
        the floor would silently drop exactly the semantic matches — «دعوا»
        reaching «دعوامون» — that are the point of having a dense index.

        `exclude_session` drops one conversation from the results: the one the
        caller is already reading. `board_id` keeps only the board being
        searched. Both are filtered here rather than in a Chroma `where` clause
        so that the lexical half is filtered by the same rule — two halves of
        one search disagreeing about what is eligible is a bug nobody would
        find."""
        total = self.count()
        if total == 0:
            return []   # an empty store is a normal state, not a failed query
        raw = self.store.get()
        corpus = [Document(id=id_, page_content=content, metadata=meta or {})
                  for id_, content, meta in zip(raw['ids'], raw['documents'],
                                                raw['metadatas'])
                  if _on_board(meta, board_id)
                  and not _in_session(meta, exclude_session)]
        if not corpus:
            return []
        depth = min(total, max(k, CANDIDATES))
        chroma_filter = where_clause(scope, fields=('created_day',))
        bm25 = RankBM25Retriever.from_documents(corpus, k=depth, scope=scope)
        queries = expand_queries(text)
        dense_rankings = [self.store.similarity_search(q, k=depth,
                                                       filter=chroma_filter)
                          for q in queries]
        lexical_rankings = [bm25.invoke(q) for q in queries]
        # Keyed on the text, not the chunk id: the dense half's documents come
        # back from Chroma and the lexical half's from the corpus above, and an
        # id only one side carries would count the same chunk twice.
        scores: dict[str, float] = {}
        seen: dict[str, Document] = {}
        for weight, rankings in zip(RECALL_WEIGHTS,
                                    (dense_rankings, lexical_rankings)):
            for ranking in rankings:
                for rank, doc in enumerate(ranking, start=1):
                    # The dense half comes straight from Chroma, so it has not
                    # been through the corpus filter above.
                    if _in_session(doc.metadata, exclude_session):
                        continue
                    seen.setdefault(doc.page_content, doc)
                    scores[doc.page_content] = (
                        scores.get(doc.page_content, 0.0)
                        + weight / (len(rankings) * (RRF_K + rank)))
        # Lexical evidence is the floor: dense similarity orders the matches
        # but never invents one. Without it, a query matching nothing is
        # padded to k with its nearest noise — which reads as a broken search.
        # Evidence means sharing an informative term with any spelling of the
        # query — deliberately not "a positive BM25 score", which a tiny
        # corpus denies even to an exact match (negative IDF).
        ordered = sorted(scores, key=lambda key: -scores[key])
        if evidence:
            query_terms = ({token for q in queries
                            for token in textnorm.tokens(q)} - QUESTION_WORDS)
            ordered = [key for key in ordered
                       if query_terms & set(textnorm.tokens(key))]
        ordered = ordered[:k]
        return [{'text': key, 'score': round(scores[key], 4),
                 'metadata': dict(seen[key].metadata)} for key in ordered]

    def count(self) -> int:
        return self.client.get_collection(self.collection_name).count()

    def drop(self) -> None:
        """Delete this collection. For tests cleaning up after themselves —
        never wired to anything a user can reach."""
        self.client.delete_collection(self.collection_name)
