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
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import numpy as np
from langchain_classic.retrievers import EnsembleRetriever, MultiQueryRetriever
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

from . import textnorm, translit

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
    """Shared halves of the two model backends: the prefixes and the shape.

    **The weights load on first use, not on construction.** Two reasons, and
    both were failures rather than preferences: `server.py` builds its app at
    import time, so an eager load made merely importing the module require the
    optional extra; and a 2.2 GB download inside `create_app` blocks /health
    until it finishes, which reads as a hung container. What *is* checked
    eagerly is that the backend can be imported at all — cheap, offline, and
    enough to keep the no-auto-modes promise that a misconfigured brain fails at
    boot instead of on someone's first question."""

    def __init__(self, model_name: str, query_prefix: str = '',
                 passage_prefix: str = '', batch_size: int = 32, factory=None):
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.batch_size = batch_size
        self._factory = factory or self._default_factory
        self._model = None
        if factory is None:
            self._check_installed()

    @property
    def model(self):
        if self._model is None:
            self._model = self._factory(self.model_name)
        return self._model

    @staticmethod
    def _default_factory(model_name):
        raise NotImplementedError

    @staticmethod
    def _check_installed() -> None:
        raise NotImplementedError

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

    _default_factory = staticmethod(_sentence_transformer)

    @staticmethod
    def _check_installed() -> None:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "embedder 'sentence-transformers' needs the 'local-embeddings' "
                'extra: uv sync --extra local-embeddings') from exc

    @property
    def dim(self) -> int:
        # Renamed in sentence-transformers 5; ask for the new name and fall back,
        # rather than emitting a deprecation warning on every load.
        dimension = (getattr(self.model, 'get_embedding_dimension', None)
                     or self.model.get_sentence_embedding_dimension)
        return int(dimension())

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

    _default_factory = staticmethod(_text_embedding)

    @staticmethod
    def _check_installed() -> None:
        try:
            import fastembed  # noqa: F401
        except ImportError as exc:
            raise ValueError("embedder 'fastembed' needs the 'semantic' extra: "
                             'uv sync --extra semantic') from exc

    @property
    def dim(self) -> int:
        return len(next(iter(self.model.embed(['probe']))))

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
    """What a card looks like to the retriever: title, notes and tags — the
    words the user actually wrote — plus the type and category labels they
    filed it under, so "habit" or "love" finds the cards stamped that way."""
    parts = [card.get('title') or '', card.get('notes') or '',
             ' '.join(card.get('tags') or []),
             card.get('type') or '', card.get('category') or '']
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


# --- query understanding ----------------------------------------------------

# The date fields `card_document` writes. A card matches a time scope if it was
# either created or last touched inside it.
DATE_FIELDS = ('created_day', 'updated_day')

# Jalali month → (start month/day, end month/day) in the Gregorian year holding
# the *start* of that month. Mapped directly rather than through a Jalali
# conversion library: the mapping drifts by a day across the years a board
# spans, and the brain's dependency budget is worth more than that day.
JALALI_MONTHS = {
    'فروردین': ((3, 21), (4, 20)), 'اردیبهشت': ((4, 21), (5, 21)),
    'خرداد': ((5, 22), (6, 21)), 'تیر': ((6, 22), (7, 22)),
    'مرداد': ((7, 23), (8, 22)), 'شهریور': ((8, 23), (9, 22)),
    'مهر': ((9, 23), (10, 22)), 'آبان': ((10, 23), (11, 21)),
    'آذر': ((11, 22), (12, 21)), 'دی': ((12, 22), (1, 20)),
    'بهمن': ((1, 21), (2, 19)), 'اسفند': ((2, 20), (3, 20)),
}
SEASONS = {
    'بهار': ((3, 21), (6, 21)), 'تابستان': ((6, 22), (9, 22)),
    'تابستون': ((6, 22), (9, 22)), 'پاییز': ((9, 23), (12, 21)),
    'زمستان': ((12, 22), (3, 20)), 'زمستون': ((12, 22), (3, 20)),
}
# The English half is new and unmeasured — the lab's corpus is Farsi — and it is
# deliberately not a mirror. «پارسال تابستون» shifts a further year because the
# Persian year turns at Nowruz, while "last summer" means the most recent one.
ENGLISH_SEASONS = {'spring': ((3, 21), (6, 21)), 'summer': ((6, 22), (9, 22)),
                   'autumn': ((9, 23), (12, 21)), 'fall': ((9, 23), (12, 21)),
                   'winter': ((12, 22), (3, 20))}
