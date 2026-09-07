"""The Overview map's arithmetic: what a card reads as, and how a set of
vectors becomes a set of dots.

All of this ran in the browser until 2026-09-07 — a CDN-loaded MiniLM embedding
every card serially on every visit, and a 350-iteration t-SNE on the paint
thread. Nothing kept the answer, so the cost was paid again every time the page
opened and it grew with the board. The vectors are now stored beside the cards
(`card_vectors` in board.db) and the layout is computed here.

Three things live in this module because they are one contract:

- `map_card_text` — the text a card is embedded as, mirroring `cardText` in
  js/lib/projection.js.
- `card_fingerprint` / `text_hash` — the digest that says whether a stored
  vector is still the right one, mirroring `textHash` in the same file.
- `project` — PCA or t-SNE down to the plotting fractions
  `normalizePoints` already produces, so the browser's plotting code is
  unchanged.

**The t-SNE is the browser's algorithm, and it is NOT the browser's
coordinates.** Measured on the day it was written, against the JS run on the
same vectors and the same PCA initialisation: the two agree to 1.4e-14 after one
iteration, 3.8e-11 after five, 1.8e-8 after twenty — and have visibly parted by
one hundred. That is not a porting bug and no port can avoid it. The gains
heuristic branches on `(gradient > 0) == (previous step > 0)`, a *discrete* test
on a sign, so a difference of 1e-15 in a coordinate near zero sends the two runs
down permanently different paths through a non-convex descent. Every value that
decides the layout matches exactly — the affinity matrix P agrees to 2.6e-15,
the seeded generator agrees bit for bit — and the maps have the same character
because they have the same perplexity rule, the same 350 iterations, the same
exaggeration schedule and the same PCA start. Individual dots are not in the
same place they were before 2026-09-07, once. The alternative was keeping two
implementations of one algorithm.

The first two are mirrored rather than shared, which is the same call
`server.js` makes for `js/core/schedule.js`'s validator and for the same reason:
the two ends have to agree and neither can import the other.
`tests/test_projection.py` pins them against values produced by the JS itself,
so a drift fails a test instead of silently re-embedding a whole board — or
silently never re-embedding an edited card.

numpy only. It is already a base dependency of this package, not an extra, so
none of this changes what `uv run --project brain pytest brain/tests` has to
install: the brain suite stays offline with no extras.
"""
from __future__ import annotations

import hashlib

import numpy as np

from .cards import embedder_identity

# The plotting window normalizePoints maps into: [0.06, 0.94] of the field,
# centred so the data mean sits on the middle crosshair. Kept as the half-width
# it is written as in the JS rather than as the two bounds, because the JS
# arithmetic is `0.5 ± SPAN * …` and two constants would be one fact twice.
SPAN = 0.44
# A ring for a set with no spread at all — every card reading identically is a
# real board state, and stacking them into one dot would lose them.
RING = 0.32
# t-SNE needs neighbours to have an opinion about. Below this the perplexity
# search has nothing to search and PCA stands in, which is the browser's own
# rule (`cards.length > 3`) carried over unchanged.
TSNE_MIN = 4
# The gradient run, from the implementation this replaces. Unchanged so the map
# people are used to is the map they get.
TSNE_ITERATIONS = 350
TSNE_EXAGGERATE_UNTIL = 100
TSNE_LEARNING_RATE = 150.0
TSNE_SEED = 0x10DE57A2


def vector_model_id(embeddings) -> str:
    """The name the stored vectors are filed under — the `model` half of
    `card_vectors`' primary key.

    It has to change exactly when the vector space changes, and `CardIndex`
    already answers that question for its own namespace, so this reuses
    `embedder_identity` rather than inventing a second answer: the class, the
    checkpoint and the prefixes, none of them read in a way that would load a
    2.2 GB model to ask.

    Digested rather than stored whole, with the readable prefix kept, so a row
    says at a glance which embedder wrote it and two embedders can never be
    mixed inside one answer. The prefix is the class name because that is the
    part a person reads; the digest is what actually decides.
    """
    identity = embedder_identity(embeddings)
    short = hashlib.blake2b(identity.encode(), digest_size=6).hexdigest()
    return f'{type(embeddings).__name__}:{short}'


# --- What a card reads as -------------------------------------------------

