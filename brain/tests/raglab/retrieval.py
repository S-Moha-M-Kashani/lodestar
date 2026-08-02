"""Retrieval, fusion, diversification, reranking and grading.

Every stage is separately switchable because they cover different failures, and
on this corpus the failures are specific:

* **BM25** is the only thing that reliably finds a rare literal — a company
  name, «آذر», an amount. Dense retrieval smooths those away.
* **Dense** is the only thing that finds a paraphrase — the diarist asks about
  «دعوا با همسرم» about a session that says «مهسا باز شکایت کرد».
* **RRF** fuses them without needing their scores to be comparable, which they
  are not (a cosine and a BM25 score share no scale).
* **Time filtering** is what makes "پارسال پاییز" answerable at all: without a
  date pre-filter, a query about last autumn competes with fifty semantically
  identical sessions from every other month.
* **MMR** matters here more than in most corpora, because the same complaint
  recurs verbatim for a year — the top 8 by relevance can be eight
  near-duplicates of one fight.
* **Reranking** fixes the ordering the first stage got roughly right; the
  `agentic` variant adds recency and emotional importance, which is what a diary
  reader actually wants ("what's the *current* state" beats "what matches best").
* **Grading** is the only thing that produces an honest *no*: without a relevance
  gate every question gets an answer, and the ground truth's abstention set is
  scored zero by construction.
"""
import math
import re
from collections import defaultdict

import numpy as np

from lodestar_brain import textnorm
from .llm import lab_chat


