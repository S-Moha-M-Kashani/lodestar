"""Embedders for the lab, behind the production `Embedder` protocol.

`ascii-hash` is the embedder the brain ships with today. It is here as a
*baseline*, and it is expected to score near zero on this corpus: its tokeniser
is `[a-z0-9]+`, so a Farsi sentence contains no tokens at all and every vector
is the zero vector. That is the whole point of measuring it — the lab should
show, in numbers, that shipping diary memory on the current default would
retrieve nothing.

`token-hash` and `char-hash` are offline, dependency-free, and Unicode-aware, so
CI can measure real retrieval without downloading a model. `fastembed` is the
honest ceiling: a multilingual transformer (Persian is in its training mix,
unlike bge-small-en, which the brain hardwires today).
"""
import hashlib

import numpy as np

from lodestar_brain.rag.embedder import HashEmbedder, _normalize

from . import textnorm

TOKEN_DIM = 512
CHAR_DIM = 1024


def _bucket(token: str, dim: int) -> int:
    return int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16) % dim


class TokenHashEmbedder:
    """Hashed bag of normalised Persian/Latin words with sub-linear term
    frequency. Lexical, but at least it sees the language."""
    dim = TOKEN_DIM
    name = 'token-hash'

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            counts: dict[int, float] = {}
            for token in textnorm.tokens(text):
                slot = _bucket(token, self.dim)
                counts[slot] = counts.get(slot, 0.0) + 1.0
            for slot, count in counts.items():
                out[i, slot] = 1.0 + np.log(count)   # damp repetition
        return _normalize(out)


class CharHashEmbedder:
    """Hashed character 4-grams. Persian inflects by affix (میخواستم /
    نمیخوام / بخوام), which whitespace tokens treat as three unrelated words;
    n-grams recover the shared stem, so this is the strongest offline option."""
    dim = CHAR_DIM
    name = 'char-hash'

    def __init__(self, n: int = 4):
        self.n = n

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for gram in textnorm.char_ngrams(text, self.n):
                out[i, _bucket(gram, self.dim)] += 1.0
        return _normalize(out)


class FastEmbedMultilingual:
    """Real sentence embeddings. Wraps fastembed directly rather than the
    brain's FastEmbedEmbedder default, because that default is bge-small-**en**
    — an English-only model scored against a Farsi corpus."""
    name = 'fastembed'

    def __init__(self, model_name: str, batch_size: int = 64):
        from fastembed import TextEmbedding  # optional 'semantic' extra
        self.model = TextEmbedding(model_name)
        self.model_name = model_name
        self.batch_size = batch_size
        self.dim = len(next(iter(self.model.embed(['probe']))))

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.array(list(self.model.embed(texts, batch_size=self.batch_size)),
                           dtype=np.float32)
        return _normalize(vectors)


def make_embedder(kind: str, settings=None):
    if kind == 'ascii-hash':
        embedder = HashEmbedder()
        embedder.name = 'ascii-hash'          # type: ignore[attr-defined]
        embedder.dim = 128                    # type: ignore[attr-defined]
        return embedder
    if kind == 'token-hash':
        return TokenHashEmbedder()
    if kind == 'char-hash':
        return CharHashEmbedder()
    if kind == 'fastembed':
        model = getattr(settings, 'fastembed_model', None) or (
            'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
        return FastEmbedMultilingual(model)
    raise ValueError(f'unknown lab embedder: {kind!r}')


def fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
    except Exception:
        return False
    return True