def map_card_text(card: dict, labels: dict[str, str]) -> str:
    """A card as the map reads it: its own words plus the labels it is filed
    under.

    Mirrors `cardText` in js/lib/projection.js exactly, field order included.
    The **labels lead** because the sentence is truncated from the tail by every
    embedder with a token limit: a long note must never be able to push a card's
    category out of its own vector. And it is the category's *label* ("Music"),
    never its id — an embedding model can read the word the user chose, and it
    is what makes renaming a category invalidate that card's vector, which is
    correct: a card in a different life area is not the same thought.

    `labels` comes from the board's own category registry, which arrives with
    the cards on `GET /api/state`. An id the registry does not hold contributes
    nothing, the answer `catLabel` gives and the answer `cleanCard` gives the
    field itself.
    """
    parts = [
        ' '.join(card.get('tags') or []),
        labels.get(card.get('category') or '', ''),
        card.get('type') or '',
        card.get('title') or '',
        card.get('notes') or '',
    ]
    return ' '.join(part for part in parts if part).strip()


def text_hash(text: str) -> str:
    """FNV-1a, base36 — `textHash` in js/lib/projection.js.

    Over UTF-16 code units, not bytes, because that is what `charCodeAt` gives
    the JS. The distinction is invisible on ASCII and total on everything else,
    so a byte-wise mirror would agree with the browser about English cards and
    disagree about every Persian one, passing any test written in English.

    Astral characters (an emoji in a title) are two code units in both
    languages, which is why the surrogate pair is iterated rather than the code
    point.
    """
    h = 2166136261
    for code in _utf16_units(text):
        h ^= code
        h = (h * 16777619) & 0xFFFFFFFF
    return _base36(h)


def _utf16_units(text: str):
    """The UTF-16 code units of `text`, which is what a JS string is made of.
    A character outside the Basic Multilingual Plane yields its two surrogates,
    exactly as `charCodeAt` would."""
    raw = text.encode('utf-16-le')
    return (raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2))


def _base36(n: int) -> str:
    """`Number.prototype.toString(36)` for a non-negative integer."""
    if n == 0:
        return '0'
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return ''.join(reversed(out))


def card_fingerprint(card: dict, labels: dict[str, str]) -> str:
    """The digest that decides whether a stored vector is still this card's.

    One step, so no caller can compose the text and the hash differently — which
    is the whole point of "one place": the browser and the brain each ask
    "is this vector stale" and a disagreement means either endless re-embedding
    or an edited card keeping a vector of what it used to say.
    """
    return text_hash(map_card_text(card, labels))


# --- Down to two dimensions ----------------------------------------------

def _normalize(ids: list[str], pts: np.ndarray) -> dict[str, tuple[float, float]]:
    """Raw 2-D points to the plotting fractions the browser already plots.

    `normalizePoints` in js/lib/projection.js, unchanged: centred on the data
    mean so the middle crosshair means something, scaled per axis by the largest
    deviation, and PC-2 inverted so it grows upward on a screen whose y axis
    does not. A set with no spread at all falls back to a ring rather than
    stacking every card on one dot.
    """
    n = len(ids)
    mean = pts.mean(axis=0)
    centred = pts - mean
    reach = np.abs(centred).max(axis=0)
    if float(reach.max()) < 1e-9:
        angles = np.arange(n) / n * 2 * np.pi
        return {
            card_id: (0.5 + RING * float(np.cos(a)), 0.5 + RING * float(np.sin(a)))
            for card_id, a in zip(ids, angles)
        }
    # A zero on one axis alone is legitimate — a set that varies in PC-1 and not
    # at all in PC-2 — and dividing by it would put every dot at NaN.
    scale = np.where(reach > 0, reach, 1.0)
    frac = centred / scale
    return {
        card_id: (0.5 + SPAN * float(frac[i, 0]), 0.5 - SPAN * float(frac[i, 1]))
        for i, card_id in enumerate(ids)
    }


def _pca2(vecs: np.ndarray) -> np.ndarray:
    """The first two principal components, by SVD of the centred matrix.

    Three lines against the sixty of hand-rolled power iteration this replaces,
    and `pca2`'s deterministic seeding stops being something to maintain: an SVD
    has no starting vector to seed. `full_matrices=False` because only the first
    two right-singular vectors are ever read.
    """
    centred = vecs - vecs.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    # A set with fewer dimensions than 2, or fewer rows, gives back fewer
    # components than asked for; the missing axis is a column of zeros, which
    # _normalize then reads as "no spread on this axis" rather than as an error.
    comps = vt[:2]
    if comps.shape[0] < 2:
        comps = np.vstack([comps, np.zeros((2 - comps.shape[0], vecs.shape[1]))])
    return centred @ comps.T


