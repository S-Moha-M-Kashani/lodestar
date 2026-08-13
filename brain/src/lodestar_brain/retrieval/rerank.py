"""The reranker seam: re-ordering what fusion got roughly right.

Two things live here. `lexical_rerank` is the shipped default — IDF-weighted term
coverage blended with the position fusion already assigned. Free, deterministic,
and bounded to [0,1] — which is what lets `coverage` also be thresholded, where a
raw BM25 score cannot be. `make_reranker` is the seam around it: `BRAIN_RERANKER`
names a backend (`lexical` | `openrouter` | `fake`), an unknown value raises at
boot like every other seam in the brain, and a new backend is a branch here
rather than an edited call site.

**The default is `lexical`, not the model** — the opposite of the embedder's
default, and deliberately so. The argument, and the measurement that would change
it, are in the `Alternatives considered` note at the bottom of this file.
"""
import logging
from typing import Protocol

import httpx
import numpy as np
from langchain_core.documents import Document

from .. import textnorm
from .expand import QUESTION_WORDS
from .fusion import RERANK_DEPTH, TOP_K

log = logging.getLogger(__name__)

RERANK_BACKENDS = ('lexical', 'openrouter', 'fake')

# What a backend loads when BRAIN_RERANK_MODEL is empty. Only the model-scored
# backend has a model at all, which is why this is a one-row table rather than a
# constant: a second hosted reranker is a row here, while 'lexical' and 'fake'
# resolve to '' because naming a model for them would make the configuration
# describe something that never runs.
RERANK_MODEL_DEFAULTS = {'openrouter': 'cohere/rerank-4-fast'}

# How long a hosted reranker may keep a search waiting. Smaller than the gate's
# GATE_BUDGET of 20s on purpose: the gate reads whole excerpts with a chat model,
# while this is one small request scoring twenty short cards. Blowing it costs
# the re-ordering and never the search — see the fail-open note on
# OpenRouterReranker.
RERANK_BUDGET = 10.0

# The n in the offline fake's character n-grams — the same 4 that
# LexicalHashEmbeddings uses, and for the same reason: Persian inflects by affix,
# so n-grams recover overlap a whitespace tokeniser never sees.
FAKE_NGRAM = 4


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


def _head(documents: list[Document], k: int, depth: int) -> list[Document]:
    """The slice a reranker reads: the first `depth` candidates, or `k` when that
    is deeper. One definition, so three backends cannot come to disagree about
    how far down the expensive stage looks. Why the rest are dropped rather than
    kept below the reranked ones is argued in `lexical_rerank`."""
    return list(documents)[:max(depth, k)]


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
    order and discards the fused score. Blending the normalised score was the
    alternative; ranks are a monotone re-expression of it, so recall over the
    depth is untouched and only the ordering inside the cut moves. Documents past
    `depth` are dropped rather than kept below the reranked ones: the reranker
    is the expensive stage, and a candidate it never read has no measured claim
    to a place."""
    candidates = _head(documents, k, depth)
    if not candidates:
        return []
    n = len(candidates)
    position = np.array([1.0 - i / (n - 1) if n > 1 else 1.0 for i in range(n)],
                        dtype=np.float32)
    scores = np.array([coverage(query, doc.page_content, idf)
                       for doc in candidates], dtype=np.float32)
    final = 0.5 * position + 0.5 * _minmax(scores)
    return [candidates[int(i)] for i in np.argsort(-final)[:k]]


class Reranker(Protocol):
    """What every backend is: fused candidates in, the k best out.

    `lexical_rerank` already has exactly this signature, so the shipped backend
    *is* the protocol rather than an adapter around it — which is what keeps the
    older claim in the note below true: the function is written to be callable,
    not to be a class.

    `idf` is on the interface rather than inside the lexical backend because the
    caller is the only one that has it: `CardIndex` reranks with the corpus
    statistics of the very retrieval being reranked, so BM25 and the reranker can
    never disagree about what a rare word is. A model-scored backend ignores it,
    and the offline suite asserts that by passing `{}`.
    """

    def __call__(self, query: str, documents: list[Document], idf: dict,
                 k: int = TOP_K,
                 depth: int = RERANK_DEPTH) -> list[Document]: ...


class FakeReranker:
    """The offline backend: a stand-in for a model that scores a (query,
    document) pair, without a model.

    Deliberately neither a pass-through nor a constant. It scores by shared
    character 4-grams and re-sorts on that alone, so an offline test can assert
    an ordering the reranker actually produced — the same reason
    `LexicalHashEmbeddings` is lexical rather than a seeded PRNG. It sees
    letters, never meaning: a paraphrase with no shared text is invisible to it.

    What makes it a useful stand-in for a *cross-encoder* rather than a second
    copy of `lexical_rerank` is that it ignores the order it was handed. A
    candidate that arrived last can finish first, which is the behaviour the
    hosted backend has and which `lexical_rerank` provably does not at the
    shipped depth — see the last paragraph of the note below. That asymmetry is
    what the offline test asserts.
    """

    def score(self, query: str, text: str) -> float:
        """Share of the query's character n-grams this text contains, in [0,1]."""
        grams = set(textnorm.char_ngrams(query, FAKE_NGRAM))
        if not grams:
            return 0.0
        return len(grams & set(textnorm.char_ngrams(text, FAKE_NGRAM))) / len(grams)

    def __call__(self, query: str, documents: list[Document], idf: dict,
                 k: int = TOP_K, depth: int = RERANK_DEPTH) -> list[Document]:
        candidates = _head(documents, k, depth)
        scored = [(self.score(query, doc.page_content), i)
                  for i, doc in enumerate(candidates)]
        # Descending by score, and by arrival order inside a tie: a fake whose
        # output depends on Python's sort stability is a fake whose test
        # sometimes passes.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [candidates[i] for _, i in scored[:k]]


