"""The embeddings seam: what a piece of text becomes before anything ranks it.

`make_embeddings` names a backend and an unknown value raises, like every other
seam in the brain. The default is a real Persian encoder, because the embedder
*is* the architecture: on a Farsi diary corpus hash embedding measured ~0.01
recall against 0.617 for `heydariAI/persian-embeddings`, a ~60× effect, while
every other knob measured was worth under 2%. `fake` is the offline-test
value and is deliberately *lexical* — it ranks by shared letters, so an offline
ranking assertion means something. It is never semantic: a paraphrase with no
shared text is invisible to it.

Everything here implements LangChain's own `Embeddings` interface, so the
retrievers assembled on top of it accept these objects without adapters.
"""
import hashlib

import numpy as np
from langchain_core.embeddings import Embeddings

from .. import textnorm

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
    n-grams recover the overlap word tokens miss. Measured at 0.386
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


"""Alternatives considered

**"Why are the embedders yours? LangChain ships a wrapper for all three."**

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
them is a measurable accuracy loss that raises nothing — the worst class of
retrieval failure, because it is silent. `langchain-community`'s
`FastEmbedEmbeddings` has the same gap and additionally prints a
sunset/unmaintained warning on import. Elsewhere in this package the framework
is taken as it comes: `RecursiveCharacterTextSplitter`, `Document`,
`Embeddings`, `EnsembleRetriever`, `MultiQueryRetriever` and `langchain-chroma`.

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
`LexicalHashEmbeddings` and `flatten_metadata` (`chunking.py`) stay ours.
"""