LAST_YEAR = ('پارسال', 'سال پیش', 'سال گذشته', 'سال قبل')

# Paraphrases a board genuinely alternates between, for deterministic query
# expansion: cheap recall for the lexical half, which otherwise misses «همسرم»
# on a board that only ever writes «مهسا».
SYNONYMS = {
    'همسرم': ('مهسا',), 'زنم': ('مهسا',), 'مهسا': ('همسرم',),
    'مادرم': ('مامان',), 'مامان': ('مادرم',), 'پدرم': ('بابا',),
    'شغل': ('کار', 'جاب'), 'کار': ('شغل',), 'استخدام': ('آفر', 'قبول'),
    'بحث': ('دعوا',), 'دعوا': ('بحث', 'قهر'),
    'مالیات': ('اداره مالیات', 'جریمه'), 'ورزش': ('باشگاه',),
    'اپلای': ('درخواست', 'رزومه'), 'ریجکت': ('جواب رد', 'قبول نشدم'),
    'خونه': ('آپارتمان', 'اجاره'), 'خواب': ('بیخوابی', 'بی خوابی'),
}
# Interrogatives only. Ordinary English stopwords are deliberately absent: this
# set strips the *asking* from a question so the lexical half scores content
# words, and dropping 'the' from "renew the visa" would change the phrase a
# BM25 query is trying to match.
QUESTION_WORDS = frozenset("""
چی چه چرا چطور چگونه کجا کِی کی چند چقدر آیا بگو بهم راجب درباره درمورد هست بود
شد کردم دادم گفتم میشه کدوم کدام حالم وضعیت
what when where why how who whom which did does was were tell about
""".split())


def _searchable(names: dict) -> dict:
    """Match on normalised text but report the properly spelled name: a question
    typed «اذر» must resolve, while the label shown back must not look folded."""
    out: dict = {}
    for name, window in names.items():
        out.setdefault(textnorm.normalize(name), (name, window))
    return out


_MONTHS = _searchable(JALALI_MONTHS)
_SEASONS = _searchable(SEASONS)


@dataclass(frozen=True)
class TimeScope:
    """A resolved date range, as the two ints the metadata carries."""
    from_int: int
    to_int: int
    label: str
    kind: str

    def matches(self, metadata: dict, fields: tuple[str, ...] = DATE_FIELDS) -> bool:
        """The in-process half of the filter, used by BM25 and the card index.
        `where_clause` is the store's half, and both are derived from this one
        object: if the two could drift, hybrid fusion would compare two
        different candidate pools and call the result a ranking."""
        return any(self.from_int <= (metadata.get(field) or 0) <= self.to_int
                   for field in fields)

    def as_dict(self) -> dict:
        return {'from': _to_iso(self.from_int), 'to': _to_iso(self.to_int),
                'label': self.label, 'kind': self.kind}


def _to_int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def _to_iso(value: int) -> str:
    return f'{value // 10000:04d}-{(value // 100) % 100:02d}-{value % 100:02d}'


def _window(anchor: date, start: tuple[int, int],
            end: tuple[int, int]) -> tuple[date, date]:
    """The [start, end] window whose start precedes `anchor` — the one that has
    already happened. Handles windows crossing new year (دی, زمستان)."""
    first = date(anchor.year, *start)
    last = date(anchor.year + (1 if end < start else 0), *end)
    if first > anchor:
        first, last = date(first.year - 1, *start), date(last.year - 1, *end)
    return first, last


def _scope(first: date, last: date, label: str, kind: str) -> TimeScope:
    return TimeScope(_to_int(first), _to_int(last), label, kind)