def _mulberry32(seed: int):
    """The browser's own generator, so a layout computed here and one computed
    there start from the same noise. Retained rather than replaced by
    `np.random.default_rng` for exactly that reason — the offline path in the
    browser still seeds this way, and two generators would be two maps."""
    a = seed & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = (t ^ (t >> 15)) * (1 | a) & 0xFFFFFFFF
        t = (t + ((t ^ (t >> 7)) * (61 | t) & 0xFFFFFFFF)) & 0xFFFFFFFF ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return nxt


def _tsne(vecs: np.ndarray, init: np.ndarray, iterations: int) -> np.ndarray:
    """Exact t-SNE, vectorized — the same algorithm the browser ran, off the
    paint thread and in whole-matrix numpy rather than in three nested loops.

    Initialised from the PCA layout, scaled right down with a seeded whisper of
    noise to break ties, so t-SNE *refines* the map instead of reshuffling it.
    That is what makes returning to a filter give the reader their dots back.
    """
    n = len(vecs)
    # Floored, exactly as the browser floors it. Not a rounding detail: at
    # twelve cards the browser's perplexity is 3 and an unfloored 3.67 gives a
    # visibly different map, which is the whole thing this port is not allowed
    # to change.
    perplexity = float(max(1, min(15, (n - 1) // 3)))
    # Squared euclidean distances, the (a-b)² = a² - 2ab + b² identity so the
    # whole matrix is three BLAS calls instead of n² Python iterations.
    sq = (vecs ** 2).sum(axis=1)
    D = np.maximum(sq[:, None] - 2 * (vecs @ vecs.T) + sq[None, :], 0.0)
    np.fill_diagonal(D, 0.0)

    # Per-point gaussian bandwidth matched to the target perplexity, by the same
    # 50-pass bisection. Vectorized across points: every row searches at once,
    # and a row that has converged simply stops moving.
    log_u = np.log(perplexity)
    beta = np.ones(n)
    lo = np.full(n, -np.inf)
    hi = np.full(n, np.inf)
    eye = np.eye(n, dtype=bool)
    # A row that has hit the tolerance stops moving, which is what the browser's
    # per-point `break` does. Vectorizing the search across rows without this
    # would keep bisecting a converged row's bandwidth for the remaining passes
    # and land somewhere the browser never goes — the same algorithm producing a
    # different map, silently.
    done = np.zeros(n, dtype=bool)
    P = np.zeros((n, n))
    for _ in range(50):
        fresh = np.exp(-D * beta[:, None])
        fresh[eye] = 0.0
        # Only the unconverged rows are replaced, so a converged row keeps the
        # affinities its final bandwidth produced.
        P = np.where(done[:, None], P, fresh)
        total = np.maximum(P.sum(axis=1), 1e-12)
        Pn = P / total[:, None]
        with np.errstate(divide='ignore', invalid='ignore'):
            logs = np.where(Pn > 0, np.log(Pn), 0.0)
        H = -(Pn * logs).sum(axis=1)
        diff = H - log_u
        done = done | (np.abs(diff) < 1e-5)
        if np.all(done):
            break
        up = (diff > 0) & ~done
        keep = done
        lo = np.where(up, beta, lo)
        hi = np.where(~up & ~keep, beta, hi)
        stepped = np.where(
            up,
            np.where(np.isinf(hi), beta * 2, (beta + hi) / 2),
            np.where(np.isinf(lo), beta / 2, (beta + lo) / 2),
        )
        beta = np.where(keep, beta, stepped)
    total = np.maximum(P.sum(axis=1), 1e-12)
    P = P / total[:, None]
    # Symmetrised and floored, so no pair carries a zero affinity into a log.
    P = np.maximum((P + P.T) / (2 * n), 1e-12)
    np.fill_diagonal(P, 0.0)

    rand = _mulberry32(TSNE_SEED ^ n)
    spread = float(np.abs(init).max()) if n else 0.0
    scale = 1e-2 / spread if spread > 0 else 1e-2
    noise = np.array([[rand() - 0.5, rand() - 0.5] for _ in range(n)]) * 1e-4
    Y = init * scale + noise
    dY = np.zeros((n, 2))
    gains = np.ones((n, 2))

    for it in range(iterations):
        exag = 12.0 if it < TSNE_EXAGGERATE_UNTIL else 1.0
        momentum = 0.5 if it < TSNE_EXAGGERATE_UNTIL else 0.8
        ysq = (Y ** 2).sum(axis=1)
        num = 1.0 / (1.0 + np.maximum(ysq[:, None] - 2 * (Y @ Y.T) + ysq[None, :], 0.0))
        np.fill_diagonal(num, 0.0)
        qsum = max(num.sum(), 1e-12)
        mult = (exag * P - np.maximum(num / qsum, 1e-12)) * num
        np.fill_diagonal(mult, 0.0)
        # The gradient, as one matrix product: 4·Σⱼ multᵢⱼ (yᵢ - yⱼ).
        grad = 4 * (mult.sum(axis=1)[:, None] * Y - mult @ Y)
        same = (grad > 0) == (dY > 0)
        gains = np.maximum(0.01, np.where(same, gains * 0.8, gains + 0.2))
        dY = momentum * dY - TSNE_LEARNING_RATE * gains * grad
        Y = Y + dY
        Y = Y - Y.mean(axis=0)
    return Y


def project(ids: list[str], vecs, kind: str = 'pca',
            iterations: int = TSNE_ITERATIONS) -> dict[str, tuple[float, float]]:
    """One set of cards as one set of dots, keyed by card id.

    `kind` is `'pca'` or `'tsne'`; anything else raises, the seam rule this
    repo applies everywhere — there is no `auto` and no silent fallback to a
    layout the reader did not ask for.

    Degenerate sets are *placed*, never refused: an empty set is an empty map,
    one card is a centred dot, and a set too small for t-SNE gets the PCA layout
    rather than a perplexity search with nothing to search. Those are real board
    states and the spec says each of them renders.
    """
    if kind not in ('pca', 'tsne'):
        raise ValueError(f'unknown projection: {kind!r}; expected pca or tsne')
    if not ids:
        return {}
    vecs = np.asarray(vecs, dtype=np.float64)
    if len(ids) == 1:
        return {ids[0]: (0.5, 0.5)}
    # Sorted by card id before anything is computed, which is what makes "the
    # same set is the same map, whichever order it arrives in" a property rather
    # than a hope. It is not cosmetic: an SVD's singular vectors are only
    # determined up to sign, so the *same* matrix with its rows permuted comes
    # back mirrored, and t-SNE's tie-breaking noise is assigned by row index. A
    # canonical order removes both. It is also the order the layout cache keys
    # on, so the cache cannot claim a hit for a layout it would not reproduce.
    order = sorted(range(len(ids)), key=lambda i: ids[i])
    ids = [ids[i] for i in order]
    vecs = vecs[order]
    pca = _pca2(vecs)
    if kind == 'tsne' and len(ids) >= TSNE_MIN:
        return _normalize(ids, _tsne(vecs, pca, iterations))
    return _normalize(ids, pca)


# --- The layout cache ------------------------------------------------------

# Small on purpose. A layout is cheap once the vectors exist — that is the whole
# reason vectors are persisted and layouts are not — so this exists to make
# "switch a filter back and forth" free, not to be a store. Eight is the size
# the browser's own tsneCache used, kept for the same reason: a reader flips
# between a handful of filters, not fifty.
LAYOUT_CACHE_MAX = 8


def layout_key(pairs: list[tuple[str, str]], kind: str, model: str) -> str:
    """The digest a layout is cached under: the **sorted** (card id,
    fingerprint) pairs, plus the projection asked for and the model behind it.

    Sorted, so the same cards arriving in a different order are the same key —
    which is what makes "the same set is the same map" a property of the cache
    as well as of the arithmetic, and `project` sorts its rows for the same
    reason. Fingerprints and not just ids, so editing one visible card's words
    invalidates the sets that hold it and leaves every other set cached.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f'{kind}\x00{model}\x00'.encode())
    for card_id, fingerprint in sorted(pairs):
        digest.update(f'{card_id}\x00{fingerprint}\x00'.encode())
    return digest.hexdigest()


class LayoutCache:
    """Insertion-ordered, oldest evicted — the same rule the browser's cache
    used. A dict rather than `functools.lru_cache` because the key is built from
    a list of pairs, which is not hashable, and hashing it into an argument
    would put the digest above in two places."""

    def __init__(self, maxsize: int = LAYOUT_CACHE_MAX):
        self.maxsize = maxsize
        self._store: dict[str, dict[str, tuple[float, float]]] = {}

    def get(self, key: str):
        return self._store.get(key)

    def put(self, key: str, coords: dict[str, tuple[float, float]]) -> None:
        self._store[key] = coords
        while len(self._store) > self.maxsize:
            self._store.pop(next(iter(self._store)))


__all__ = ['card_fingerprint', 'layout_key', 'LayoutCache', 'map_card_text',
           'project', 'text_hash', 'vector_model_id']


"""Alternatives considered — why is this t-SNE ours?

**Why did you write your own t-SNE?** Because the repo already had one, running
in the browser, and its output is the map people have been reading for weeks.
This is that implementation moved into numpy: same perplexity search, same 350
iterations, same early exaggeration, same seed, same PCA initialisation. Nothing
about the *algorithm* is ours by preference — the choice being defended is
porting rather than substituting.

**Why the obvious option fails.** The obvious option is
`sklearn.manifold.TSNE`. The tempting argument against it — "it would move
everybody's dots" — is one this module is **not** entitled to make, and finding
that out is the most useful thing in this note: the port moves them too (see the
measurement in the module docstring). What survives is the weaker and truer
version. scikit-learn's t-SNE since 0.22 defaults to Barnes-Hut with its own
learning-rate schedule (`learning_rate='auto'` = max(N/early_exaggeration/4,
50)) and a different exaggeration regime, so it changes the *character* of the
map — how tight a cluster gets, how far apart two of them sit — and not merely
the coordinates, which is a change worth measuring rather than absorbing. And
it would add scikit-learn *and* scipy to a package whose only numeric dependency
is numpy, roughly 100 MB, to replace sixty lines that are already calibrated
against a corpus this project has been reading for weeks.

**Why not the framework.** LangChain has nothing here, and that is not an
oversight on its part: dimensionality reduction for a *visualisation* is not
retrieval. The framework is used in this codebase wherever it has an answer —
`create_agent`, `RecursiveCharacterTextSplitter`, `EnsembleRetriever`,
`SummarizationMiddleware`, `ClearToolUsesEdit`, `ToolErrorMiddleware`, the
`Embeddings` and `BaseChatModel` types the seams return. This is simply not one
of those seams.

**The libraries that would do it.**

- `scikit-learn`'s `TSNE` — the default answer, and what I would reach for on a
  greenfield project. Barnes-Hut, well tested, `random_state` for
  reproducibility. Brings scipy.
- `openTSNE` — faster and better maintained as a dedicated implementation
  (FIt-SNE interpolation, proper `transform` for new points, which is the one
  capability nothing here has). Also brings scipy.
- `umap-learn` — arguably the better *algorithm* for this job: it preserves
  more global structure, so distance between two clusters means something,
  which is exactly what a reader of this map assumes it means. Brings numba,
  which is a much larger commitment than scipy.
- `numpy` alone — what this is.

**Why they were not adopted.** Two reasons, decisive first.

1. **A change to this screen should be a decision, not a side effect.** All
   three change the character of the map and none of them was chosen for being
   better at anything measured here — they were chosen for being available. The
   port changes coordinates and keeps the character; a substitution changes
   both, and would do it inside a commit whose subject line is about moving
   arithmetic off the paint thread. If the map is going to read differently,
   that should be the announced point of a change and not its footnote.
2. **The dependency budget is a stated constraint** and the saving is small.
   numpy is a base dependency; scipy and numba are not, and the brain image is
   already carrying a 2.2 GB embedding model. Sixty lines of vectorized numpy,
   under test, against ~100 MB of wheels is the wrong trade at this size.

**What would change the decision.** A measurement, on the real board, of two
things this cannot give: (a) wall-clock at the card counts that actually hurt —
exact t-SNE is O(n²) per iteration, so if a 2 000-card board takes seconds here
where Barnes-Hut takes tens of milliseconds, the cost of a one-time layout
change is worth paying and openTSNE is the pick; and (b) whether readers
actually use *between-cluster* distance, because if they do then UMAP is the
honest answer and t-SNE has been quietly lying to them all along — t-SNE's
inter-cluster distances are not meaningful, and no amount of implementation
quality fixes that. Either result gets written back into this note with the
number that produced it.
"""
