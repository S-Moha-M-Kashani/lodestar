"""Re-ordering what fusion got roughly right, without a model.

IDF-weighted term coverage against the position fusion already assigned. Free,
deterministic, and bounded to [0,1] — which is what lets `coverage` also be
thresholded, where a raw BM25 score cannot be.
"""
import numpy as np
from langchain_core.documents import Document

from .. import textnorm
from .expand import QUESTION_WORDS
from .fusion import RERANK_DEPTH, TOP_K


def _minmax(values: np.ndarray) -> np.ndarray:
    """To [0,1], so coverage and position can be mixed. An all-equal set maps to
    0.5 rather than to 0 or 1, which would invent a ranking out of a tie."""
    if values.size == 0:
        return values
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-9:
        return np.full_like(values, 0.5)
    return (values - low) / (high - low)


# The weight floor for coverage. BM25Okapi assigns a *negative* IDF to a term
# present in most of a small corpus (one card, two chunks), and a shared term
# counting against a match would read "board too small" as "no match".
MIN_TERM_WEIGHT = 0.1


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
    weights = {term: max(idf.get(term, 1.0), MIN_TERM_WEIGHT) for term in terms}
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


"""Alternatives considered

**"Why is the reranker yours? LangChain has rerankers."**

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
"""
