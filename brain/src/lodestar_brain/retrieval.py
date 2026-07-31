"""Retrieval's foundation: what gets embedded, and how it is shaped for a store.

This is the shipped half of candidate F (`docs/rag-chosen-architecture.md`) —
the pieces every later stage stands on. Three things live here:

**The embeddings seam.** `make_embeddings` names a backend and an unknown value
raises, like every other seam in the brain. The default is a real Persian
encoder, because the embedder *is* the architecture: on the lab's Farsi corpus
hash embedding measured ~0.01 recall against 0.617 for
`heydariAI/persian-embeddings`, a ~60× effect, while every other knob in the
sweep was worth under 2%. `fake` is the offline-test value and is deliberately
*lexical* — it ranks by shared letters, so an offline ranking assertion means
something. It is never semantic: a paraphrase with no shared text is invisible
to it.

**The splitter.** `RecursiveCharacterTextSplitter`, with the Persian sentence
enders in its separator list, so a chat transcript is cut at a boundary a reader
would recognise rather than at character 500.

**The document shape.** A board card becomes one `Document` whose metadata is
flat, complete and filterable. Complete matters: a key only some documents carry
turns a `where` clause into a silent partial scan, which reads as a retrieval
bug rather than the schema bug it is.

Everything here implements LangChain's own interfaces (`Embeddings`,
`Document`), so the retrievers assembled on top of it in the rest of this module
accept these objects without adapters.
"""
import hashlib
import json
from datetime import datetime, timezone

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import textnorm

# --- embeddings -------------------------------------------------------------

BACKENDS = ('fastembed', 'sentence-transformers')

# The measured winner on the diary corpus: an XLM-RoBERTa fine-tuned on Persian
# rather than multilingual by accident. ~2.2 GB on first use, and it needs the
# 'local-embeddings' extra.
DEFAULT_LOCAL_MODEL = 'heydariAI/persian-embeddings'
# fastembed's smallest Farsi-capable option, for an environment that wants ONNX
# and a ~120 MB download instead.
DEFAULT_FASTEMBED_MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'

BACKEND_DEFAULTS = {'sentence-transformers': DEFAULT_LOCAL_MODEL,
                    'fastembed': DEFAULT_FASTEMBED_MODEL}

# Models trained to see a question and a passage differently, and the strings
# they were trained on. The prefix belongs to the *model*, not to the backend:
# the Persian default is symmetric and appears nowhere in this table. Losing a
# prefix costs accuracy and raises nothing, which is why it is written down
# beside the model rather than left to a call site.
E5_PREFIXES = ('query: ', 'passage: ')
QWEN3_INSTRUCT = ('Instruct: Given a question about a personal journal, '
                  'retrieve the passages that answer it\nQuery: ')
EMBED_PREFIXES = {
    'intfloat/multilingual-e5-small': E5_PREFIXES,
    'intfloat/multilingual-e5-base': E5_PREFIXES,
    'intfloat/multilingual-e5-large': E5_PREFIXES,
    'Qwen/Qwen3-Embedding-0.6B': (QWEN3_INSTRUCT, ''),
    'Qwen/Qwen3-Embedding-8B': (QWEN3_INSTRUCT, ''),
}

LEXICAL_DIM = 1024


def _bucket(token: str, dim: int) -> int:
    return int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16) % dim


