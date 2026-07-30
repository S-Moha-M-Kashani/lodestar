"""Which language model runs which stage.

Seven stages of the lab can call a model, and they want different things from
one. The summariser runs once per session (157 calls per build) and wants cheap;
the key-facts judge runs once per question and wants the strongest thing
available; the reranker sits in the latency path of every query. Sharing one
model across all of them makes those trade-offs unmeasurable, so each stage
carries its own choice and nothing here is hard-coded.

Two rules the catalogue keeps:

**Every option says where its weights stand.** On a Farsi corpus the open-weight
models are the interesting ones — they can be run locally later, which is the
whole direction of the brain's LLMProvider seam — so `open` / `closed` is part of
the label, not a footnote.

**A model the lab has not actually run stays in the list, marked unavailable.**
Silently dropping it would hide the option; showing it as NA says "worth trying,
nobody measured it yet". Availability is verified against OpenRouter's own model
list when a key is present, and never guessed when it is not.
"""
from dataclasses import dataclass, fields

from .config import LabConfig, LabSettings

SOURCES = ('default', 'open', 'closed', 'unknown')


@dataclass(frozen=True)
class ModelOption:
    id: str                  # the OpenRouter slug that goes on the wire
    label: str               # what the dropdown shows
    source: str              # open | closed | unknown
    note: str = ''           # why you would pick it
    verified: bool = False   # this lab has actually produced numbers with it

    def as_dict(self, live: frozenset) -> dict:
        return {'id': self.id, 'label': self.label, 'source': self.source,
                'note': self.note, 'available': self.verified or self.id in live}


# Deliberately short. A dropdown of sixty slugs is not a choice either, and every
# entry here is one somebody might reasonably want on Farsi diary text.
CHAT_MODELS = (
    ModelOption('openai/gpt-5-nano', 'GPT-5 nano', 'closed', verified=True,
                note='every grade in .runs/ so far was measured on this'),
    ModelOption('openai/gpt-5-mini', 'GPT-5 mini', 'closed',
                note='the same family with more capacity — the obvious A/B'),
    ModelOption('openai/gpt-5', 'GPT-5', 'closed',
                note='for judging, where a wrong verdict costs more than tokens'),
    ModelOption('anthropic/claude-haiku-4.5', 'Claude Haiku 4.5', 'closed',
                note='fast, and strong at "answer only from this context"'),
    ModelOption('google/gemini-2.5-flash', 'Gemini 2.5 Flash', 'closed',
                note='cheap long context, useful for the summary pass'),
    ModelOption('meta-llama/llama-3.3-70b-instruct', 'Llama 3.3 70B', 'open',
                note='open weights, genuinely multilingual'),
    ModelOption('qwen/qwen-2.5-72b-instruct', 'Qwen2.5 72B', 'open',
                note='the strongest open candidate on Persian in public evals'),
    ModelOption('google/gemma-3-27b-it', 'Gemma 3 27B', 'open',
                note='small enough to run locally once Ollama lands'),
    ModelOption('mistralai/mistral-nemo', 'Mistral Nemo', 'open',
                note='Apache-2.0, cheap enough for the 157-session summary pass'),
    ModelOption('deepseek/deepseek-chat', 'DeepSeek Chat', 'open',
                note='open weights, reasons well about contradictions'),
)


@dataclass(frozen=True)
class ModelRole:
    key: str            # 'answer'
    label: str          # 'Answer'
    field: str          # 'generation.model'
    help: str
    only_when: str      # when this role is actually consulted

    @property
    def step(self) -> str:
        """Which pipeline step this model serves, and therefore which colour it
        wears in the panel. Derived from the field rather than declared twice:
        an ink that disagreed with where the value is stored would be worse than
        no ink at all."""
        return self.field.partition('.')[0]

    def as_dict(self) -> dict:
        return {'key': self.key, 'label': self.label, 'field': self.field,
                'help': self.help, 'only_when': self.only_when,
                'step': self.step}