def _previous_month(anchor: date) -> tuple[date, date]:
    end = anchor.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def resolve_time_scope(question: str, today: date | None = None) -> TimeScope | None:
    """A date range from the question's own time language, or None when it has
    none. Time words are the most selective filter available — a board holds a
    year of similar cards, and a date range cuts the pool before ranking starts.

    Returns the most recent matching window at or before `today`: «آذر» means
    the آذر that has already happened."""
    anchor = today or datetime.now(timezone.utc).date()
    text = textnorm.normalize(question)
    words = set(textnorm.tokens(text, drop_stopwords=False))
    shift_year = any(phrase in text for phrase in LAST_YEAR)

    def shifted(start, end, years=1):
        first, last = _window(anchor, start, end)
        return (date(first.year - years, *start), date(last.year - years, *end))

    for key, (label, (start, end)) in _SEASONS.items():
        if key in text:
            first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
            return _scope(first, last,
                          f'{label}{" پارسال" if shift_year else ""}', 'season')
    for key, (label, (start, end)) in _MONTHS.items():
        if key in words:
            first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
            return _scope(first, last, label, 'jalali-month')
    if 'نوروز' in text or 'عید' in words:
        start, end = (3, 18), (4, 4)
        first, last = shifted(start, end) if shift_year else _window(anchor, start, end)
        return _scope(first, last, 'نوروز', 'holiday')

    months_back = re.search(r'(\d+)\s*ماه\s*(?:پیش|قبل|گذشته|اخیر)', text)
    if months_back:
        span = int(months_back.group(1)) * 30
        return _scope(anchor - timedelta(days=span), anchor,
                      f'{span} روز اخیر', 'relative')
    if re.search(r'(هفته|هفتهٔ)\s*(پیش|قبل|گذشته)', text):
        return _scope(anchor - timedelta(days=10), anchor, 'هفته گذشته', 'relative')
    if re.search(r'ماه\s*(پیش|قبل|گذشته)', text):
        return _scope(*_previous_month(anchor), 'ماه گذشته', 'relative')
    if 'دیروز' in words:
        return _scope(anchor - timedelta(days=1), anchor - timedelta(days=1),
                      'دیروز', 'relative')
    if any(phrase in text for phrase in ('اخیرا', 'این چند وقت', 'این روزا',
                                         'این مدت')):
        return _scope(anchor - timedelta(days=60), anchor, 'اخیرا', 'relative')
    if shift_year:
        # پارسال is Nowruz to Nowruz, not January to January.
        return _scope(date(anchor.year - 1, 3, 21), date(anchor.year, 3, 20),
                      'پارسال', 'relative')

    english = _english_scope(text.lower(), anchor)
    if english:
        return english
    explicit = re.search(r'\b(20\d\d)\b', text)
    if explicit:
        year = int(explicit.group(1))
        return TimeScope(year * 10000 + 101, year * 10000 + 1231, str(year),
                         'gregorian-year')
    return None


def _english_scope(text: str, anchor: date) -> TimeScope | None:
    """The English half. Same rule as the Farsi one — the most recent window
    that has already happened — with one guard: bare 'fall' is a verb often
    enough that it needs a determiner, and a false positive on a time filter
    *removes* good candidates, which is worse than missing the filter."""
    for name, (start, end) in ENGLISH_SEASONS.items():
        pattern = (r'\b(?:last|this|in|during)\s+(?:the\s+)?fall\b'
                   if name == 'fall' else rf'\b{name}\b')
        if re.search(pattern, text):
            return _scope(*_window(anchor, start, end), name, 'season')
    if re.search(r'\byesterday\b', text):
        return _scope(anchor - timedelta(days=1), anchor - timedelta(days=1),
                      'yesterday', 'relative')
    months_back = re.search(r'\b(\d+)\s+months?\s+ago\b', text)
    if months_back:
        span = int(months_back.group(1)) * 30
        return _scope(anchor - timedelta(days=span), anchor,
                      f'last {span} days', 'relative')
    if re.search(r'\blast\s+week\b', text):
        return _scope(anchor - timedelta(days=10), anchor, 'last week', 'relative')
    if re.search(r'\blast\s+month\b', text):
        return _scope(*_previous_month(anchor), 'last month', 'relative')
    if re.search(r'\blast\s+year\b', text):
        # Unlike پارسال: an English year runs January to January.
        year = anchor.year - 1
        return TimeScope(year * 10000 + 101, year * 10000 + 1231, 'last year',
                         'relative')
    if re.search(r'\b(recently|lately)\b', text):
        return _scope(anchor - timedelta(days=60), anchor, 'recently', 'relative')
    return None