def _unit(vectors: np.ndarray) -> np.ndarray:
    """Cosine similarity is only cosine if the vectors are unit length."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


class LexicalHashEmbeddings(Embeddings):
    """Hashed character 4-grams over normalised text — offline, deterministic,
    and script-agnostic.

    Character n-grams rather than words because Persian inflects by affix
    (میخواستم / نمیخوام / بخوام share a stem no whitespace tokeniser finds), so
    n-grams recover the overlap word tokens miss. The lab measured this at 0.386
    session recall where the ASCII token-bucket embedder it replaces scored
    0.014 — chance. Still lexical: it sees letters, never meaning.
    """
    dim = LEXICAL_DIM

    def __init__(self, n: int = 4):
        self.n = n

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for gram in textnorm.char_ngrams(text, self.n):
                out[i, _bucket(gram, self.dim)] += 1.0
        return [vector.tolist() for vector in _unit(out)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class _PrefixedEmbeddings(Embeddings):
    """Shared halves of the two model backends: the prefixes and the shape."""

    def __init__(self, model_name: str, query_prefix: str = '',
                 passage_prefix: str = '', batch_size: int = 32):
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.batch_size = batch_size

    def _encode(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def _vectors(self, texts: list[str], prefix: str) -> list[list[float]]:
        payload = [prefix + text for text in texts] if prefix else list(texts)
        return [vector.tolist() for vector in _unit(self._encode(payload))]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._vectors(list(texts), self.passage_prefix)

    def embed_query(self, text: str) -> list[float]:
        return self._vectors([text], self.query_prefix)[0]


def _sentence_transformer(model_name: str):
    from sentence_transformers import SentenceTransformer  # 'local-embeddings'
    return SentenceTransformer(model_name)


class SentenceTransformerEmbeddings(_PrefixedEmbeddings):
    """Any HuggingFace checkpoint. The backend that reaches the Persian-tuned
    encoders fastembed does not serve — which, on this corpus, are the ones
    worth running. The checkpoint's own `modules.json` decides pooling, so a
    model is used the way its authors trained it rather than mean-pooled by us
    and quietly mis-scored.

    `factory` exists so the prefix behaviour is testable without a 2 GB
    download; production leaves it alone."""

    def __init__(self, model_name: str, query_prefix: str = '',
                 passage_prefix: str = '', batch_size: int = 32, factory=None):
        super().__init__(model_name, query_prefix, passage_prefix, batch_size)
        self.model = (factory or _sentence_transformer)(model_name)
        # Renamed in sentence-transformers 5; ask for the new name and fall back,
        # rather than emitting a deprecation warning on every boot.
        dimension = (getattr(self.model, 'get_embedding_dimension', None)
                     or self.model.get_sentence_embedding_dimension)
        self.dim = int(dimension())

    def _encode(self, texts: list[str]) -> np.ndarray:
        vectors = self.model.encode(texts, batch_size=self.batch_size,
                                    show_progress_bar=False,
                                    convert_to_numpy=True)
        return np.asarray(vectors, dtype=np.float32)


def _text_embedding(model_name: str):
    from fastembed import TextEmbedding  # optional 'semantic' extra
    return TextEmbedding(model_name)


class FastEmbedEmbeddings(_PrefixedEmbeddings):
    """fastembed's short ONNX list: a smaller download and no torch, at the cost
    of only reaching the models it happens to serve."""

    def __init__(self, model_name: str, query_prefix: str = '',
                 passage_prefix: str = '', batch_size: int = 64, factory=None):
        super().__init__(model_name, query_prefix, passage_prefix, batch_size)
        self.model = (factory or _text_embedding)(model_name)
        self.dim = len(next(iter(self.model.embed(['probe']))))

    def _encode(self, texts: list[str]) -> np.ndarray:
        vectors = list(self.model.embed(texts, batch_size=self.batch_size))
        return np.asarray(vectors, dtype=np.float32)


def resolve_embed_model(kind: str, settings=None, model: str = '') -> str:
    """The model a backend will actually load: what was pinned, else that
    backend's own default. Kept in one place so the configuration and the
    embedder it describes can never name different models."""
    if kind not in BACKENDS:
        return ''
    return (model or getattr(settings, 'embed_model', '')
            or BACKEND_DEFAULTS[kind])


def make_embeddings(kind: str, settings=None, model: str = '') -> Embeddings:
    """The embedder seam. A new backend is a new branch here, never an edited
    call site, and an unknown value raises at boot."""
    if kind == 'fake':
        return LexicalHashEmbeddings()
    if kind in BACKENDS:
        name = resolve_embed_model(kind, settings, model)
        query_prefix, passage_prefix = EMBED_PREFIXES.get(name, ('', ''))
        builder = (SentenceTransformerEmbeddings if kind == 'sentence-transformers'
                   else FastEmbedEmbeddings)
        return builder(name, query_prefix=query_prefix,
                       passage_prefix=passage_prefix)
    if kind == 'hash':
        # Retired *by name*. Dropping it silently would leave a stale
        # BRAIN_EMBEDDER=hash selecting whatever replaced it, which is how the
        # old 'auto' mode ran token buckets for months while the config claimed
        # embeddings.
        raise ValueError(
            "embedder 'hash' is retired: it tokenised [a-z0-9]+, so non-Latin "
            "text embedded to the zero vector. Use 'fake' for offline tests "
            "(lexical, never semantic) or a real backend: "
            f'{", ".join(BACKENDS)}')
    raise ValueError(f'unknown embedder: {kind!r}; expected '
                     f'{", ".join((*BACKENDS, "fake"))}')


# --- chunking ---------------------------------------------------------------

# 500 characters keeps the granularity chat memory has always had; the overlap
# is the reason for the recursive splitter at all — a thought cut in half is
# whole in one of the two windows.
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100

# Cut at the largest boundary that fits. The Persian enders are in the list
# because the text is Farsi typed by a human: without «؟» and «،» a Persian
# paragraph falls through to the space separator and is cut mid-clause.
SEPARATORS = ['\n\n', '\n', '. ', '؟ ', '? ', '! ', '؛ ', '، ', ' ', '']

_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    separators=SEPARATORS, keep_separator=True)


def split_text(text: str) -> list[str]:
    """Chunk a transcript. Blank pieces are dropped rather than indexed: an
    empty document is a row that matches nothing and dilutes every score."""
    return [chunk.strip() for chunk in _SPLITTER.split_text(text)
            if chunk.strip()]


# --- documents --------------------------------------------------------------

# Every key, on every document. A field only some rows carry turns a `where`
# clause into a silent partial scan.
CARD_META_KEYS = ('id', 'num', 'title', 'columnId', 'type', 'category', 'tags',
                  'created_day', 'updated_day')


def day_int(epoch_ms) -> int:
    """1773135000000 -> 20260310. Metadata filters compare numbers, not date
    strings, so the date rides as an int and a time scope is a $gte/$lte pair.
    UTC, deliberately: a filter that moves with the reader's timezone would make
    the same query match different cards on different machines. 0 for a missing
    timestamp — outside every real range, so it is excluded rather than
    matching everything."""
    if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
        return 0
    at = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return at.year * 10000 + at.month * 100 + at.day


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool))


def flatten_metadata(metadata: dict) -> dict:
    """Make a metadata dict a store will accept and a filter can read.

    Chroma takes scalars only. A list of scalars is space-joined rather than
    JSON-encoded, because a joined string is still searchable and filterable
    while a JSON string is neither — you cannot query inside one. `None` is
    dropped: the store rejects null, and an absent key fails a reader loudly
    instead of grouping every row under a nonexistent value. Anything genuinely
    nested survives under '<key>_json' so the body is not lost, with the
    understood cost that it cannot be filtered on."""
    flat: dict = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if _is_scalar(value):
            flat[key] = value
        elif isinstance(value, (list, tuple)) and all(map(_is_scalar, value)):
            flat[key] = ' '.join(str(item) for item in value)
        else:
            flat[f'{key}_json'] = json.dumps(value)
    return flat


def card_text(card: dict) -> str:
    """What a card looks like to the retriever. Title, notes and tags — the
    words the user actually wrote."""
    parts = [card.get('title') or '', card.get('notes') or '',
             ' '.join(card.get('tags') or [])]
    return ' '.join(part for part in parts if part).strip()


def card_document(card: dict) -> Document:
    """One card as one document. The title is repeated in the metadata even
    though it is in the text: a caller building a result list needs the title
    alone, and re-reading the board to get it would be a second round trip
    behind every tool call."""
    metadata = flatten_metadata({
        'id': card.get('id') or '',
        'num': card.get('num') or 0,
        'title': card.get('title') or '',
        'columnId': card.get('columnId') or '',
        'type': card.get('type') or '',
        'category': card.get('category') or '',
        'tags': card.get('tags') or [],
        'created_day': day_int(card.get('createdAt')),
        'updated_day': day_int(card.get('updatedAt')),
    })
    # flatten_metadata drops nothing here — every value above is a scalar or a
    # list — but an empty tag list joins to '', so the key set stays complete.
    metadata.setdefault('tags', '')
    return Document(id=card.get('id') or '', page_content=card_text(card),
                    metadata=metadata)


"""Alternatives considered

