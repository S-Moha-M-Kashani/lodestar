"""Does this message belong to the conversation it is being sent into?

The Assistant's confusion was never the model's fault: one endless transcript
meant every turn carried a slice of the last subject, so "hi" was read as the
next line of last month's thread. Sessions fixed that structurally. This module
covers the case sessions cannot: the user is *in* a chat, has moved on, and has
not thought to start a new one.

It only ever answers a question. The browser holds the turn back, offers "start a
new chat", and the user decides — so a wrong answer here costs one click, which
is what lets the signals stay cheap and local. Nothing in this module writes,
sends, or splits anything.

Two signals, cheapest first:

1. `is_opener` — a bare greeting, no subject. Pure pattern matching, no model at
   all, which is why the offline e2e suite can drive the whole nudge.
2. cosine distance from the centroid of the session's recent user messages,
   through the ordinary embeddings seam.

Fail open, always: an embedder that is missing, downloading or broken returns
"no drift" and the turn goes as asked.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Measured, 2026-08-06, against the labelled pairs in brain/tests/evals/
# with the real `heydariAI/persian-embeddings` model: same-subject pairs top
# out at 0.639, new-subject pairs start at 0.778, so this is the midpoint of
# the gap. Cosine *distance* (1 - similarity), so bigger is further apart.
DRIFT_DISTANCE = 0.708

# A greeting and nothing else. Tight and anchored at both ends on purpose: the
# same trade the transcriber's `signals_no_audio` makes, for the same reason. A
# false positive costs one click; a pattern loose enough to match "highlight the
# overdue habits" would interrupt real work, and one that matched any message
# *containing* a greeting would fire on "hi, can you check the visa rules?" —
# which is a subject, politely introduced.
_GREETING = re.compile(
    r'^(?:hi+|hey+|hello+|helo|yo|sup|salam|salaam|سلام|درود)'
    r'(?:\s+(?:there|again|all|everyone))?$',
    re.IGNORECASE)
# Trailing punctuation and the emoji people greet with, stripped before matching
# so "Hi!" and "سلام 👋" are the greetings they obviously are.
_TRIM = ' \t\r\n!?.,;:—–-…«»"\'👋🙂🙋'


@dataclass(frozen=True)
class DriftVerdict:
    """Not a bare bool: the UI says *why* it asked, and a calibration run that
    cannot be read is not a calibration."""

    changed: bool
    score: float
    reason: str


def is_opener(text: str) -> bool:
    """True when the message is a greeting carrying no subject of its own."""
    stripped = ' '.join(str(text or '').strip(_TRIM).split())
    return bool(_GREETING.match(stripped))


def _centroid(vectors: list[list[float]]) -> list[float]:
    width = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(width)]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    if not norm:
        # A zero vector has no direction, so there is no distance to report.
        # Nudging on it would be nudging on an artefact of the embedder.
        return 0.0
    return 1.0 - sum(x * y for x, y in zip(a, b)) / norm


def detect_drift(recent: list[str], incoming: str, embeddings) -> DriftVerdict:
    """Whether `incoming` looks like a new subject against `recent`.

    `recent` is this session's recent user messages, oldest first. Empty means
    the first message of a chat, which can never drift — there is nothing to
    drift from, and nudging there would open every new chat with a question
    about whether to start a new chat.
    """
    prior = [t for t in (recent or []) if str(t).strip()]
    text = str(incoming or '').strip()
    if not prior:
        return DriftVerdict(False, 0.0, 'first-message')
    if is_opener(text):
        # Reported as certain rather than as a distance: no embedding was taken,
        # and a score invented here would look like a measurement.
        return DriftVerdict(True, 1.0, 'opener')
    if embeddings is None:
        # No semantic signal available. Same answer as a broken embedder, because
        # it is the same situation: nobody measured anything, so nothing is
        # claimed. The caller decides when to withhold one — see the route.
        return DriftVerdict(False, 0.0, 'unavailable')
    try:
        centroid = _centroid(embeddings.embed_documents(prior))
        vector = embeddings.embed_query(text)
        score = _cosine_distance(centroid, vector)
    except Exception:
        # Deliberately every exception. The embedding model downloads on first
        # use and weighs ~2.2 GB, so "not fetched yet" is a normal state of a
        # working install, and a detector that blocks a turn is worse than no
        # detector at all.
        return DriftVerdict(False, 0.0, 'unavailable')
    changed = score >= DRIFT_DISTANCE
    return DriftVerdict(changed, score, 'distance' if changed else 'same-topic')


__all__ = ['DRIFT_DISTANCE', 'DriftVerdict', 'detect_drift', 'is_opener']

"""Alternatives considered