def where_clause(scope: TimeScope | None,
                 fields: tuple[str, ...] = DATE_FIELDS) -> dict | None:
    """The store's half of the filter, in Chroma's operator dialect. One clause
    per date field, OR'd: a card created before the window but updated inside it
    is a card the window is about."""
    if scope is None:
        return None
    clauses = [{'$and': [{field: {'$gte': scope.from_int}},
                         {field: {'$lte': scope.to_int}}]} for field in fields]
    return clauses[0] if len(clauses) == 1 else {'$or': clauses}


def keyword_query(question: str) -> str:
    """Strip the asking, keep the subject, so lexical retrieval scores content
    words rather than 'how' and 'what'."""
    kept = [token for token in textnorm.tokens(question)
            if token not in QUESTION_WORDS]
    return ' '.join(kept) or question


def expand_queries(question: str) -> list[str]:
    """Deterministic multi-query expansion: the question, its keyword form, a
    synonym-substituted variant, and a cross-script variant ("mahsa" also
    searches «مهسا» and back). No model, so it can always be on — and the
    question itself always leads, so nothing is retrieved *instead* of it."""
    variants = [question]
    keywords = keyword_query(question)
    if keywords != question:
        variants.append(keywords)
    swapped: list[str] = []
    for token in textnorm.tokens(question):
        swapped.extend(SYNONYMS.get(token, ()))
    if swapped:
        variants.append(f"{keywords} {' '.join(dict.fromkeys(swapped))}")
    crossed: list[str] = []
    for token in textnorm.tokens(question):
        crossed.extend(translit.variants(token))
    if crossed:
        variants.append(f"{keywords} {' '.join(dict.fromkeys(crossed))}")
    return list(dict.fromkeys(variants))


def multi_query(base: BaseRetriever, llm) -> BaseRetriever:
    """The model-backed alternative to `expand_queries`, taken from LangChain
    rather than reimplemented. It writes the paraphrases a fixed synonym table
    cannot know, at one LLM call per question."""
    return MultiQueryRetriever.from_llm(retriever=base, llm=llm)


# --- retrievers -------------------------------------------------------------

TOP_K = 8           # contexts handed to the answerer
CANDIDATES = 40     # depth taken from each half before fusion
RRF_K = 60          # the constant in 1/(k + rank)
RERANK_DEPTH = 20   # how many candidates the reranker actually reads
# (dense, bm25) fusion weights for chat recall. Deliberately not the card
# index's equal split: recall queries are mostly literals the user remembers
# saying — a password, a name, an amount — where the exact-term half should
# dominate and the dense half only backstops paraphrase.
RECALL_WEIGHTS = (0.2, 0.8)


class RankBM25Retriever(BaseRetriever):
    """Okapi BM25 over Persian-normalised tokens, as a plain `BaseRetriever`.

    BM25 is the only stage that reliably finds a rare literal — a company name,
    «آذر», an amount — which dense retrieval smooths away. It is here rather
    than imported because `langchain_community.BM25Retriever` is itself a thin
    wrapper over the same `rank_bm25`, in a package that announces on import
    that it is no longer maintained. `EnsembleRetriever` cannot tell them apart.
    """
    documents: list[Document] = []
    bm25: Any = None
    k: int = TOP_K
    scope: Any = None   # a TimeScope, or None for no date filter

    @classmethod
    def from_documents(cls, documents, k: int = TOP_K,
                       scope: 'TimeScope | None' = None) -> 'RankBM25Retriever':
        docs = list(documents)
        # BM25Okapi divides by the average document length, so an empty corpus
        # is not a degenerate index — it is a ZeroDivisionError.
        corpus = [textnorm.tokens(doc.page_content) for doc in docs]
        return cls(documents=docs, k=k, scope=scope,
                   bm25=BM25Okapi(corpus) if docs else None)

    @property
    def idf(self) -> dict:
        """Term weights, for the lexical reranker. Same corpus statistics as the
        retrieval it is reranking, so the two cannot disagree about what a rare
        word is."""
        return getattr(self.bm25, 'idf', {}) or {}

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:
        if self.bm25 is None:
            return []
        scores = self.bm25.get_scores(textnorm.tokens(query))
        out: list[Document] = []
        for i in sorted(range(len(scores)), key=lambda i: -scores[i]):
            if scores[i] <= 0:
                break   # no shared term: a zero-score document is not a hit
            doc = self.documents[i]
            if self.scope is not None and not self.scope.matches(doc.metadata):
                continue
            out.append(doc)
            if len(out) >= self.k:
                break
        return out


