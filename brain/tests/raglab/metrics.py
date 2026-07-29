"""Deterministic scoring against the ground truth.

These metrics run offline, need no LLM, and are the ones to trust when comparing
configurations — an LLM judge introduces variance exactly where you are trying to
measure a 3-point difference. RAGAS metrics (ragas_eval.py) sit on top for the
answer-quality dimensions that genuinely need a model.

Two of them are specific to this ground truth and worth more than the textbook
set:

* **quote recall** — the fraction of the ground truth's *verbatim* evidence
  quotes that appear inside the retrieved text. Session-level recall says the
  right session was found; quote recall says the sentence that actually answers
  the question survived chunking. A chunker that splits mid-thought scores well
  on the first and badly on the second, which is precisely the failure a
  session-level metric hides.
* **latest-state recall** — on knowledge-update questions, whether the *most
  recent* evidence session was retrieved. Retrieving only the superseded state
  is worse than retrieving nothing: it produces a confident, stale answer.
"""
import difflib
import math
from collections import defaultdict

from . import textnorm
from .corpus import evidence_sessions

TYPES = ('single-hop', 'temporal', 'multi-hop', 'aggregation', 'knowledge-update',
         'commitment', 'entity', 'pattern', 'abstention', 'adversarial')


def recall_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    if not gold:
        return float('nan')
    top = set(retrieved[:k])
    return len([g for g in gold if g in top]) / len(gold)


def precision_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    if not retrieved:
        return 0.0
    top = retrieved[:k]
    return len([r for r in top if r in set(gold)]) / len(top)


