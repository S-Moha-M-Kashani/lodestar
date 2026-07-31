"""RAGAS bridge.

Two tiers, because they cost different things and answer different questions:

**offline** — `NonLLMContextPrecisionWithReference` and `NonLLMContextRecall`
score the retrieved context against the ground truth's verbatim evidence quotes
using string similarity. No model, no key, no variance. These are the RAGAS
numbers to compare configurations on.

**llm** — `Faithfulness` (is the answer supported by the context?),
`ResponseRelevancy`, `FactualCorrectness` and the LLM context metrics. These
judge generation, which no deterministic metric can, and they need a model.
OpenRouter serves the chat model; it serves no embeddings, so the lab's own
embedder is wrapped for the one metric that needs vectors instead of silently
falling back to an OpenAI embedding call the user never asked to pay for.

RAGAS is an optional dependency of the lab, not of the brain. Everything here is
imported lazily and every failure is reported as a note rather than raised — a
missing wheel must not take the panel down.
"""
import os
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler

from .metrics import Measure

# RAGAS posts a usage event per evaluate() call. When that endpoint is
# unreachable the request does not fail fast — it blocks for ~150 seconds, per
# call, regardless of how many samples are being scored. That single line was
# 98% of a run's wall clock: 150s of waiting around 0.1s of measurement. Set
# before any ragas import; every ragas import in this module is lazy, so this
# module-level line always wins.
os.environ.setdefault('RAGAS_DO_NOT_TRACK', 'true')

OFFLINE_METRICS = ('non_llm_context_precision_with_reference',
                   'non_llm_context_recall')
LLM_METRICS = ('faithfulness', 'answer_relevancy', 'factual_correctness(mode=f1)',
               'llm_context_precision_with_reference', 'context_recall')

# The four that get a vote. Everything else the lab measures is still computed,
# still reported and still worth reading — it simply does not choose the
# architecture, because a metric only earns a vote if it grades something the
# others do not.
#
# Between them these four cover both ways a RAG pipeline fails, in both
# directions:
#
#   context precision   was what came back relevant, and ranked well?
#   context recall       did retrieval get everything the answer needed?
#   faithfulness         did the answer stay inside what was retrieved?
#   answer relevancy     did it actually address the question asked?
#
# What is deliberately excluded and why: `factual_correctness` scores against
# the reference *answer*, so it grades the fixture's phrasing as much as the
# pipeline; the offline pair are whole-string similarity, so they penalise large
# chunks regardless of whether the answer is inside them (measured: switching to
# multi-turn semantic segments halved them while *raising* quote recall), which
# makes them unusable for ranking across chunkers; and our own deterministic
# metrics grade retrieval almost exclusively, so ranking on them picks a config
# that finds the evidence and then says nothing useful about it. Those stay as
# the numbers to *debug* with — they never vary between runs, which is exactly
# what a judged score cannot promise.
DECISION_METRICS = ('faithfulness', 'answer_relevancy',
                    'llm_context_precision_with_reference', 'context_recall')

# How hard RAGAS is allowed to push the judge, per backend.
#
# RAGAS defaults to 16 concurrent requests and the lab used to accept that. A
# remote API absorbs it; one model on a laptop serves two or three at a time, so
# the sixteenth request waits behind the queue and trips the client timeout.
# Measured on gemma4:e2b judging candidate A: a single call took ~8s, calls under
# load reached 80–92s, and the run came back with one of its four deciding
# metrics — the other three were `Exception raised in Job[20]: TimeoutError()`.
# Concurrency and timeout cannot change *what* a judge scores, only whether the
# score arrives, which is why these are tuned per backend and not per candidate.
JUDGE_LOAD = {'openrouter': {'max_workers': 16, 'timeout': 180},
              'ollama': {'max_workers': 3, 'timeout': 600},
              'fake': {'max_workers': 16, 'timeout': 60}}


def judge_load(settings) -> dict:
    """RunConfig arguments for the backend actually serving the judge."""
    return JUDGE_LOAD.get(getattr(settings, 'provider', 'openrouter'),
                          JUDGE_LOAD['openrouter'])