def hybrid_retriever(dense: BaseRetriever, lexical: BaseRetriever,
                     weights: tuple[float, float] = (0.5, 0.5)) -> EnsembleRetriever:
    """Reciprocal Rank Fusion of the dense and lexical halves.

    Scores never enter the formula — only ranks — so a cosine and a BM25 score
    combine without calibration, and a half returning nonsense degrades the
    result instead of destroying it. Equal weights because the lab measured no
    reason to prefer one: they fail on different questions, not by different
    amounts."""
    return EnsembleRetriever(retrievers=[dense, lexical],
                             weights=list(weights), c=RRF_K)


# --- reranking --------------------------------------------------------------


def _minmax(values: np.ndarray) -> np.ndarray:
    """To [0,1], so coverage and position can be mixed. An all-equal set maps to
    0.5 rather than to 0 or 1, which would invent a ranking out of a tie."""
    if values.size == 0:
        return values
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-9:
        return np.full_like(values, 0.5)
    return (values - low) / (high - low)


def coverage(query: str, text: str, idf: dict) -> float:
    """IDF-weighted term coverage: what share of the question's informative
    words this text actually contains. Bounded to [0,1], so it can be
    thresholded — a raw BM25 score cannot, since its scale depends on the
    corpus."""
    terms = {token for token in textnorm.tokens(query)
             if token not in QUESTION_WORDS}
    if not terms:
        return 0.0
    present = set(textnorm.tokens(text))
    weights = {term: idf.get(term, 1.0) for term in terms}
    total = sum(weights.values()) or 1.0
    return float(sum(w for term, w in weights.items() if term in present) / total)


def lexical_rerank(query: str, documents: list[Document], idf: dict,
                   k: int = TOP_K, depth: int = RERANK_DEPTH) -> list[Document]:
    """Re-order what fusion got roughly right, half on position and half on
    term coverage.

    Position stands in for relevance because `EnsembleRetriever` returns fused
    order and discards the fused score. The lab blended the normalised score
    instead; ranks are a monotone re-expression of it, so recall over the depth
    is untouched and only the ordering inside the cut moves. Documents past
    `depth` are dropped rather than kept below the reranked ones: the reranker
    is the expensive stage, and a candidate it never read has no measured claim
    to a place."""
    candidates = list(documents)[:max(depth, k)]
    if not candidates:
        return []
    n = len(candidates)
    position = np.array([1.0 - i / (n - 1) if n > 1 else 1.0 for i in range(n)],
                        dtype=np.float32)
    scores = np.array([coverage(query, doc.page_content, idf)
                       for doc in candidates], dtype=np.float32)
    final = 0.5 * position + 0.5 * _minmax(scores)
    return [candidates[int(i)] for i in np.argsort(-final)[:k]]


# --- the relevance gate -----------------------------------------------------

# The measured threshold from the lab's candidate F. A context must be graded at
# least this useful to reach the answerer.
GRADE_THRESHOLD = 0.4
# Half marks means "the model expressed no opinion", and it deliberately clears
# the threshold: a reply that could not be parsed must degrade to the ungated
# pipeline — which measured a tie with the gated one — rather than silently
# emptying the context.
NO_OPINION = 0.5
# How long the user waits for the gate before it is abandoned. One constant, not
# the local/remote pair in llm.py, and for the opposite reason: a
# timeout protects the model's right to finish a call the answer depends on,
# while this bounds the wait for a stage that measured no quality gain at all.
# A loaded local model therefore loses its gate instead of costing 90 seconds.
GATE_BUDGET = 20.0
GATE_MAX_CHARS = 700

GATE_PROMPT = (
    'You score how useful each numbered excerpt is for answering a question '
    'about the user\'s own notes and journal. The text may be Persian or '
    'English. Reply with one line per excerpt in the form '
    '"<number>: <score 0-10>" and nothing else. 0 means irrelevant, 10 means it '
    'directly contains the answer.')