class OpenRouterReranker:
    """The model-scored backend: OpenRouter's `/rerank`, a cross-encoder scoring
    every (query, document) pair.

    **No new dependency.** OpenRouter serves reranking beside chat completions on
    the same host, with the same key, so this is one `httpx.post` to a base url
    the brain already carries — where `cohere` or `langchain-cohere` would have
    been a package, a second credential and a second account. `llm.py` makes the
    same call for the opposite reason (Ollama's OpenAI-compatible /v1 is why a
    local model costs no dependency either); this is that argument applied to a
    different endpoint on the same host.

    One request for the whole candidate list, like the gate's one batched call
    and priced the same way — per search, not per document.

    **Fail open, and open means *nothing*.** A reranker that cannot be reached
    returns the fused order untouched, logged: the search still answers, just
    without the re-ordering it paid for. Deliberately not a fallback to
    `lexical_rerank` — a configuration naming one backend must never be quietly
    served by another, which is the whole point of there being no `auto` mode.
    The distinction is worth stating precisely: an unknown *configuration* raises
    at boot, and an unreachable *service* degrades to the identity.
    """

    def __init__(self, model: str, api_key: str,
                 base_url: str = 'https://openrouter.ai/api/v1',
                 timeout: float = RERANK_BUDGET):
        self.model = model
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._api_key = api_key

    def __call__(self, query: str, documents: list[Document], idf: dict,
                 k: int = TOP_K, depth: int = RERANK_DEPTH) -> list[Document]:
        candidates = _head(documents, k, depth)
        if not candidates:
            return []
        try:
            res = httpx.post(
                f'{self.base_url}/rerank',
                headers={'Authorization': f'Bearer {self._api_key}'},
                json={'model': self.model, 'query': query,
                      'documents': [doc.page_content for doc in candidates],
                      'top_n': min(k, len(candidates))},
                timeout=self.timeout)
            res.raise_for_status()
            results = res.json().get('results') or []
        except Exception as exc:
            log.warning('reranker %s unreachable, keeping the fused order: %s',
                        self.model, exc)
            return candidates[:k]
        # Sorted here rather than trusted from the wire. The API documents its
        # results as ranked, but the order this pipeline hands to the answerer is
        # ours to guarantee, and `index` is what maps a score back to a document
        # — a result carrying no usable index is dropped rather than guessed at.
        scored = [(float(row['relevance_score']), int(row['index']))
                  for row in results
                  if isinstance(row, dict) and 'relevance_score' in row
                  and isinstance(row.get('index'), int)
                  and 0 <= row['index'] < len(candidates)]
        if not scored:
            log.warning('reranker %s scored nothing usable, keeping the fused '
                        'order', self.model)
            return candidates[:k]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [candidates[i] for _, i in scored[:k]]


def resolve_rerank_model(kind: str, settings=None, model: str = '') -> str:
    """The model a backend will actually score with: what was pinned, else that
    backend's own default. One place, like `resolve_embed_model`, so the
    configuration and the reranker it describes cannot name different models."""
    if kind not in RERANK_MODEL_DEFAULTS:
        return ''
    return (model or getattr(settings, 'rerank_model', '')
            or RERANK_MODEL_DEFAULTS[kind])


def make_reranker(kind: str, settings=None, model: str = '') -> Reranker:
    """The reranker seam. A new backend is a new branch here, never an edited
    call site, and an unknown value raises at boot.

    The hosted backend refuses to build without a key, the way `make_url_safety`
    does and unlike `make_chat_model`, because the two fail differently: a
    keyless chat model returns a 401 the user reads, while a keyless reranker
    would fail open on every question and silently serve the un-reranked
    pipeline under a configuration claiming a cross-encoder. Silent is the one
    thing a retrieval stage must not be.
    """
    if kind == 'lexical':
        return lexical_rerank
    if kind == 'fake':
        return FakeReranker()
    if kind == 'openrouter':
        if not getattr(settings, 'openrouter_api_key', ''):
            raise ValueError(
                'BRAIN_RERANKER=openrouter needs OPENROUTER_API_KEY; set it, or '
                'choose BRAIN_RERANKER=lexical to rerank without a model')
        return OpenRouterReranker(
            resolve_rerank_model('openrouter', settings, model),
            settings.openrouter_api_key,
            base_url=getattr(settings, 'openrouter_base_url',
                             'https://openrouter.ai/api/v1'))
    raise ValueError(f'unknown reranker: {kind!r}; expected '
                     f'{", ".join(RERANK_BACKENDS)}')