# How many judge calls one sample costs, per metric. Measured against the prompts
# RAGAS actually sends, not guessed from the metric count:
#
#   faithfulness       2      decompose the answer into atomic statements, then
#                             one NLI pass ruling on all of them together
#   answer_relevancy   1      generate questions the answer would answer
#   context_recall     1      classify each reference sentence in one pass
#   factual_correctness 2     decompose claims both ways
#
# and context precision is the one that scales: **one verdict per retrieved
# chunk**, so it is k calls, and k is therefore what drives the bill. At k=8 it
# is 8 of the 12 deciding calls per question. Worth knowing before starting a run
# on a laptop model, which is exactly why the estimate is displayed.
CALLS_PER_SAMPLE = {'faithfulness': 2, 'answer_relevancy': 1,
                    'context_recall': 1, 'factual_correctness': 2}


def expected_judge_calls(n_samples: int, k: int,
                         include_factual: bool = True) -> int:
    """Roughly how many judge calls a judged run will make.

    An estimate and displayed as one: RAGAS retries malformed output, and
    faithfulness's statement count varies with the answer, so the true figure is
    this ± the judge's schema adherence. It exists so a two-hour run says it is a
    two-hour run at the start rather than at the end."""
    per_sample = sum(count for name, count in CALLS_PER_SAMPLE.items()
                     if include_factual or name != 'factual_correctness')
    return max(0, n_samples) * (per_sample + max(0, k))


class JudgeWatch(BaseCallbackHandler):
    """Counts judge calls as they land, so the judged phase can report progress.

    RAGAS scores a whole batch behind one `evaluate()` call, and with a local
    judge that batch is most of the run's wall clock. Without a per-call hook the
    bar sits at a single number for hours, which is indistinguishable from a hang
    — the same failure the per-question reporting in `run_eval` exists to avoid.

    A LangChain handler rather than RAGAS's own `_pbar`, because that one is
    private API and a progress bar must not be able to break a run."""

    def __init__(self, total: int, progress=None,
                 base: float = 0.92, span: float = 0.07):
        self.total = max(1, total)
        self.progress = progress
        self.base = base
        self.span = span
        self.calls = 0

    def fraction(self) -> float:
        # Clamped, because the estimate can be exceeded: a judge that retries
        # spends real calls, and a bar reading 130% would say the estimate was a
        # promise.
        return self.base + self.span * min(1.0, self.calls / self.total)

    def detail(self) -> str:
        return f'judge call {self.calls} of ~{self.total}'

    def _tick(self) -> None:
        self.calls += 1
        if self.progress:
            self.progress('ragas', self.fraction(), self.detail())

    def on_llm_end(self, response, **kwargs) -> None:
        self._tick()

    def on_llm_error(self, error, **kwargs) -> None:
        # A failed call is time spent, so it counts. Hiding it would make the bar
        # stall exactly when something is going wrong.
        self._tick()


def decision_score(metrics: dict) -> float | None:
    """The one number the architecture is chosen by: the unweighted mean of the
    four deciding metrics, or None unless every one of them is present.

    Unweighted because any weighting would be a claim about their relative
    importance that this fixture cannot support, and a hidden thumb on the scale
    is how a sweep ends up confirming whatever its author already believed.

    None rather than a partial mean because a mean over whichever metrics
    happened to succeed is not comparable between runs: an offline run would
    score on nothing, a half-failed judged run on two, and the shorter list
    would win for being easier.
    """
    values = []
    for name in DECISION_METRICS:
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or value != value:   # missing/NaN
            return None
        values.append(float(value))
    return round(sum(values) / len(values), 4)