def _within_budget(budget_s: float, work):
    """Run `work`, and give up waiting for it after `budget_s`.

    The thread is a daemon and is never joined: an abandoned provider call must
    not hold up process exit, and the model's own timeout is what actually ends
    it. Returns None if the budget was blown or the call raised — both of which
    the caller reads as "no opinion"."""
    box: dict = {}

    def run():
        try:
            box['value'] = work()
        except Exception:
            pass   # a failed gate is a no-op gate; the caller defaults to 0.5

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(budget_s)
    return box.get('value')


def relevance_scores(llm, query: str, texts: list[str],
                     budget_s: float = GATE_BUDGET) -> list[float]:
    """Grade every candidate in **one** call.

    One call per candidate would be more accurate and would also multiply a
    question's cost by k — the difference between a gate that ships and a gate
    that is only ever measured once. Anything the reply does not score keeps
    NO_OPINION."""
    if not texts:
        return []
    listing = '\n\n'.join(f'[{i + 1}] {text[:GATE_MAX_CHARS]}'
                          for i, text in enumerate(texts))
    reply = _within_budget(budget_s, lambda: llm.invoke(
        [('system', GATE_PROMPT),
         ('user', f'Question: {query}\n\n{listing}')]))
    scores = [NO_OPINION] * len(texts)
    for line in str(getattr(reply, 'content', '') or '').splitlines():
        match = re.match(r'\s*\[?(\d+)\]?\s*[:.\-]\s*(\d+(?:\.\d+)?)', line)
        if match:
            index, value = int(match.group(1)) - 1, float(match.group(2))
            if 0 <= index < len(texts):
                scores[index] = min(10.0, value) / 10.0
    return scores


def relevance_gate(llm, query: str, documents: list[Document],
                   threshold: float = GRADE_THRESHOLD,
                   budget_s: float = GATE_BUDGET) -> list[Document]:
    """Candidate F's one addition after retrieval: drop the contexts a model
    says do not help. An empty result is the honest answer — without a gate every
    question gets an answer, including the ones the board cannot support."""
    scores = relevance_scores(llm, query, [doc.page_content for doc in documents],
                              budget_s)
    return [doc for doc, score in zip(documents, scores) if score >= threshold]


GRADERS = ('llm', 'none')


def gate_llm(kind: str, llm):
    """The model the gate should use, or None when the gate is off.

    The seam rule applied to BRAIN_GRADER: a new grader is a branch here, never
    an edited call site, and an unknown value raises at boot rather than
    silently leaving the gate switched off."""
    if kind == 'none':
        return None
    if kind == 'llm':
        return llm
    raise ValueError(f'unknown grader: {kind!r}; expected '
                     f'{" or ".join(repr(k) for k in GRADERS)}')


# --- the board's index ------------------------------------------------------


def rrf_fuse(rankings: list[list[Document]], c: int = RRF_K) -> list[Document]:
    """The same fusion `EnsembleRetriever` applies between retrievers, applied
    across query variants — which it cannot do, because it takes one query."""
    scores: dict[str, float] = {}
    seen: dict[str, Document] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            key = doc.id or doc.page_content
            seen.setdefault(key, doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (c + rank)
    return [seen[key] for key in sorted(scores, key=lambda key: -scores[key])]


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
               llm=None, threshold: float = GRADE_THRESHOLD,
               time_filter: bool = True) -> list[Document]:
        """The chosen architecture, end to end: resolve the time language,
        expand the query, retrieve both ways, fuse, rerank, and — when a model is
        given — gate."""
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
        ranked = lexical_rerank(' '.join(queries), fused, self.bm25.idf, k=k)
        if llm is None:
            return ranked
        return relevance_gate(llm, query, ranked, threshold)


