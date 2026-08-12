"""Candidate F's one addition after retrieval: ask a model which contexts help.

One batched call grades every candidate, and everything about the module fails
open — a blown budget, a raised exception, an unparseable line all mean "no
opinion", which clears the threshold. The worst a broken gate can do is nothing,
and "nothing" is the ungated pipeline that measured a tie with this one.
"""
import re
import threading

from langchain_core.documents import Document

# The measured threshold from the lab's candidate F. A context must be graded at
# least this useful to reach the answerer.
GRADE_THRESHOLD = 0.4
# Half marks means "the model expressed no opinion", and it deliberately clears
# the threshold: a reply that could not be parsed must degrade to the ungated
# pipeline — which measured a tie with the gated one — rather than silently
# emptying the context.
NO_OPINION = 0.5
# How long the user waits for the gate before it is abandoned. One constant, not
# the local/remote pair in llm.py, and for the opposite reason: a
# timeout protects the model's right to finish a call the answer depends on,
# while this bounds the wait for a stage that measured no quality gain at all.
# A loaded local model therefore loses its gate instead of costing 90 seconds.
GATE_BUDGET = 20.0
GATE_MAX_CHARS = 700

GATE_PROMPT = (
    'You score how useful each numbered excerpt is for answering a question '
    'about the user\'s own notes and journal. The text may be Persian or '
    'English. Reply with one line per excerpt in the form '
    '"<number>: <score 0-10>" and nothing else. 0 means irrelevant, 10 means it '
    'directly contains the answer.')


def _within_budget(budget_s: float, work):
    """Run `work`, and give up waiting for it after `budget_s`.

    The thread is a daemon and is never joined: an abandoned provider call must
    not hold up process exit, and the model's own timeout is what actually ends
    it. Returns None if the budget was blown or the call raised — both of which
    the caller reads as "no opinion"."""
    box: dict = {}

    def run():
        try:
            box['value'] = work()
        except Exception:
            pass   # a failed gate is a no-op gate; the caller defaults to 0.5

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(budget_s)
    return box.get('value')


def relevance_scores(llm, query: str, texts: list[str],
                     budget_s: float = GATE_BUDGET) -> list[float]:
    """Grade every candidate in **one** call.

    One call per candidate would be more accurate and would also multiply a
    question's cost by k — the difference between a gate that ships and a gate
    that is only ever measured once. Anything the reply does not score keeps
    NO_OPINION."""
    if not texts:
        return []
    listing = '\n\n'.join(f'[{i + 1}] {text[:GATE_MAX_CHARS]}'
                          for i, text in enumerate(texts))
    reply = _within_budget(budget_s, lambda: llm.invoke(
        [('system', GATE_PROMPT),
         ('user', f'Question: {query}\n\n{listing}')]))
    scores = [NO_OPINION] * len(texts)
    for line in str(getattr(reply, 'content', '') or '').splitlines():
        match = re.match(r'\s*\[?(\d+)\]?\s*[:.\-]\s*(\d+(?:\.\d+)?)', line)
        if match:
            index, value = int(match.group(1)) - 1, float(match.group(2))
            if 0 <= index < len(texts):
                scores[index] = min(10.0, value) / 10.0
    return scores


def relevance_gate(llm, query: str, documents: list[Document],
                   threshold: float = GRADE_THRESHOLD,
                   budget_s: float = GATE_BUDGET) -> list[Document]:
    """Candidate F's one addition after retrieval: drop the contexts a model
    says do not help. An empty result is the honest answer — without a gate every
    question gets an answer, including the ones the board cannot support."""
    scores = relevance_scores(llm, query, [doc.page_content for doc in documents],
                              budget_s)
    return [doc for doc, score in zip(documents, scores) if score >= threshold]


GRADERS = ('llm', 'none')


def gate_llm(kind: str, llm):
    """The model the gate should use, or None when the gate is off.

    The seam rule applied to BRAIN_GRADER: a new grader is a branch here, never
    an edited call site, and an unknown value raises at boot rather than
    silently leaving the gate switched off."""
    if kind == 'none':
        return None
    if kind == 'llm':
        return llm
    raise ValueError(f'unknown grader: {kind!r}; expected '
                     f'{" or ".join(repr(k) for k in GRADERS)}')


"""Alternatives considered

**"Why is the relevance gate yours? LangChain has document compressors for
exactly this."**

*Short answer.* Because LangChain's LLM filter calls the model once per
document, and it fails closed. This one is a single batched call that fails
open.

*Why the obvious option fails.* `LLMChainFilter` asks the model, per document,
whether it is relevant. At k=8 that is eight requests for one question — the
cost that kept an LLM gate out of the lab's sweep until it could be run against
a local model, and it is per *question*, so it multiplies through every eval. The
second problem is the default direction of failure: its boolean output parser
treats an unparseable answer as "not relevant", so a model that replies in prose
deletes the evidence. Here an unparsed line is NO_OPINION at 0.5, which clears
the 0.4 threshold on purpose — the worst a broken gate can do is nothing, and
"nothing" is the ungated pipeline that measured a tie with this one.

*Why not the framework.* `EmbeddingsFilter` *is* free and is the compressor that
needs no model — but it thresholds embedding similarity, which is more of what
dense retrieval already did. It cannot express the distinction the gate exists
for: on topic, and still not an answer. `ContextualCompressionRetriever` remains
the right seam to hang this on, as noted in `rerank.py`.

*The libraries that would do it.* `LLMChainFilter` — the greenfield pick if
per-question cost were not the constraint, and the more accurate of the two by
construction, since each document gets the model's whole attention. Cohere's
rerank API — one call, a relevance score per document, closest in shape to this
function; a paid third-party service that would see the journal. FlashRank — a
local cross-encoder, small and fast, English.

*Why not adopted, and what would change it.* Batching, and the fail-open
default. Two things would change it: a provider where per-document calls are
cheap enough to stop mattering, or — more usefully — a measurement, because the
batched listing's accuracy cost against per-document grading has never been
measured. The lab can run both over the same 30 questions with the same judge;
if per-document wins by more than the decision score's spread, the cost argument
has to be re-made rather than assumed.
"""