def decision_spread(rows: list[dict]) -> dict:
    """The deciding score's mean and standard error over per-question composites.

    A leaderboard of means alone cannot say whether it ranked anything. These
    candidates land within ~0.01 of each other, and on a couple of dozen
    questions that gap is smaller than the error on either number — which is a
    fact about the experiment, not an opinion about it, so the score carries it.

    Per question first, then across questions: the four metrics score the same
    answers and move together, so pooling four separate standard errors as if
    they were independent would understate the spread. A question missing any of
    the four has no composite and is not a sample — the same rule
    `decision_score` applies to the run as a whole.
    """
    composites = [value for value in (decision_score(row) for row in rows)
                  if value is not None]
    n = len(composites)
    if not n:
        return {'n': 0, 'mean': None, 'stderr': None}
    mean = sum(composites) / n
    if n < 2:
        # One sample has no spread. Reporting 0.0 would read as certainty.
        return {'n': n, 'mean': round(mean, 4), 'stderr': None}
    variance = sum((value - mean) ** 2 for value in composites) / (n - 1)
    return {'n': n, 'mean': round(mean, 4),
            'stderr': round((variance / n) ** 0.5, 4)}


INSTALL_HINT = 'npm run raglab  (it pins these: ' \
    "ragas==0.4.*, langchain-community<0.4, langchain-openai<1, rapidfuzz)"

# These are RAGAS's metrics, under RAGAS's names, so the panel says whose
# definition it is showing and which class produced the number — including, for
# the five judged ones, that a model produced it and therefore that the number
# moves when the model does. Same shape as metrics.MEASURES so the dashboard has
# one idea of what a score is.
JUDGED = ', scored by the RAGAS judge model — a model\'s verdict, so it varies'
NO_JUDGE = ' — string distance via rapidfuzz, no model involved'

RAGAS_MEASURES = (
    Measure('ragas_decision', 'RAGAS decision score',
            'the number the architecture was chosen by', '',
            'mean(faithfulness, answer_relevancy, '
            'llm_context_precision_with_reference, context_recall) — undefined '
            'unless all four are present',
            'ragas_eval.decision_score over ragas 0.4.x metrics' + JUDGED,
            'Four judged RAGAS metrics, averaged unweighted, and the only score '
            'that picks between configurations here. They were chosen because '
            'between them they cover both halves of the pipeline in both '
            'directions: context precision asks whether what came back was '
            'relevant and well ranked, context recall whether everything the '
            'answer needed was retrieved, faithfulness whether the answer '
            'stayed inside what was retrieved, and answer relevancy whether it '
            'addressed the question at all. Everything else on this screen is '
            'reported and none of it votes — factual correctness grades the '
            'fixture\'s phrasing as much as the pipeline, the offline context '
            'pair punish large chunks for being large, and our own '
            'deterministic metrics grade retrieval almost exclusively, so '
            'ranking on them rewards a config that finds the evidence and then '
            'says nothing useful about it. The cost of that choice is honest: '
            'all four come from a judge, so this number carries a model\'s '
            'variance, and it is only comparable between runs scored by the '
            'same RAGAS judge model.'),
    Measure('non_llm_context_recall', 'Context recall (offline)',
            'RAGAS, string distance', 'retrieval',
            'reference quotes matched in the retrieved contexts / reference '
            'quotes, matching by string similarity',
            'ragas 0.4.x NonLLMContextRecall' + NO_JUDGE,
            'RAGAS\'s context recall with the judge removed: it compares the '
            'retrieved context to the ground truth\'s verbatim evidence quotes as '
            'strings. Deterministic, and the RAGAS number to compare configs on — '
            'but it compares *whole strings*, so longer chunks score lower even '
            'when the answer is in them. Use quote recall to compare across '
            'chunkers.'),
    Measure('non_llm_context_precision_with_reference',
            'Context precision (offline)', 'RAGAS, string distance', 'retrieval',
            'mean precision@k over the retrieved contexts, a context counting as '
            'relevant when it is similar enough to a reference quote',
            'ragas 0.4.x NonLLMContextPrecisionWithReference' + NO_JUDGE,
            'How much of the retrieved context matches the reference evidence, '
            'weighted towards the top of the ranking. Same whole-string caveat as '
            'its recall twin.'),
    Measure('faithfulness', 'Faithfulness', 'answer supported by the context',
            'generation',
            'supported claims / total claims in the answer',
            'ragas 0.4.x Faithfulness' + JUDGED,
            'RAGAS\'s own definition: the judge breaks the generated answer into '
            'individual claims, then checks each one against the retrieved '
            'context. An answer is faithful when the context supports every '
            'claim in it — so this measures invention, not correctness. A '
            'perfectly faithful answer can still be wrong if retrieval returned '
            'the wrong passage.'),
    Measure('answer_relevancy', 'Answer relevancy', 'does it answer the question',
            'generation',
            'mean cosine similarity between the original question and n '
            'questions the judge reverse-engineers from the answer',
            'ragas 0.4.x ResponseRelevancy (judge + the lab\'s own embedder for '
            'the vectors)' + JUDGED,
            'RAGAS asks the judge to invent the questions the answer would be a '
            'reply to, embeds them, and compares them with the real question. It '
            'catches answers that are true and on-topic but do not actually '
            'address what was asked. The vectors come from the embedder under '
            'test, so no external embedding API is called.'),
    Measure('factual_correctness(mode=f1)', 'Factual correctness',
            'claims vs the reference answer', 'generation',
            'F1 = 2PR/(P+R) over claims: P = claims in the answer supported by '
            'the reference, R = claims in the reference present in the answer',
            'ragas 0.4.x FactualCorrectness(mode=f1)' + JUDGED,
            'Unlike faithfulness, this compares the answer to the *reference '
            'answer* rather than to the retrieved context — so it measures being '
            'right, not merely being grounded. Both directions are counted, which '
            'is why it is an F1: an answer that omits half the reference loses '
            'recall, one that adds unsupported detail loses precision.'),
    Measure('llm_context_precision_with_reference', 'Context precision (judged)',
            'RAGAS, judged relevance', 'retrieval',
            'mean precision@k where the judge, not a string metric, decides '
            'whether each retrieved context is relevant',
            'ragas 0.4.x LLMContextPrecisionWithReference' + JUDGED,
            'The judged twin of the offline precision metric. It is immune to the '
            'whole-string penalty that makes the offline pair unfair to large '
            'chunks, at the cost of a model call per context and a model\'s '
            'variance.'),
    Measure('context_recall', 'Context recall (judged)', 'RAGAS, judged coverage',
            'retrieval',
            'reference claims attributable to the retrieved context / reference '
            'claims',
            'ragas 0.4.x LLMContextRecall' + JUDGED,
            'The judge splits the reference answer into claims and asks, for each '
            'one, whether the retrieved context could support it. It answers '
            '"was everything needed retrieved?" without depending on the exact '
            'wording of a quote.'),
)