# --- chat memory ------------------------------------------------------------

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
                       'created_day': day_int(row.get('createdAt'))}
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

    def search(self, text: str, k: int = 5,
               scope: TimeScope | None = None) -> list[dict]:
        """Hybrid, BM25-heavy: dense from Chroma, BM25 over the same chunks,
        each fused across the expanded queries (synonyms, cross-script), then
        combined by weighted RRF (RECALL_WEIGHTS). Fused inline rather than
        via `hybrid_retriever` because `EnsembleRetriever` discards the fused
        score, and this result carries one all the way to the UI."""
        total = self.count()
        if total == 0:
            return []   # an empty store is a normal state, not a failed query
        raw = self.store.get()
        corpus = [Document(id=id_, page_content=content, metadata=meta or {})
                  for id_, content, meta in zip(raw['ids'], raw['documents'],
                                                raw['metadatas'])]
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
                    seen.setdefault(doc.page_content, doc)
                    scores[doc.page_content] = (
                        scores.get(doc.page_content, 0.0)
                        + weight / (len(rankings) * (RRF_K + rank)))
        ordered = sorted(scores, key=lambda key: -scores[key])[:k]
        return [{'text': key, 'score': round(scores[key], 4),
                 'metadata': dict(seen[key].metadata)} for key in ordered]

    def count(self) -> int:
        return self.client.get_collection(self.collection_name).count()

    def drop(self) -> None:
        """Delete this collection. For tests cleaning up after themselves —
        never wired to anything a user can reach."""
        self.client.delete_collection(self.collection_name)


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

**3. "Why is BM25 yours? `langchain-community` has a `BM25Retriever`."**

*Short answer.* The ranking function is not ours — `rank_bm25` computes it. What
is ours is the twenty lines that make it a `BaseRetriever` and hand it the
Persian tokeniser, which is exactly what `langchain_community.BM25Retriever`
also is, on top of a package that announces on import that it is unmaintained.

*Why the obvious option fails.* Two things, and the second is the expensive one.
`from langchain_community.retrievers import BM25Retriever` prints a
sunset/no-longer-maintained `DeprecationWarning` — a dependency the project
would be adopting as it is being retired. And its `preprocess_func` defaults to
whitespace splitting, so «می‌خوام» and «می خوام» become two postings that never
meet: a query matches half the cards it should and nothing raises. The parameter
existing at all is the framework saying tokenising is the caller's job — the
same evidence `textnorm`'s own note cites.

*The libraries that would do it.* `langchain-community`'s wrapper, with
`preprocess_func=textnorm.tokens` passed at every construction site — the
smallest diff, if the sunset warning were acceptable. Beyond that the honest
alternatives are not libraries but services: Elasticsearch or OpenSearch (a real
inverted index, incremental updates, an analyzer chain — and a JVM to run),
Tantivy via `tantivy-py` (fast, Rust, no Persian analyzer), or Postgres
full-text search (no Persian dictionary ships with it).

*Why not adopted, and what would change it.* An in-process index is rebuilt from
`/api/state` on every tool call, which is affordable because a personal board is
hundreds of cards. What would change the decision is that number: at the point
where rebuilding costs more than a round trip to a service — call it tens of
thousands of cards, and the way to know is to measure the rebuild, not to guess
— the answer becomes an index that persists and updates incrementally, and
`rank_bm25` cannot do either.

**4. "Why is the time filter yours? `dateparser` exists, and it speaks Farsi."**

*Short answer.* Because a retrieval filter needs a *range* and a date parser
returns a *point*, and because half of what has to be understood here
(«پارسال پاییز», «این چند وقت») is not a date at all.

*Why the obvious option fails.* `dateparser` genuinely handles Jalali dates and
Persian relative expressions, so this is not a coverage argument. It is a shape
argument: «آذر» has to become 2025-11-22 … 2025-12-21, and a parser that returns
one datetime leaves the caller to invent the granularity. Get that wrong and the
filter is a single-day window over a month-long question, which does not error —
it silently returns nothing and reads as "retrieval is broken".

*Why not the framework.* LangChain has `SelfQueryRetriever`, which asks an LLM
to write the metadata filter. That is the framework's answer to this problem and
it is a real one — but it costs a model call per query, it can emit a filter over
fields that do not exist, and a wrong filter deletes evidence before ranking
sees it. A deterministic resolver is testable and free; the seams where the
framework is taken as it comes are listed under question 1.

*The libraries that would do it.* `dateparser` — the pick for absolute dates in
many formats, and worth adopting for that branch alone if a board starts
collecting them. `jdatetime` or `khayyam` — correct Jalali arithmetic, no
language understanding, which would replace the hard-coded month table and
nothing else. Facebook's `duckling` — best-in-class range extraction and it
returns grain, so it solves the actual problem; it is a JVM service, which is
the wrong deployment shape for a local-first single-user app. spaCy `DATE`
entities — no trained Persian pipeline.