**1. "Why are the embedders yours? LangChain ships a wrapper for all three."**

*Short answer.* Because the two that exist would have to be corrected at every
call site and the third is unusable in tests. LangChain's `Embeddings` interface
*is* used — these classes implement it, so every retriever and vector store in
the framework accepts them. What is ours is the twenty lines behind that
interface, not the seam.

*Why the obvious option fails.* The obvious offline embedder is
`langchain_core.embeddings.DeterministicFakeEmbedding`. It seeds a PRNG from a
hash of the whole text, so «I ran this morning» and «I ran this evening» are
near-orthogonal — the similarity it reports is hash luck, not overlap. Every
offline ranking assertion in `brain/tests` ("the related card outranks the
unrelated one") would then pass or fail by accident, which is worse than having
no test. `LexicalHashEmbeddings` ranks by shared character 4-grams, so the
assertion holds for the reason it claims to.

*Why not the framework.* `langchain-huggingface`'s `HuggingFaceEmbeddings` would
serve the sentence-transformers backend and is close to what is written here.
The gap is the prefixes: its `embed_query` differs from `embed_documents` only
in batching, with no notion of a per-model query string. The E5 model cards
require `query: ` / `passage: ` and Qwen3's require a task instruction; omitting
them is a measurable accuracy loss that raises nothing, which is the exact class
of failure the RAG lab exists to catch. `langchain-community`'s
`FastEmbedEmbeddings` has the same gap and additionally prints a
sunset/unmaintained warning on import. Elsewhere in this module the framework is
taken as it comes: `RecursiveCharacterTextSplitter`, `Document`, `Embeddings`,
and — in the rest of the file — `EnsembleRetriever`, `MultiQueryRetriever` and
`langchain-chroma`.

*The libraries that would do it.*

- `langchain-huggingface` — maintained, first-party, the right answer for the
  sentence-transformers backend on a greenfield project *if* the models involved
  need no prefixes.
- `langchain-community` `FastEmbedEmbeddings` — a thinner wrapper than ours, at
  the cost of depending on a package that announces it is no longer maintained.
- `sentence-transformers` alone, with `prompts` / `prompt_name` — since 2.4 the
  library can carry named prompts in the model config, which is the same idea as
  `EMBED_PREFIXES` done one level lower. The catch is that the E5 checkpoints do
  not ship a prompt config, so the strings still have to come from somewhere.
- `langchain_core.embeddings.DeterministicFakeEmbedding` — for the fake, and
  only if no test ever asserts an ordering.

*Why they were not adopted, and what would change it.* Decisively: the prefix
table is the difference between a run that measures the model and a run that
measures the model handicapped, and no wrapper here applies it. Secondarily, the
`fake` backend has to be lexical for the offline suite to assert anything about
ranking. What would change it: `langchain-huggingface` growing a `prompt_name`
(or equivalent) pass-through to `model.encode`. At that point the
sentence-transformers class becomes a two-line subclass or disappears, and only
`LexicalHashEmbeddings` and `flatten_metadata` stay ours.

**2. "Why is `flatten_metadata` yours? `langchain-chroma` has a filter for
this."**

*Short answer.* Because the framework's version deletes data and this one
converts it. Both make Chroma accept the record; only one leaves the tags
searchable.

*Why the obvious option fails.* `filter_complex_metadata` drops any value that
is not a scalar. A card's `tags` is a list, so the framework's helper silently
removes the field — and the tags are among the few words on a card that the user
chose as an index term. Nothing raises; the card is simply harder to find
afterwards, which surfaces months later as "search does not work" rather than as
an error.

*Why not the framework, and the libraries.* There is no third option here worth
naming: Chroma's own client raises on a non-scalar, `filter_complex_metadata`
drops it, and a JSON blob under the original key would be accepted but
unfilterable — you cannot query inside a JSON string, so it would look like a
working filter that never matches. Space-joining is the only one of the three
that keeps the value both stored and queryable. (Preserving lists properly would
mean a store with typed array fields — Qdrant, Weaviate, Postgres with
`pgvector` — a much larger decision than this function.)

*Why not adopted, and what would change it.* Joining is lossy in one specific
way: a tag containing a space becomes two tokens. That is acceptable while tags
are single words, and the board's tag input does not encourage otherwise. If
tags become phrases, the fix is not this function — it is moving the chat store
off Chroma to something with a real array type, and that is a Session-5-sized
argument, not a helper.
"""