RAGAS_MEASURE_HELP = {
    f'metric.{measure.key}':
    f'{measure.help} Formula: {measure.formula}. Computed by {measure.library}.'
    for measure in RAGAS_MEASURES}


@dataclass
class Availability:
    installed: bool = False
    llm_ready: bool = False
    version: str = ''
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {'installed': self.installed, 'llm_ready': self.llm_ready,
                'version': self.version, 'notes': list(self.notes),
                'offline_metrics': list(OFFLINE_METRICS),
                'llm_metrics': list(LLM_METRICS), 'install_hint': INSTALL_HINT}


def availability(settings) -> Availability:
    notes: list[str] = []
    try:
        import ragas
    except Exception as error:
        return Availability(notes=(f'ragas not importable: {error}',))
    version = getattr(ragas, '__version__', '?')
    try:
        import rapidfuzz  # noqa: F401
    except Exception:
        notes.append('rapidfuzz is missing — the offline (non-LLM) RAGAS metrics '
                     'cannot run without it')
    llm_ready = False
    # `llm_ready` asks whether a *real* judge can be reached, which is not the
    # same question as whether an OpenRouter key exists: a model served by Ollama
    # on this machine judges perfectly well and needs no key at all. What must
    # stay disqualified is the fake provider, which grades every answer without
    # ever failing and would fill the leaderboard with confident noise.
    if not settings.llm_ready:
        notes.append('no LLM backend for judging — set OPENROUTER_API_KEY, or '
                     'RAGLAB_LLM=ollama to judge on a local model')
    else:
        try:
            import langchain_openai  # noqa: F401
            llm_ready = True
        except Exception as error:
            notes.append(f'langchain-openai missing, LLM metrics disabled: {error}')
    return Availability(installed=True, llm_ready=llm_ready, version=version,
                        notes=tuple(notes))