ROLES = (
    ModelRole('summarize', 'Summaries', 'index.summarizer_model',
              'Writes the session, month and thread summaries the rollup layers '
              'are made of. It runs once per session, so this is the one role '
              'where cost dominates — and the only one that changes what is '
              'stored, so switching it builds a new collection.',
              'Summaries = llm'),
    ModelRole('expand', 'Query rewriting (HyDE)', 'retrieval.expansion_model',
              'Invents a plausible diary answer and searches with that instead '
              'of the question, on the theory that an answer looks more like the '
              'text you are hunting for than a question does. Only HyDE uses a '
              'model; multi-query expansion is rule-based and free.',
              'HyDE is on'),
    ModelRole('rerank', 'Reranker', 'retrieval.reranker_model',
              'Reads the top candidates and scores each one against the '
              'question. It sits in the latency path of every query, so a slow '
              'model here is felt on every single question.',
              'Reranker = llm'),
    ModelRole('grade', 'Relevance gate', 'retrieval.grader_model',
              'Decides whether a chunk is relevant at all. This is what lets '
              'the lab abstain rather than answer from noise: measured here, an '
              'LLM gate at 0.4 refused all five unanswerable questions while '
              'wrongly refusing 3% of the answerable ones — the lexical gate had '
              'no threshold that could do both.',
              'Gate = llm'),
    ModelRole('answer', 'Answer', 'generation.model',
              'Writes the Farsi answer from the retrieved context, cites session '
              'ids, and is the stage that must refuse when the diary is silent. '
              'Generation is the current bottleneck — faithfulness 0.743 against '
              'key-fact coverage 0.261 — so this is the interesting dropdown.',
              'Answerer = llm'),
    ModelRole('judge', 'Key-facts judge', 'generation.judge_model',
              'Checks a Farsi answer against the ground truth\'s English key '
              'facts. It is translating as well as judging, which is why no '
              'deterministic metric can replace it — and why a weak model here '
              'produces confidently wrong scores.',
              'the key-facts judge is on'),
    ModelRole('ragas', 'RAGAS judge', 'generation.ragas_model',
              'The model RAGAS uses for faithfulness, answer relevancy and '
              'factual correctness. Separate from the answerer on purpose: a '
              'model grading its own output is not evidence.',
              'RAGAS = judged'),
)

ROLE_HELP = {f'model.{role.key}': role.help for role in ROLES}


@dataclass(frozen=True)
class Roles:
    """The model each stage will actually ask for. '' means "whatever the
    provider defaults to", which is how RAGLAB_MODEL keeps working."""
    summarize: str = ''
    expand: str = ''
    rerank: str = ''
    grade: str = ''
    answer: str = ''
    judge: str = ''
    ragas: str = ''

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def chosen(cfg: LabConfig, role: ModelRole) -> str:
    group, _, name = role.field.partition('.')
    return getattr(getattr(cfg, group), name) or ''


def resolve(cfg: LabConfig, settings: LabSettings) -> Roles:
    """Config → concrete model per stage, filling blanks with the lab default."""
    picked = {role.key: chosen(cfg, role) or settings.llm_model for role in ROLES}
    return Roles(**picked)


_LIVE: dict[str, frozenset] = {}


def openrouter_ids(settings: LabSettings) -> frozenset:
    """Model ids OpenRouter is currently serving, or an empty set.

    Best-effort by design: the lab must run with no network at all, so a failure
    here means "cannot verify" (everything unverified shows as NA) and never an
    exception. Cached per base URL — the panel asks for options on every visit."""
    if not settings.openrouter_api_key:
        return frozenset()
    if settings.openrouter_base_url in _LIVE:
        return _LIVE[settings.openrouter_base_url]
    ids: frozenset = frozenset()
    try:
        import httpx
        res = httpx.get(f'{settings.openrouter_base_url.rstrip("/")}/models',
                        timeout=6.0)
        res.raise_for_status()
        ids = frozenset(m['id'] for m in res.json().get('data', []) if m.get('id'))
    except Exception:
        ids = frozenset()
    _LIVE[settings.openrouter_base_url] = ids
    return ids


def catalogue(settings: LabSettings) -> list[dict]:
    """The dropdown contents: the lab default first, then every candidate with
    its licence and whether it can be used right now."""
    live = openrouter_ids(settings)
    known = list(CHAT_MODELS)
    if settings.llm_model not in {option.id for option in known}:
        # An id set by RAGLAB_MODEL is by definition one the user wants, so it is
        # offered even though nothing is known about its weights.
        known.insert(0, ModelOption(settings.llm_model, settings.llm_model,
                                    'unknown', verified=True,
                                    note='named by RAGLAB_MODEL'))
    entries = [option.as_dict(live) for option in known]
    # Usable models first; NA sinks to the bottom without leaving the list.
    entries.sort(key=lambda entry: not entry['available'])
    return [{'id': '', 'label': f'lab default ({settings.llm_model})',
             'source': 'default', 'available': True,
             'note': 'follows RAGLAB_MODEL, so one change moves every stage'}
            ] + entries


def note_for(cfg: LabConfig, settings: LabSettings) -> str:
    """A one-line description of the stage/model split, for a run's notes."""
    roles = resolve(cfg, settings)
    used = {role.key: getattr(roles, role.key) for role in ROLES}
    distinct = sorted(set(used.values()))
    if len(distinct) == 1:
        return f'every LLM stage ran on {distinct[0]}'
    return 'models per stage: ' + ', '.join(f'{key}={value}'
                                            for key, value in used.items())