def mrr(retrieved: list[str], gold: list[str]) -> float:
    for rank, item in enumerate(retrieved, start=1):
        if item in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    """Binary-gain nDCG. Rewards putting evidence first, not merely including
    it — which matters because the answerer sees a truncated context."""
    if not gold:
        return float('nan')
    gains = [1.0 if item in set(gold) else 0.0 for item in retrieved[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / ideal if ideal else float('nan')


def hit_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    return 1.0 if set(retrieved[:k]) & set(gold) else 0.0


def quote_recall(context_text: str, question: dict) -> float:
    """Verbatim-quote coverage, with a similarity fallback.

    Exact substring first, because the ground truth guarantees each quote is
    verbatim in its message. When a chunker normalises whitespace the substring
    test fails on text a reader would call identical, so a quote is also counted
    when its closest window in the context is >=90% similar."""
    quotes = [ev['quote'] for ev in question.get('evidence', [])]
    if not quotes:
        return float('nan')
    haystack = textnorm.normalize(context_text)
    found = 0
    for quote in quotes:
        needle = textnorm.normalize(quote)
        if needle in haystack:
            found += 1
        elif _fuzzy_contains(haystack, needle):
            found += 1
    return found / len(quotes)


def _fuzzy_contains(haystack: str, needle: str, threshold: float = 0.9) -> bool:
    if len(needle) > len(haystack):
        return False
    matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    match = matcher.find_longest_match(0, len(needle), 0, len(haystack))
    return match.size / len(needle) >= threshold


def latest_state_session(question: dict) -> str | None:
    """The newest evidence session — the one carrying the current truth."""
    evidence = question.get('evidence', [])
    if not evidence:
        return None
    return max(evidence, key=lambda ev: ev['session_id'])['session_id']


def answer_similarity(response: str, reference: str) -> float:
    """Character-level similarity to the reference answer. A blunt instrument,
    but a *stable* one: no model, no variance, and on Farsi it tracks whether the
    same names, dates and numbers appear."""
    if not response or not reference:
        return 0.0
    return difflib.SequenceMatcher(None, textnorm.normalize(response),
                                   textnorm.normalize(reference)).ratio()


def token_f1(response: str, reference: str) -> float:
    """Unigram F1 over content words — the SQuAD-style measure, which credits a
    short correct answer that a similarity ratio penalises for being short."""
    predicted = textnorm.tokens(response)
    gold = textnorm.tokens(reference)
    if not predicted or not gold:
        return 0.0
    overlap = 0
    remaining = list(gold)
    for token in predicted:
        if token in remaining:
            remaining.remove(token)
            overlap += 1
    if not overlap:
        return 0.0
    precision, recall = overlap / len(predicted), overlap / len(gold)
    return 2 * precision * recall / (precision + recall)


def score_question(question: dict, outcome, k: int) -> dict:
    """Every per-question number the report needs, for one config."""
    gold = evidence_sessions(question)
    retrieved = outcome.sessions
    context_text = '\n'.join(c.text for c in outcome.contexts)
    answerable = bool(question.get('answerable'))
    row = {
        'id': question['id'], 'type': question['type'],
        'difficulty': question['difficulty'], 'answerable': answerable,
        'retrieved_sessions': retrieved[:k],
        'n_contexts': len(outcome.contexts),
        'context_chars': len(context_text),
        'abstained': outcome.abstained,
        'time_scope': outcome.time_scope,
        'layers': sorted({c.layer for c in outcome.contexts}),
        'latency_ms': round(sum(outcome.timings.values()), 1),
    }
    if answerable:
        row |= {
            'recall': recall_at_k(retrieved, gold, k),
            'precision': precision_at_k(retrieved, gold, k),
            'mrr': mrr(retrieved, gold),
            'ndcg': ndcg_at_k(retrieved, gold, k),
            'hit': hit_at_k(retrieved, gold, k),
            'quote_recall': quote_recall(context_text, question),
        }
        if question['type'] == 'knowledge-update':
            latest = latest_state_session(question)
            row['latest_state_hit'] = float(latest in retrieved[:k]) if latest else float('nan')
        # An answerable question that got refused is a false abstention — the
        # failure mode a badly tuned grader produces.
        row['false_abstention'] = float(outcome.abstained)
    else:
        # Correct behaviour is a refusal (abstention) or a corrected premise
        # (adversarial). Both show up as `abstained`, because the answerer sets
        # it when it emits the refusal phrase.
        row['abstained_correctly'] = float(outcome.abstained)
    if outcome.answer is not None:
        row['answer'] = outcome.answer
        reference = question.get('answer_fa', '')
        if answerable and reference:
            row['answer_similarity'] = answer_similarity(outcome.answer, reference)
            row['answer_token_f1'] = token_f1(outcome.answer, reference)
    return row


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and not _isnan(v)]
    return round(sum(clean) / len(clean), 4) if clean else None


def _isnan(value) -> bool:
    return isinstance(value, float) and math.isnan(value)


AGGREGATED = ('recall', 'precision', 'mrr', 'ndcg', 'hit', 'quote_recall',
              'latest_state_hit', 'false_abstention', 'abstained_correctly',
              'answer_similarity', 'answer_token_f1', 'key_fact_coverage',
              'latency_ms', 'n_contexts', 'context_chars')


def aggregate(rows: list[dict]) -> dict:
    """Overall means, plus a per-type and per-difficulty breakdown.

    The per-type table is the point of the whole exercise: a config that lifts
    single-hop recall while destroying temporal recall has not improved, and one
    average hides that completely."""
    overall = {name: _mean([r[name] for r in rows if name in r])
               for name in AGGREGATED}
    by_type: dict[str, dict] = {}
    for type_name in TYPES:
        subset = [r for r in rows if r['type'] == type_name]
        if not subset:
            continue
        by_type[type_name] = {'n': len(subset)} | {
            name: _mean([r[name] for r in subset if name in r])
            for name in ('recall', 'quote_recall', 'ndcg', 'hit',
                         'abstained_correctly', 'false_abstention',
                         'answer_similarity')}
    by_difficulty: dict[str, dict] = {}
    for level in ('easy', 'medium', 'hard'):
        subset = [r for r in rows if r['difficulty'] == level]
        if subset:
            by_difficulty[level] = {'n': len(subset),
                                    'recall': _mean([r['recall'] for r in subset
                                                     if 'recall' in r])}
    layer_usage: dict[str, int] = defaultdict(int)
    for row in rows:
        for layer in row.get('layers', []):
            layer_usage[layer] += 1
    overall['headline'] = _headline(overall)
    return {'overall': overall, 'by_type': by_type, 'by_difficulty': by_difficulty,
            'layer_usage': dict(sorted(layer_usage.items(), key=lambda kv: -kv[1])),
            'n_questions': len(rows)}


def _headline(overall: dict) -> float | None:
    """One comparable number for the leaderboard: retrieval quality, the
    survival of the answering sentence, and honest refusal, weighted in that
    order. Deliberately excludes generation quality so configs measured with the
    extractive answerer stay comparable to those measured with an LLM."""
    parts = [(overall.get('recall'), 0.4), (overall.get('quote_recall'), 0.3),
             (overall.get('ndcg'), 0.2), (overall.get('abstained_correctly'), 0.1)]
    usable = [(v, w) for v, w in parts if v is not None]
    if not usable:
        return None
    total = sum(w for _, w in usable)
    return round(sum(v * w for v, w in usable) / total, 4)
