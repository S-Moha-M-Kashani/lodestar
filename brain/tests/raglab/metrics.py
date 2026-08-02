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
from dataclasses import dataclass

from lodestar_brain import textnorm
from .corpus import evidence_sessions

TYPES = ('single-hop', 'temporal', 'multi-hop', 'aggregation', 'knowledge-update',
         'commitment', 'entity', 'pattern', 'habit', 'abstention', 'adversarial')


@dataclass(frozen=True)
class Measure:
    """What one number on the dashboard means.

    A score nobody can check is worse than no score: "faithfulness 0.74" says
    nothing without whose definition it is, what arithmetic produced it, and what
    code ran that arithmetic. So every metric — ours and RAGAS's — carries the
    same four facts in the same shape, which is what lets the panel render them
    identically instead of having two ideas of what a score is.

    `step` is the pipeline stage the number grades, so it wears that stage's ink
    (see config.STEPS). '' means the whole pipeline, which no single colour can
    honestly claim.
    """
    key: str          # the key it arrives under in summary.overall / ragas.metrics
    label: str        # what the score card is titled
    short: str        # the one-line caption under the number
    step: str         # 'retrieval' | 'generation' | '' (whole pipeline)
    formula: str      # the arithmetic, not a description of it
    library: str      # what computed it, and whether a model was involved
    help: str         # the paragraph behind the '!'

    def as_dict(self) -> dict:
        return {'key': self.key, 'label': self.label, 'short': self.short,
                'step': self.step, 'formula': self.formula,
                'library': self.library, 'help': self.help}


# Order matters: this is the order the score cards appear in, headline first.
NO_MODEL = ' — pure Python, no model, so it never varies between runs'