class _LabEmbeddings:
    """LangChain's Embeddings surface over the lab's embedder, so RAGAS metrics
    that need vectors use the same representation the retrieval under test used
    — and no external embedding API is called."""

    def __init__(self, embedder):
        self.embedder = embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.embedder.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return self.embedder.embed([text])[0].tolist()

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


def _samples(pairs, ragas_mod, include_answers: bool,
             reference_texts: dict | None = None):
    """(question, outcome) → RAGAS samples, skipping what cannot be scored."""
    SingleTurnSample = ragas_mod.SingleTurnSample
    samples, skipped = [], 0
    for question, outcome in pairs:
        contexts = [c.text for c in outcome.contexts]
        references = (reference_texts or {}).get(question['id']) or [
            ev['quote'] for ev in question.get('evidence', [])]
        # Non-LLM context metrics compare retrieved text to reference text; with
        # either side empty the score is undefined, not zero.
        if not contexts or not references:
            skipped += 1
            continue
        payload = dict(user_input=question['question_fa'],
                       retrieved_contexts=contexts,
                       reference_contexts=references,
                       reference=question.get('answer_fa', ''))
        if include_answers:
            if not outcome.answer:
                skipped += 1
                continue
            payload['response'] = outcome.answer
        samples.append(SingleTurnSample(**payload))
    return samples, skipped


