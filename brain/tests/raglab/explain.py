"""Every factor in the lab, explained.

Twenty-eight knobs and seven model roles is more than anybody holds in their
head, and a knob you cannot explain is a knob you cannot make a real decision
about. So each one carries a sentence or three, served over the API and shown
next to the control behind a `!`.

The text lives beside the definition it describes — knobs in `config.HELP`,
model roles in `models.ROLES` — and this module only assembles the two and
reports what is missing. `missing()` returning anything is a test failure, which
is what stops a new field from shipping unexplained.
"""
from .config import HELP, GenerationConfig, IndexConfig, RetrievalConfig
from .metrics import AGGREGATED, MEASURE_HELP, MEASURES
from .models import ROLE_HELP, ROLES
from .ragas_eval import (LLM_METRICS, OFFLINE_METRICS, RAGAS_MEASURE_HELP,
                         RAGAS_MEASURES)

GROUPS = (('index', IndexConfig), ('retrieval', RetrievalConfig),
          ('generation', GenerationConfig))


def topics() -> dict[str, str]:
    """Every explainable key → its text. '<group>.<field>' for configuration,
    'model.<role>' for the model roles, 'run.<field>' for one-run controls, and
    'metric.<key>' for every number a run reports — one registry, so the panel
    has one explainer mechanism rather than one per kind of thing."""
    return (dict(HELP) | dict(ROLE_HELP) | dict(MEASURE_HELP)
            | dict(RAGAS_MEASURE_HELP))


def measures() -> list[dict]:
    """Every metric the panel can print, ours then RAGAS's, in display order."""
    return [measure.as_dict() for measure in MEASURES + RAGAS_MEASURES]


def missing_metrics() -> list[str]:
    """Metrics a run can report with nothing explaining them.

    The counterpart of missing() for the knobs: a key added to
    metrics.AGGREGATED or to the RAGAS metric lists without a Measure beside it
    fails a test rather than reaching the dashboard as a bare number."""
    defined = {measure.key for measure in MEASURES + RAGAS_MEASURES}
    reported = (set(AGGREGATED) | {'headline'} | set(OFFLINE_METRICS)
                | set(LLM_METRICS))
    return sorted(reported - defined)


def model_fields() -> set[str]:
    """Config fields that are explained by a model role rather than by HELP."""
    return {role.field for role in ROLES}


def missing() -> list[str]:
    """Configuration fields with no explainer, in '<group>.<field>' form."""
    covered = set(topics()) | model_fields()
    return [f'{group}.{name}'
            for group, kind in GROUPS
            for name in kind.__dataclass_fields__
            if f'{group}.{name}' not in covered]