"""Alternatives considered

**"Why is the reranker yours? LangChain has rerankers."**

*Short answer.* It is now a seam with three backends, and only the *default* is
ours. `BRAIN_RERANKER=openrouter` puts a real cross-encoder in the same slot in
one line of config. What stays ours is `lexical_rerank`, because LangChain's
rerankers all need a model and the measured pipeline uses one that does not.

*Why the obvious option fails.* `CrossEncoderReranker` and Cohere's reranker
score a (query, document) pair with a trained model — better in principle, and
still unmeasured against this one on this corpus. The cost is a download or an
API bill on every query, and for the Persian half of this corpus the strongest
*local* cross-encoders available are English-only, which returns confident
numbers that measure nothing. That last objection is what the hosted backend
answers: Cohere's rerank-4 family is multilingual across 100+ languages, so the
comparison can finally be run on the corpus this board actually holds. IDF term
coverage remains deterministic, costs nothing, and is bounded to [0,1] so it can
also be thresholded.

*Why not the framework.* `ContextualCompressionRetriever` plus a
`DocumentCompressor` is still the right *framework* seam for this, and nothing
here prevents any of these three being wrapped as one; `Reranker` above is that
shape minus the class, since `lexical_rerank` already had the signature and a
protocol costs nothing to satisfy. What the framework does not have is a
deterministic lexical reranker to put inside it — nor an offline fake that
reorders, which is what lets the seam be tested with no network at all.
`langchain-cohere` would serve the hosted backend and was not taken: OpenRouter
already carries the key, the base url and the billing for this project, and
serves reranking on the same host as chat, so the whole backend is one
`httpx.post` where the package would be a dependency, a second credential and a
second account.

*The libraries that would do it.* `langchain-cohere`'s `CohereRerank` — first
party, the greenfield pick if the project were not already standardised on one
gateway. `FlashRank` — a local ONNX cross-encoder, small and fast; its strong
models are English, which is the objection above. `sentence-transformers`'
`CrossEncoder` behind `CrossEncoderReranker` — free after a download, and
multilingual only if you can find a multilingual checkpoint worth running.
`rerankers` (the unified wrapper) — one interface over all of the above, which
is precisely what `make_reranker` is, so it would replace a seam rather than
fill one.

*Why the default is `lexical` and not the model.* The embedder defaults to the
expensive option because that choice was *measured* — a ~60× recall effect. This
one has not been measured at all, and until it has, three properties of the
default carry the argument: the shipped pipeline's numbers (precision@k, the
rare-literal eval) were all taken with `lexical`; a hosted reranker bills a
search on every question, including the `/rag/recall` search box, which is built
to wait for nothing and cost nothing; and it sends the card text of a private
board to a third party. A default that quietly bills an API and exports a diary
is the exact footgun the retired `auto` modes were removed for. So the roadmap's
`cohere/rerank-4-fast` ships as the *hosted backend's own default model*
(`RERANK_MODEL_DEFAULTS`) rather than as the default backend, and choosing it is
one env var.

*What would change it, and how to run that.* A run varying only the reranker
over the same fixture, scored on precision@k and on where the rare literal
lands:

    # both halves offline except the reranker under test
    export BRAIN_EVAL_LIVE=1 OPENROUTER_API_KEY=sk-...
    uv run --project brain pytest brain/tests/evals/test_rag_quality.py -v

  with `CardIndex(LexicalHashEmbeddings())` in that file built instead as
  `CardIndex(LexicalHashEmbeddings(), rerank=make_reranker('openrouter',
  load_settings()))` — the constructor argument exists for this — and the run
  repeated with `rerank=lexical_rerank`. Two numbers decide it: precision@k
  across the fixture queries, and `rank_of(expected_id)` in
  `test_lexical_scoring_beats_dense_alone_on_a_rare_literal`. **If the hosted
  reranker wins precision@k by more than the fixture's spread *and* does not
  push the rare literal down, the default moves** — and the number goes here,
  in this file, not only into a commit message. If it wins on precision and
  loses the rare literal, the honest reading is that coverage and a
  cross-encoder measure different things and the answer is to blend them, not
  to swap them. Note what the measurement will *not* tell you: a hosted rerank
  is billed per search and is invisible to `pricing.py`, which prices tokens
  from a turn's usage, so the cost side of that decision has to be read off the
  provider's dashboard.

Recorded honestly, and unchanged by any of this: at the shipped depth of 20 the
50/50 blend of position and coverage cannot promote a last-placed candidate past
a first-placed one, because min-max normalisation puts both extremes at 0 and 1
on each axis. That is inherited from the measured configuration rather than
chosen here, it is the reason the reranker moves the middle of the list and not
its ends, and it is the one behaviour a cross-encoder would change outright —
which is also why `FakeReranker` is written to do exactly that offline.
"""
