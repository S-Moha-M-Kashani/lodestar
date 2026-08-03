"""The RAG Lab Inspector — a read-only viewer served on :9003.

Three views (ground-truth pairs, chunks-by-session, per-question retrieval
trace) over the same fixtures and pipeline the lab measures with. It builds its
own in-memory index and writes nothing. Composition root: `create_inspector_app`.
"""
from lodestar_brain import textnorm


def _norm(text: str) -> str:
    return ' '.join(textnorm.tokens(text, drop_stopwords=False))


def mark_gold(candidate_texts: list[str],
              evidence_quotes: list[str]) -> list[bool]:
    """Which candidates contain a question's gold evidence quote.

    Substring either direction over the shared normaliser: a chunk may be
    smaller than a quote (part of one message) or larger (several). Normalising
    first means a whitespace or zero-width difference cannot hide a real match —
    the same reason the tokeniser is shared across the whole brain."""
    quotes = [_norm(q) for q in evidence_quotes if q.strip()]
    out = []
    for text in candidate_texts:
        norm = _norm(text)
        out.append(any(q in norm or norm in q for q in quotes))
    return out