class BM25:
    """Okapi BM25 over Persian-normalised tokens. Written here rather than taken
    from a library so the lab keeps the brain's zero-heavy-dependency habit and
    so the tokeniser is exactly the one the rest of the lab measures with."""

    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [textnorm.tokens(d) for d in documents]
        self.lengths = np.array([len(d) or 1 for d in self.docs], dtype=np.float32)
        self.avg_len = float(self.lengths.mean()) if len(self.lengths) else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, tokens in enumerate(self.docs):
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            for token, count in counts.items():
                self.postings[token].append((i, count))
        n = len(self.docs) or 1
        self.idf = {token: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5))
                    for token, p in self.postings.items()}

    def scores(self, query: str, allowed: np.ndarray | None = None) -> np.ndarray:
        out = np.zeros(len(self.docs), dtype=np.float32)
        norm = self.k1 * (1 - self.b + self.b * self.lengths / self.avg_len)
        for token in set(textnorm.tokens(query)):
            posting = self.postings.get(token)
            if not posting:
                continue
            idf = self.idf[token]
            for doc_id, count in posting:
                out[doc_id] += idf * count * (self.k1 + 1) / (count + norm[doc_id])
        if allowed is not None:
            out = np.where(allowed, out, 0.0)
        return out

    def top(self, query: str, k: int,
            allowed: np.ndarray | None = None) -> list[tuple[int, float]]:
        scores = self.scores(query, allowed)
        if not scores.size:
            return []
        order = np.argsort(-scores)[:k]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion. Scores never enter the formula — only ranks — so
    a lexical list and a dense list combine without calibration, and one
    retriever returning nonsense degrades the result instead of destroying it."""
    fused: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            fused[chunk_id] += 1.0 / (k + rank)
    return dict(fused)


def mmr(candidate_vectors: np.ndarray, relevance: np.ndarray, k: int,
        lam: float) -> list[int]:
    """Maximal Marginal Relevance: pick the next chunk that is relevant *and*
    unlike what is already picked. lam=1.0 is plain relevance ordering.

    Relevance carries the query, so no query vector is needed — and a candidate
    set whose vectors could not all be read back degrades to relevance order
    rather than mis-indexing the similarity matrix."""
    if lam >= 1.0 or candidate_vectors.shape[0] != relevance.size:
        return list(np.argsort(-relevance)[:k])
    chosen: list[int] = []
    remaining = set(range(len(relevance)))
    similarity = candidate_vectors @ candidate_vectors.T
    while remaining and len(chosen) < k:
        best, best_score = None, -1e9
        for i in remaining:
            redundancy = max((similarity[i, j] for j in chosen), default=0.0)
            score = lam * relevance[i] - (1 - lam) * redundancy
            if score > best_score:
                best, best_score = i, score
        chosen.append(best)      # type: ignore[arg-type]
        remaining.discard(best)  # type: ignore[arg-type]
    return chosen


def recency_weight(span_to: int, query_date_int: int, half_life_days: float) -> float:
    """Exponential decay on age, the recency term from Generative Agents. Diary
    questions are overwhelmingly about the current state of things, so age is
    information, not noise."""
    days = max(0.0, _days_between(span_to, query_date_int))
    return float(0.5 ** (days / max(1.0, half_life_days)))


def _days_between(a: int, b: int) -> float:
    from datetime import date
    def to_date(value: int) -> date:
        return date(value // 10000, (value // 100) % 100, value % 100)
    try:
        return abs((to_date(b) - to_date(a)).days)
    except ValueError:
        return 0.0


def normalize_scores(values: np.ndarray) -> np.ndarray:
    """Min-max to [0,1]. Needed before mixing relevance with recency and
    importance, which live on their own scales."""
    if values.size == 0:
        return values
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-9:
        return np.ones_like(values) * 0.5
    return (values - low) / (high - low)


class CrossEncoderReranker:
    """fastembed's cross-encoder. Scores the (query, chunk) pair jointly instead
    of comparing two independent embeddings, which is the single biggest quality
    jump available — when a model that speaks the language is installed."""

    def __init__(self, model_name: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self.model = TextCrossEncoder(model_name=model_name)
        self.model_name = model_name

    def score(self, query: str, documents: list[str]) -> np.ndarray:
        return np.array(list(self.model.rerank(query, documents)), dtype=np.float32)


def cross_encoder_available(model_name: str) -> bool:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        return any(m['model'] == model_name
                   for m in TextCrossEncoder.list_supported_models())
    except Exception:
        return False


LLM_GRADE_PROMPT = (
    'You score how useful each numbered excerpt is for answering a question '
    'about the user\'s personal diary. The diary is in Persian. Reply with one '
    'line per excerpt in the form "<number>: <score 0-10>" and nothing else. '
    '0 means irrelevant, 10 means it directly contains the answer.')


class GradeUnavailable(RuntimeError):
    """The grading model could not be reached, so nothing was scored."""


def llm_scores(llm, model: str, query: str, documents: list[str],
               max_chars: int = 700) -> np.ndarray:
    """Batch relevance grading in one call. One call per candidate would be more
    accurate and would also make a 100-question sweep cost hundreds of requests,
    which is the difference between a usable panel and an unusable one."""
    if not documents:
        return np.zeros(0, dtype=np.float32)
    listing = '\n\n'.join(f'[{i + 1}] {d[:max_chars]}'
                          for i, d in enumerate(documents))
    try:
        turn = lab_chat(llm, [{'role': 'system', 'content': LLM_GRADE_PROMPT},
                              {'role': 'user',
                               'content': f'Question: {query}\n\n{listing}'}],
                        model)
        text = turn.content or ''
    except Exception as error:
        # This used to return 0.5 for every document, which clears the gate's
        # default 0.4 threshold: an unreachable model turned grader='llm' into
        # a no-op and no field on the run said so. A lab's whole output is a
        # claim about what a configuration scored, so a row labelled
        # grader='llm' that was measured ungated is the one artefact it must
        # never produce — the reasoning that already makes judged_settings()
        # refuse an unbacked run rather than let the fake provider fill in.
        raise GradeUnavailable(
            f'the LLM grade stage could not reach its model '
            f'({model or "the configured default"}): {error}') from error
    scores = np.full(len(documents), np.nan, dtype=np.float32)
    for line in text.splitlines():
        match = re.match(r'\s*\[?(\d+)\]?\s*[:.\-]\s*(\d+(?:\.\d+)?)', line)
        if match:
            i, value = int(match.group(1)) - 1, float(match.group(2))
            if 0 <= i < len(documents):
                scores[i] = min(10.0, value) / 10.0
    # An unparsed line means "no opinion", not "irrelevant" — defaulting those
    # to zero would let a malformed reply silently empty the context.
    return np.where(np.isnan(scores), 0.5, scores)
