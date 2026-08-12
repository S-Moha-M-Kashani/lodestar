"""The two halves of a search, and the arithmetic that combines them.

BM25 over Persian-normalised tokens is one half, a dense store the other, and
Reciprocal Rank Fusion is what puts them together — between retrievers
(`hybrid_retriever`) and across query variants (`rrf_fuse`), which
`EnsembleRetriever` cannot do because it takes one query.

The pipeline's depth constants live here too: they describe how deep each stage
reads, which is a property of the fusion the stages feed.
"""
from typing import Any

from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from rank_bm25 import BM25Okapi

from .. import textnorm
from .timescope import TimeScope

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


"""Alternatives considered

**"Why is BM25 yours? `langchain-community` has a `BM25Retriever`."**

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
"""
