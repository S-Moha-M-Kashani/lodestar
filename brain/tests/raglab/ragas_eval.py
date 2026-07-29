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

INSTALL_HINT = 'npm run raglab  (it pins these: ' \
    "ragas==0.4.*, langchain-community<0.4, langchain-openai<1, rapidfuzz)"


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
    if not settings.openrouter_api_key:
        notes.append('no OPENROUTER_API_KEY — LLM-judged RAGAS metrics disabled')
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
        reference_texts: dict | None = None) -> dict:
    """Score a run with RAGAS. `pairs` is [(ground-truth question, Outcome)].

    Returns means per metric plus notes; never raises."""
    import warnings

    report: dict = {'mode': mode, 'metrics': {}, 'n_samples': 0, 'skipped': 0,
                    'notes': []}
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
                metrics += _llm_metrics(settings, embedder)
            except Exception as error:
                report['notes'].append(f'LLM metrics unavailable: {error}')
        try:
            result = evaluate(EvaluationDataset(samples=samples), metrics=metrics,
                              show_progress=False)
        except Exception as error:
            report['notes'].append(f'ragas evaluate failed: {error}')
            return report

    report['n_samples'] = len(samples)
    report['metrics'] = _means(result)
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


def _llm_metrics(settings, embedder):
    from langchain_openai import ChatOpenAI
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (Faithfulness, FactualCorrectness, LLMContextRecall,
                               LLMContextPrecisionWithReference, ResponseRelevancy)

    judge = LangchainLLMWrapper(ChatOpenAI(
        model=settings.llm_model, api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url, timeout=90))
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
