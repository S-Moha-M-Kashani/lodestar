"""Embedders behind one protocol. HashEmbedder mirrors the frontend's offline
fallback (deterministic, no downloads) — used in tests/e2e/CI and whenever
fastembed isn't installed. FastEmbedEmbedder is the real semantic model."""
import hashlib
import re
from typing import Protocol

import numpy as np

EMBED_DIM = 128


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


class HashEmbedder:
    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in re.findall(r'[a-z0-9]+', text.lower()):
                digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
                out[i, digest % EMBED_DIM] += 1.0
        return _normalize(out)


class FastEmbedEmbedder:
    def __init__(self, model_name: str = 'BAAI/bge-small-en-v1.5'):
        from fastembed import TextEmbedding  # optional 'semantic' extra
        self.model = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.array(list(self.model.embed(texts)), dtype=np.float32)
        return _normalize(vectors)


def make_embedder(kind: str) -> Embedder:
    if kind == 'hash':
        return HashEmbedder()
    if kind == 'fastembed':
        return FastEmbedEmbedder()
    try:                       # kind == 'auto'
        return FastEmbedEmbedder()
    except Exception:          # fastembed extra not installed → offline fallback
        return HashEmbedder()