def run(pairs, settings, embedder, mode: str = 'offline',
        sample_limit: int | None = None,
        reference_texts: dict | None = None,
        judge_model: str = '', progress=None, k: int = 0) -> dict:
    """Score a run with RAGAS. `pairs` is [(ground-truth question, Outcome)].

    `judge_model` is the model RAGAS judges with — separate from the answerer on
    purpose, since a model grading its own output is not evidence.

    `progress(stage, fraction, detail)` is called as each judge call lands, and
    `k` is only there to estimate how many of those to expect — context precision
    is one call per retrieved chunk.

    Returns means per metric plus notes; never raises."""
    import warnings

    # `decision` starts as None and stays None on every path that cannot measure
    # all four — a missing key, a failed import, an offline run. A leaderboard
    # that ranked on a partially-measured composite would put the run that
    # measured least at the top.
    report: dict = {'mode': mode, 'metrics': {}, 'n_samples': 0, 'skipped': 0,
                    'decision': None,
                    'decision_spread': decision_spread([]),
                    'decision_metrics': list(DECISION_METRICS), 'notes': []}
    status = availability(settings)
    if not status.installed:
        report['notes'] = list(status.notes) + [f'install with: {INSTALL_HINT}']
        return report
    if mode == 'llm' and not status.llm_ready:
        report['notes'] = list(status.notes) or ['LLM metrics unavailable']
        return report

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        try:
            import ragas
            from ragas import EvaluationDataset, evaluate
            from ragas.metrics import (NonLLMContextPrecisionWithReference,
                                       NonLLMContextRecall)
            from ragas.run_config import RunConfig
        except Exception as error:
            report['notes'].append(f'ragas import failed: {error}')
            return report

        # Answer-side metrics only make sense when something was generated.
        include_answers = mode == 'llm'
        if sample_limit:
            pairs = list(pairs)[:sample_limit]
        samples, skipped = _samples(pairs, ragas, include_answers, reference_texts)
        report['skipped'] = skipped
        if not samples:
            report['notes'].append('nothing scoreable: no question produced both '
                                   'contexts and reference quotes')
            return report

        metrics = [NonLLMContextPrecisionWithReference(), NonLLMContextRecall()]
        if mode == 'llm':
            try:
                metrics += _llm_metrics(settings, embedder, judge_model)
                # Which backend served the judge, on the row itself. A decision
                # score is only comparable within one judge, so two rows scored
                # by different models are two different measurements — and the
                # model slug alone does not say whether it ran locally.
                report['judge'] = {
                    'provider': getattr(settings, 'provider', ''),
                    'model': judge_model or settings.llm_model}
                report['notes'].append(
                    f"judged by {report['judge']['model']} via "
                    f"{report['judge']['provider']}")
            except Exception as error:
                report['notes'].append(f'LLM metrics unavailable: {error}')
        load = judge_load(settings)
        report['judge_load'] = dict(load)
        watch = None
        if mode == 'llm':
            report['expected_judge_calls'] = expected_judge_calls(len(samples), k)
            watch = JudgeWatch(report['expected_judge_calls'], progress)
        try:
            result = evaluate(EvaluationDataset(samples=samples), metrics=metrics,
                              show_progress=False,
                              callbacks=[watch] if watch else None,
                              run_config=RunConfig(**load))
        except Exception as error:
            report['notes'].append(f'ragas evaluate failed: {error}')
            return report

    if watch:
        # What it actually cost, beside what it was estimated to cost. The gap is
        # the judge's retry rate, which is the one part of the estimate no
        # arithmetic can predict — and the lab does not otherwise meter calls.
        report['judge_calls'] = watch.calls
    report['n_samples'] = len(samples)
    report['metrics'] = _means(result)
    report['decision'] = decision_score(report['metrics'])
    report['decision_spread'] = decision_spread(
        list(getattr(result, 'scores', []) or []))
    if report['decision'] is None:
        absent = [name for name in DECISION_METRICS
                  if name not in report['metrics']]
        report['notes'].append(
            'no decision score: this run did not measure '
            + ', '.join(absent)
            + ' — only a judged run with an answerer can be ranked')
    if mode == 'offline':
        # Measured, not hypothetical: switching from 500-char packing to
        # multi-turn semantic segments *raised* quote recall while these scores
        # fell by half, purely because the retrieved strings got longer. They
        # compare whole strings, so they are only comparable between configs with
        # similar chunk sizes.
        report['notes'].append(
            'offline RAGAS context metrics are whole-string similarity, so they '
            'penalise longer chunks regardless of whether the answer is in them '
            '— compare them only across configs with similar chunk sizes, and '
            'use quote recall to compare across chunkers')
    report['ragas_version'] = getattr(ragas, '__version__', '?')
    report['notes'].extend(status.notes)
    return report


def _llm_metrics(settings, embedder, judge_model: str = ''):
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (Faithfulness, FactualCorrectness, LLMContextRecall,
                               LLMContextPrecisionWithReference, ResponseRelevancy)

    from .llm import judge_llm

    # Built through the lab's own seam rather than a ChatOpenAI here. This module
    # used to name the class, the OpenRouter key and the base URL itself, which
    # meant the judge was the one stage that could not follow RAGLAB_LLM: with
    # the answerer running locally, the row's judge was still going out to a
    # paid API, and nothing on the row said so.
    judge = LangchainLLMWrapper(judge_llm(settings, judge_model))
    vectors = LangchainEmbeddingsWrapper(_LabEmbeddings(embedder))
    return [Faithfulness(llm=judge),
            ResponseRelevancy(llm=judge, embeddings=vectors),
            FactualCorrectness(llm=judge),
            LLMContextPrecisionWithReference(llm=judge),
            LLMContextRecall(llm=judge)]


def _means(result) -> dict:
    """Average RAGAS's per-sample scores. `.scores` is a list of dicts in every
    0.x release; `.to_pandas()` is not used, to keep pandas out of the lab."""
    rows = list(getattr(result, 'scores', []) or [])
    if not rows:
        return {}
    keys = {key for row in rows for key in row}
    out = {}
    for key in sorted(keys):
        values = [row[key] for row in rows
                  if isinstance(row.get(key), (int, float))
                  and row[key] == row[key]]           # drop NaN
        if values:
            out[key] = round(sum(values) / len(values), 4)
    return out