## Why did you write your own topic-change detector?

Because the cheap half is a regex and the expensive half is one call to an
embedder this service already loads, and neither is a library's job. The whole
module is ninety lines with no new dependency, and its output is a suggestion a
human accepts or refuses — the accuracy bar for that is far below the bar for
anything that acts on its own.

**Why the obvious option fails.** The obvious option is to ask the chat model:
*"is this a new subject? yes/no"*. It is more accurate than cosine distance and
needs no threshold. It also puts a paid round trip in front of every message the
user sends, doubles the latency before the first token, and makes the send path
depend on the chat provider being reachable — for a hint. Worse, it fails in the
direction that matters: when OpenRouter is slow, the nudge is slow, and the nudge
sits *before* the turn. A detector that delays the conversation it is protecting
has cost more than it saved.

**Why not the framework.** LangChain has no topic-segmentation primitive, and
this is the one place that is not a gap — `ConversationSummaryBufferMemory` and
friends solve the adjacent problem (fit a long chat into a context) by
summarising, which this project has declined twice. What LangChain does supply is
the part worth having: `Embeddings`, which is exactly the seam
`detect_drift` takes as an argument, so the local sentence-transformers model,
fastembed, and the test stub all arrive the same way. The regex half is ours
because it is fifteen tokens of Persian and English greetings, and no library
ships that list for the language pair this board is used in.

**The libraries that would do it.**

- `sentence-transformers`' own `util.cos_sim` — already installed transitively;
  saves the six lines of `_cosine_distance` and adds a torch import to a path
  that must work when torch is absent (`BRAIN_EMBEDDER=fake`).
- `numpy` — same six lines, genuinely faster on long vectors, and a dependency
  the brain does not otherwise need at this layer. At one comparison per message
  the speed is unmeasurable.
- `scikit-learn`'s `cosine_similarity` — the same trade as numpy plus 30 MB.
- **TextTiling** (`nltk.tokenize.TextTilingTokenizer`) — the real answer to this
  problem and the one I would reach for on a greenfield project: it segments a
  document by lexical cohesion, which is precisely "where does the subject
  change". It wants a long document to find its own boundaries, though, and the
  question here is asked about *one incoming message* against a short history —
  the shape TextTiling is weakest on. It also brings nltk and its corpora.
- `bertopic` / `sentence-transformers`' clustering utilities — topic *modelling*
  over a corpus, not a boundary test on the newest item. Right tool, wrong
  question.

**Why they were not adopted, and what would change the decision.** Decisively:
the numeric work is one dot product on two short vectors, and pulling in numpy,
torch or nltk to do it would make the module heavier than the thing it computes —
while breaking the property that the brain's test suite runs offline with no
extras. That is a taste call about dependencies, and it is stated as one.

The part that is *not* taste is `DRIFT_DISTANCE`, and it is now a measurement,
not a guess. The labelled fixture set in `brain/tests/evals/` — same-topic
pairs, different-topic pairs and openers, scored against the real
`heydariAI/persian-embeddings` model — came back cleanly separable on
2026-08-06: same-subject distances span 0.367–0.639, new-subject 0.778–0.921,
so any cut-off in (0.639, 0.778) works and 0.708 is the midpoint. The original
guess of 0.45 sat *inside* the same-subject range and would have nudged 67% of
on-topic messages. The classes separating this cleanly is also the verdict on
the LLM call rejected above: it has not earned its latency. If a future, larger
fixture set closes the gap — false-positive rate above roughly 10%, where the
nudge becomes nagging — that decision reopens, and `DriftVerdict` is the seam
it arrives behind.
"""