*Why not adopted, and what would change it.* The month table drifts by about a
day against a true Jalali conversion across the years a board spans, which is
inside the tolerance of a filter whose windows are 30 days wide — and
`jdatetime` would fix only that day. `duckling` is the one that would genuinely
be better, and what would change the decision is deployment: if the brain ever
runs beside other services anyway, its grain-aware ranges beat this module's
hand-written branches, and the English half — new, unmeasured, written for this
board rather than for a corpus — is the first thing it should replace.

**5. "Why is the reranker yours? LangChain has rerankers."**

*Short answer.* LangChain's rerankers all need a model. This one is free, and
the measured pipeline uses it.

*Why the obvious option fails.* `CrossEncoderReranker` and Cohere's reranker
score a (query, document) pair with a trained model — better, and the lab has
`cross-encoder` as a switchable option precisely so that is measurable. The cost
is a download or an API bill on every query, and for the Persian half of this
corpus the strongest cross-encoders available are English-only, which returns
confident numbers that measure nothing. IDF term coverage is deterministic,
costs nothing, and is bounded to [0,1] so it can also be thresholded.

*Why not the framework.* `ContextualCompressionRetriever` plus a
`DocumentCompressor` is the right *seam* for this, and nothing here prevents
`lexical_rerank` being wrapped as one later; the function is written to be
callable, not to be a class. What the framework does not have is a
deterministic lexical reranker to put inside it.

*Why not adopted, and what would change it.* A measurement: a lab run varying
only the reranker, with a Persian-capable cross-encoder against `lexical`, on the
same 30 questions. If it wins on LLM context precision, the compressor seam is
already the place to put it. Recorded honestly: at candidate F's depth of 20 the
50/50 blend of position and coverage cannot promote a last-placed candidate past
a first-placed one, because min-max normalisation puts both extremes at 0 and 1
on each axis. That is inherited from the lab rather than chosen here, and it is
the reason the reranker moves the middle of the list and not its ends.

**6. "Why is the relevance gate yours? LangChain has document compressors for
exactly this."**

*Short answer.* Because LangChain's LLM filter calls the model once per
document, and it fails closed. This one is a single batched call that fails
open.

*Why the obvious option fails.* `LLMChainFilter` asks the model, per document,
whether it is relevant. At k=8 that is eight requests for one question — the
cost that kept an LLM gate out of the lab's sweep until it could be run against
a local model, and it is per *question*, so it multiplies through every eval. The
second problem is the default direction of failure: its boolean output parser
treats an unparseable answer as "not relevant", so a model that replies in prose
deletes the evidence. Here an unparsed line is NO_OPINION at 0.5, which clears
the 0.4 threshold on purpose — the worst a broken gate can do is nothing, and
"nothing" is the ungated pipeline that measured a tie with this one.

*Why not the framework.* `EmbeddingsFilter` *is* free and is the compressor that
needs no model — but it thresholds embedding similarity, which is more of what
dense retrieval already did. It cannot express the distinction the gate exists
for: on topic, and still not an answer. `ContextualCompressionRetriever` remains
the right seam to hang this on, as noted under question 5.

*The libraries that would do it.* `LLMChainFilter` — the greenfield pick if
per-question cost were not the constraint, and the more accurate of the two by
construction, since each document gets the model's whole attention. Cohere's
rerank API — one call, a relevance score per document, closest in shape to this
function; a paid third-party service that would see the journal. FlashRank — a
local cross-encoder, small and fast, English.

*Why not adopted, and what would change it.* Batching, and the fail-open
default. Two things would change it: a provider where per-document calls are
cheap enough to stop mattering, or — more usefully — a measurement, because the
batched listing's accuracy cost against per-document grading has never been
measured. The lab can run both over the same 30 questions with the same judge;
if per-document wins by more than the decision score's spread, the cost argument
has to be re-made rather than assumed.

**7. "Why is the board's index in memory when chat memory is in Chroma?"**

*Short answer.* Because one is derived and the other is the record. Cards live in
SQLite and this index is rebuilt from `/api/state`; a transcript exists nowhere
but the store.

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
compromise under question 2; another service to run. `sqlite-vec` — genuinely the
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