MEASURES = (
    Measure('headline', 'Composite', 'weighted retrieval score', '',
            '0.4·recall + 0.3·quote_recall + 0.2·ndcg + '
            '0.1·abstained_correctly, renormalised over whichever of the four '
            'this run produced',
            'metrics._headline' + NO_MODEL,
            'One comparable number for the leaderboard, weighted in the order '
            'that matters here: did retrieval find the evidence, did the '
            'answering sentence survive chunking, was the evidence ranked first, '
            'and was an unanswerable question refused. Generation quality is '
            'deliberately excluded, so a config measured with the extractive '
            'answerer stays comparable to one measured with an LLM.'),
    Measure('recall', 'Recall@k', 'evidence sessions found',
            'retrieval', '|gold ∩ top-k| / |gold|',
            'metrics.recall_at_k' + NO_MODEL,
            'Of the diary sessions the ground truth marks as evidence, the share '
            'that appear in the top k retrieved. This is the ceiling on '
            'everything downstream: an answer cannot cite what retrieval never '
            'returned. Questions with no evidence (the unanswerable ones) are '
            'excluded rather than scored zero.'),
    Measure('quote_recall', 'Quote recall', 'answering sentence survived chunking',
            'retrieval',
            'matched quotes / total quotes, where a quote counts as matched if it '
            'is a substring of the normalised context or its longest common run '
            'covers >= 0.9 of it',
            'metrics.quote_recall (difflib.SequenceMatcher)' + NO_MODEL,
            'Session recall says the right day was found; this says the sentence '
            'that actually answers the question is inside the retrieved text. A '
            'chunker that cuts mid-thought scores well on the first and badly on '
            'this one, which is exactly the failure a session-level metric hides.'),
    Measure('ndcg', 'nDCG@k', 'evidence ranked first', 'retrieval',
            'DCG/IDCG with binary gains: DCG = Σ gain_i / log2(i+2), IDCG the '
            'same sum over a perfect ordering',
            'metrics.ndcg_at_k (math.log2)' + NO_MODEL,
            'Rewards putting evidence at the top rather than merely including it '
            'somewhere in k. It matters because the answerer sees a context that '
            'gets truncated, so rank 1 and rank 8 are not worth the same.'),
    Measure('mrr', 'MRR', 'rank of first evidence', 'retrieval',
            '1 / rank of the first evidence session, 0 if none was retrieved',
            'metrics.mrr' + NO_MODEL,
            'How deep you have to read before the first correct hit. Sensitive to '
            'the top of the list only — useful next to nDCG, not instead of it.'),
    Measure('precision', 'Precision@k', 'signal to noise', 'retrieval',
            '|top-k ∩ gold| / |top-k|',
            'metrics.precision_at_k' + NO_MODEL,
            'How much of what was retrieved is actually evidence. Low precision '
            'is not automatically bad — it is the cost of a wide k — but it is '
            'what dilutes the answerer\'s context and what MMR trades against.'),
    Measure('hit', 'Hit@k', 'at least one evidence session', 'retrieval',
            '1 if top-k contains any gold session, else 0',
            'metrics.hit_at_k' + NO_MODEL,
            'The most forgiving retrieval metric: did anything relevant come '
            'back at all. Useful for spotting total misses that an averaged '
            'recall softens.'),
    Measure('latest_state_hit', 'Latest state',
            'changed facts, current version', 'retrieval',
            '1 if the newest evidence session is in top-k, else 0 '
            '(knowledge-update questions only)',
            'metrics.latest_state_session' + NO_MODEL,
            'On facts that changed over the year, retrieving only the superseded '
            'state is worse than retrieving nothing: it produces a confident, '
            'stale answer. This checks the most recent evidence session was '
            'found.'),
    Measure('abstained_correctly', 'Abstention', 'unanswerable refused',
            'generation',
            'refusals / unanswerable questions',
            'metrics.score_question, reading the answerer\'s refusal flag'
            + NO_MODEL,
            'The diary genuinely has nothing about some of these questions. '
            'Saying so is the correct answer, and this is the fraction of those '
            'where the answerer refused instead of inventing something.'),
    Measure('false_abstention', 'False refusals', 'answerable wrongly refused',
            'generation', 'refusals / answerable questions (lower is better)',
            'metrics.score_question, reading the answerer\'s refusal flag'
            + NO_MODEL,
            'The other side of abstention, and the failure a badly tuned '
            'relevance gate produces: the evidence was there and the answerer '
            'still said it had nothing. Read it together with abstention — a '
            'gate that refuses everything scores perfectly on one and terribly '
            'here.'),
    Measure('answer_similarity', 'Answer similarity', 'vs the reference answer',
            'generation',
            'difflib ratio = 2·M / T over normalised characters, where M is '
            'matched characters and T the combined length',
            'metrics.answer_similarity (difflib.SequenceMatcher)' + NO_MODEL,
            'A blunt instrument, chosen because it is a *stable* one: no model, '
            'no variance. On Farsi it mostly tracks whether the same names, dates '
            'and numbers appear. It punishes a correct short answer, which is why '
            'token F1 sits beside it.'),
    Measure('answer_token_f1', 'Answer token F1', 'unigram overlap', 'generation',
            'F1 = 2PR/(P+R) over content words, P = overlap/|predicted|, '
            'R = overlap/|reference|, counting duplicates once',
            'metrics.token_f1 (textnorm tokeniser)' + NO_MODEL,
            'The SQuAD-style measure. It credits a short correct answer that a '
            'character-similarity ratio penalises purely for being short.'),
    Measure('key_fact_coverage', 'Key facts', 'judged fact coverage',
            'generation',
            'facts the judge marked present / key facts in the ground truth',
            'evaluate.judge_key_facts — an LLM judge (the "Key-facts judge" '
            'model role), so this number carries that model\'s variance',
            'The ground truth lists the English key facts a correct answer must '
            'contain; the answers are Farsi. The judge is translating as well as '
            'checking, which is why no deterministic metric replaces it — and why '
            'a weak model here produces confidently wrong scores.'),
    Measure('latency_ms', 'Latency', 'ms per question', '',
            'sum of the per-stage timings for one question, in milliseconds',
            'time.perf_counter around each pipeline stage' + NO_MODEL,
            'Whole-pipeline wall clock, so it spans every stage rather than '
            'grading one. Dominated by whatever calls a model: an LLM reranker or '
            'gate moves this by orders of magnitude, a hash embedder does not.'),
    Measure('n_contexts', 'Contexts', 'chunks handed to the answerer', 'retrieval',
            'mean number of contexts surviving rerank, gate and expansion',
            'metrics.score_question' + NO_MODEL,
            'What k actually delivered. It falls below k when the relevance gate '
            'removes chunks, which is the honest reason an answer had less to '
            'work with.'),
    Measure('context_chars', 'Context size', 'characters of context', 'retrieval',
            'mean total characters of the assembled context',
            'metrics.score_question' + NO_MODEL,
            'How much text the answerer was given. Worth watching next to '
            'precision: two configs with the same recall can differ by 3x here, '
            'and the bigger one is paying for it in tokens and in dilution.'),
)

MEASURE_HELP = {f'metric.{measure.key}':
                f'{measure.help} Formula: {measure.formula}. Computed by '
                f'{measure.library}.'
                for measure in MEASURES}


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
    overall['headline'] = _headline(overall)
    return {'overall': overall, 'by_type': by_type, 'by_difficulty': by_difficulty,
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
