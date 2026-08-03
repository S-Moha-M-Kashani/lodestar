"""Lab settings and the three config objects the whole pipeline is driven by.

Splitting the knobs into IndexConfig / RetrievalConfig / GenerationConfig is not
cosmetic: only IndexConfig changes what is stored, so its fingerprint names the
in-memory index. Retrieval and generation can then be swept for free against
an index that is already built — which is what makes the settings panel usable.

**There are no storage settings here, and that is the design.** An experiment's
index lives in process memory (`store.MemoryVectors`) and its results in one
JSON file per run under RUNS_DIR. The lab used to carry a Chroma url and its own
database name, guarded by a check that refused 'lodestar'; a setting that does
not exist is the stronger guard, because it cannot be pointed at real chat
memory by a typo, a stale shell, or a command copied from an old README.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]        # the lodestar repo root
RUNS_DIR = Path(__file__).resolve().parent / '.runs'

# Which backend serves the lab's chat models. Local Ollama is the default: the
# lab's judged runs can make hundreds of model calls, so a default must never
# silently spend API credit. Naming another provider is an explicit opt-in.
LLM_PROVIDERS = ('', 'openrouter', 'ollama', 'fake')

# The default chat model per backend, because a slug only means something to the
# backend that serves it. The local default is the model the judge screen has a
# row for (`.screens/`) — a default nobody screened is judge-shopping with extra
# steps. 'fake' keeps the remote slug: it ignores the model entirely, and changing
# it would make the offline runs' notes disagree with every earlier one.
PROVIDER_MODELS = {'openrouter': 'openai/gpt-5-nano',
                   'ollama': '4skl/gemma4-e2b-mtp',
                   'fake': 'openai/gpt-5-nano'}


def load_env_file(path: Path | None = None) -> None:
    """Read repo-root .env into the environment without overriding what is
    already set. The brain gets its key from the shell or Docker; the lab is
    started by hand, so it reads the file the user already keeps there."""
    path = path or ROOT / '.env'
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class LabSettings:
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    # See LLM_PROVIDERS. Set RAGLAB_LLM=ollama to run every LLM stage — answerer,
    # gate, reranker and the RAGAS judge — on a model on this machine, which is
    # what makes the expensive candidates (a per-chunk LLM gate is k calls per
    # question) measurable without buying credit.
    llm_provider: str = 'ollama'
    ollama_base_url: str = 'http://localhost:11434/v1'
    # '' = the provider's own default (PROVIDER_MODELS), resolved in __post_init__
    # so every reader sees a concrete slug. It has to follow the provider: a
    # remote slug left standing under RAGLAB_LLM=ollama made
    # `models.provider_problems` refuse every run, for a model the user never
    # picked. A default that cannot run is a broken default, not a strict one.
    llm_model: str = ''
    fastembed_model: str = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
    # Multilingual on purpose: fastembed's default rerankers (ms-marco-MiniLM,
    # jina-reranker-v1-*-en) are English-only and score Farsi pairs as noise.
    # ~1.1 GB on first use; override with RAGLAB_CROSS_ENCODER.
    cross_encoder_model: str = 'jinaai/jina-reranker-v2-base-multilingual'

    def __post_init__(self):
        if self.llm_provider not in LLM_PROVIDERS:
            raise ValueError(
                f'unknown RAGLAB_LLM {self.llm_provider!r}; expected one of '
                + ', '.join(repr(name) for name in LLM_PROVIDERS))
        # Only when unset: overwriting a stated model would mean a run labelled
        # with one model had been scored by another, which is the one artefact
        # this lab must never produce.
        if not self.llm_model:
            object.__setattr__(self, 'llm_model', PROVIDER_MODELS[self.provider])

    @property
    def provider(self) -> str:
        """The backend a chat model will actually be built with — never ''.

        One place resolves the blank, because every caller that resolved it for
        itself was a chance for the judge to run on one backend while the run
        notes claimed the other."""
        if self.llm_provider:
            return self.llm_provider
        return 'openrouter' if self.openrouter_api_key else 'fake'

    @property
    def llm_ready(self) -> bool:
        """Whether an LLM stage would reach a real model. 'fake' answers and
        judges without ever failing, which is why this is not `bool(key)`: the
        thing worth knowing is not whether a credential exists but whether the
        numbers a run produces mean anything."""
        return self.provider in ('openrouter', 'ollama')


def load_lab_settings(env: dict | None = None) -> LabSettings:
    load_env_file()
    env = os.environ if env is None else env
    # BRAIN_CHROMA_URL and RAGLAB_CHROMA_DATABASE are deliberately not read: the
    # board's Chroma stack runs whenever a board does, and an experiment must not
    # be able to find it just because it is there.
    return LabSettings(
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL',
                                    'https://openrouter.ai/api/v1'),
        llm_provider=env.get('RAGLAB_LLM', 'ollama'),
        ollama_base_url=env.get('RAGLAB_OLLAMA_BASE_URL',
                                'http://localhost:11434/v1'),
        llm_model=env.get('RAGLAB_MODEL', ''),
        fastembed_model=env.get(
            'RAGLAB_FASTEMBED_MODEL',
            'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'),
        cross_encoder_model=env.get('RAGLAB_CROSS_ENCODER',
                                    'jinaai/jina-reranker-v2-base-multilingual'),
    )


def settings_for_provider(settings: LabSettings, provider: str) -> LabSettings:
    """One run's backend override — how the panel's mode dropdown moves the
    LLM stages without restarting the lab. '' means no override: the settings
    pass through untouched.

    The old backend's *default* model does not survive the switch, because a
    slug only means something to the backend that serves it (PROVIDER_MODELS);
    a model the user explicitly named (RAGLAB_MODEL) is never replaced, or the
    run's label and the model that produced it would disagree."""
    if not provider:
        return settings
    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f'unknown provider {provider!r}; expected one of '
            + ', '.join(repr(name) for name in LLM_PROVIDERS if name))
    model = ('' if settings.llm_model == PROVIDER_MODELS.get(settings.provider)
             else settings.llm_model)
    return replace(settings, llm_provider=provider, llm_model=model)


# Every option tuple below leads with the value the lab actually defaults to.
# That is not cosmetic ordering: these tuples are what both panels render, so a
# default buried sixth reads as an exotic choice while three hash embedders that
# exist only to be measured *against* sit at the top of the list. The measured
# winner should be the first thing offered. `test_every_option_list_leads_with_
# the_default` holds the two in step, so changing a default without moving it
# fails rather than quietly demoting it.
CHUNKERS = ('semantic-drift', 'fixed', 'fixed-overlap', 'message', 'turn-pair',
            'session')
# Which chunkers actually read chunk_chars and overlap. Read off chunking.py's
# own branches rather than assumed: 'semantic-drift' passes chunk_chars to
# _semantic_segments as its max_chars cap ("or where the segment would outgrow
# max_chars"), so it belongs here even though it cuts on meaning rather than
# length. 'message', 'turn-pair' and 'session' emit one piece per message, pair
# or day and ignore both numbers entirely.
CHAR_SIZED_CHUNKERS = ('semantic-drift', 'fixed', 'fixed-overlap')
OVERLAP_CHUNKERS = ('fixed-overlap',)
# Two of these load a named model: 'fastembed' (its own ONNX list) and
# 'sentence-transformers' (any HuggingFace checkpoint — the only way to reach
# the Persian-tuned encoders). The hash embedders take no model at all. The
# 'openai' API backend left 2026-08-02 with its whole catalogue.
EMBEDDERS = ('sentence-transformers', 'fastembed',
             'ascii-hash', 'token-hash', 'char-hash')
MODEL_EMBEDDERS = ('fastembed', 'sentence-transformers')
RETRIEVERS = ('hybrid-rrf', 'dense', 'bm25')
RERANKERS = ('lexical', 'none', 'recency', 'agentic', 'cross-encoder', 'llm')
GRADERS = ('none', 'lexical', 'llm')
ANSWERERS = ('extractive', 'none', 'llm')
# Ascending, and the order the remainder of an uneven sample is handed out in, so
# a balanced selection is reproducible rather than merely proportionate.
DIFFICULTIES = ('easy', 'medium', 'hard')
# How a limited run picks its questions. See evaluate.select_questions.
BALANCES = ('stride', 'difficulty')


# Which controls are live, and under what. Served rather than duplicated in each
# panel for the same reason STEPS is: two copies of a rule drift, and a panel
# that greys out the wrong knob teaches the reader something false about the
# pipeline. Each entry names the field it depends on, the values that switch it
# on, and — the part that matters — *why*, because a control that is greyed out
# with no reason is indistinguishable from one that is broken.
#
# `on` lists the enabling values; `on_true` means a boolean must be set. The
# reason is written to complete the sentence "disabled because …".
DEPENDENCIES = {
    'index.chunk_chars': {
        'field': 'index.chunker', 'on': list(CHAR_SIZED_CHUNKERS),
        'reason': 'the message, turn-pair and session chunkers cut on structure, '
                  'not on length'},
    'index.overlap': {
        'field': 'index.chunker', 'on': list(OVERLAP_CHUNKERS),
        'reason': 'only the fixed-overlap chunker slides a window'},
    'index.embed_model': {
        'field': 'index.embedder', 'on': list(MODEL_EMBEDDERS),
        'reason': 'the hash embedders load no model'},
    'retrieval.rrf_k': {
        'field': 'retrieval.retriever', 'on': ['hybrid-rrf'],
        'reason': 'only hybrid-rrf fuses two rankings'},
    'retrieval.rerank_depth': {
        'field': 'retrieval.reranker', 'on': [r for r in RERANKERS if r != 'none'],
        'reason': 'nothing is reranked'},
    'retrieval.reranker_model': {
        'field': 'retrieval.reranker', 'on': ['llm'],
        'reason': 'only the llm reranker calls a model'},
    'retrieval.recency_half_life_days': {
        'field': 'retrieval.reranker', 'on': ['recency', 'agentic'],
        'reason': 'only the recency and agentic rerankers weigh age'},
    'retrieval.agentic_weights': {
        'field': 'retrieval.reranker', 'on': ['agentic'],
        'reason': 'only the agentic reranker has weights to balance'},
    'retrieval.grade_threshold': {
        'field': 'retrieval.grader', 'on': [g for g in GRADERS if g != 'none'],
        'reason': 'the gate is off, so nothing is scored to threshold'},
    'retrieval.grader_model': {
        'field': 'retrieval.grader', 'on': ['llm'],
        'reason': 'only the llm gate calls a model'},
    'retrieval.expansion_model': {
        'field': 'retrieval.hyde', 'on_true': True,
        'reason': 'HyDE is off — multi-query expansion is rule-based and uses no '
                  'model'},
    'generation.model': {
        'field': 'generation.answerer', 'on': ['llm'],
        'reason': 'only the llm answerer calls a model'},
    'generation.judge_model': {
        'field': 'generation.key_facts_judge', 'on_true': True,
        'reason': 'the key-facts judge is off'},
}


def dependency_state(cfg_dict: dict) -> dict:
    """For one config, which dependent fields are live and why not.

    Returns `{'<group>.<field>': {'enabled': bool, 'reason': str}}`. Shared by
    both panels and by the tests, so what the UI greys out and what the pipeline
    ignores cannot disagree."""
    state = {}
    for key, rule in DEPENDENCIES.items():
        group, _, name = rule['field'].partition('.')
        current = (cfg_dict.get(group) or {}).get(name)
        enabled = (bool(current) if rule.get('on_true')
                   else current in rule.get('on', ()))
        state[key] = {'enabled': enabled, 'reason': '' if enabled else rule['reason']}
    return state


@dataclass(frozen=True)
class Step:
    """One of the three stages a knob or a model can belong to.

    The panel groups and colour-codes everything by these, so the list is served
    rather than reinvented in each frontend. Only the *meaning* lives here — the
    ink for each step is a CSS token, because a colour that has to work on four
    different papers is not a fact about the pipeline.

    Two names on purpose: `label` titles a panel of knobs, `short` tags a group
    of models inside another panel, where a whole clause would not fit.
    """
    key: str        # matches the config group, and the CSS ink token
    short: str      # 'Index'
    label: str      # 'Index — what gets stored'
    note: str       # when it runs, and therefore what a change there costs


STEPS = (
    Step('index', 'Index', 'Index — what gets stored',
         'Runs once per corpus. It decides what can ever be found, so a change '
         'here rebuilds the collection and invalidates the numbers below it.'),
    Step('retrieval', 'Retrieval', 'Retrieval & ranking',
         'Runs on every question against an index that already exists — which '
         'is why these are the knobs worth sweeping first.'),
    Step('generation', 'Generation', 'Generation & scoring',
         'Turns the retrieved contexts into a Farsi answer, refuses when the '
         'diary is silent, and grades what it wrote.'),
)


@dataclass(frozen=True)
class IndexConfig:
    """What gets indexed. Its fingerprint names the in-memory index."""
    chunker: str = 'semantic-drift'
    chunk_chars: int = 500
    overlap: int = 100          # fixed-overlap only
    contextual: bool = True     # prepend a situating header to every chunk
    # Persian-tuned by default: the corpus is a Farsi diary, and the offline
    # hash embedders exist to be *measured against* a real encoder, not to be it.
    # '' below means "the recommended model for that backend"
    # (embedding.BACKEND_DEFAULTS), which for sentence-transformers is
    # heydariAI/persian-embeddings.
    embedder: str = 'sentence-transformers'
    embed_model: str = ''       # model-backed kinds only; '' = backend default

    def normalized(self) -> 'IndexConfig':
        # A model that is not consulted is blanked, because this object's
        # fingerprint names the index: a model nobody calls must not invalidate
        # it and cost a 157-session rebuild.
        return replace(self, embed_model=(self.embed_model
                                          if self.embedder in MODEL_EMBEDDERS
                                          else ''))

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self.normalized()), sort_keys=True)
        return hashlib.sha1(payload.encode()).hexdigest()[:12]

    def collection(self) -> str:
        return f'raglab-{self.fingerprint()}'


@dataclass(frozen=True)
class RetrievalConfig:
    """Everything between a question and the assembled context."""
    retriever: str = 'hybrid-rrf'
    k: int = 8                       # contexts handed to the answerer
    candidates: int = 40             # depth taken from each retriever
    rrf_k: int = 60
    time_filter: bool = True         # resolve Farsi time words into a date range
    # On by default because it is free and measured positive on this fixture:
    # quote recall 0.489 → 0.512 and precision 0.243 → 0.300, no LLM call.
    multi_query: bool = True
    hyde: bool = False               # LLM hypothetical answer as the query
    expansion_model: str = ''        # HyDE only; '' = LabSettings.llm_model
    mmr_lambda: float = 1.0          # 1.0 = pure relevance, lower = diversify
    reranker: str = 'lexical'
    rerank_depth: int = 20
    reranker_model: str = ''         # reranker='llm' only
    recency_half_life_days: float = 180.0
    agentic_weights: tuple[float, float, float] = (1.0, 0.3, 0.2)
    grader: str = 'none'             # gate that makes abstention possible
    grade_threshold: float = 0.0
    grader_model: str = ''           # grader='llm' only
    max_context_chars: int = 6000

    def normalized(self) -> 'RetrievalConfig':
        return replace(self, agentic_weights=tuple(self.agentic_weights))


@dataclass(frozen=True)
class GenerationConfig:
    answerer: str = 'extractive'
    model: str = ''                  # '' = LabSettings.llm_model
    key_facts_judge: bool = False    # LLM check of ground-truth key_facts
    judge_model: str = ''            # the key-facts judge
    ragas_model: str = ''            # RAGAS's own judge, kept separate on purpose


@dataclass(frozen=True)
class LabConfig:
    index: IndexConfig = field(default_factory=IndexConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    label: str = ''

    @classmethod
    def from_dict(cls, data: dict) -> 'LabConfig':
        """Build from the panel's JSON, ignoring unknown keys so a stale browser
        tab cannot crash a run."""
        def pick(kind, payload):
            fields = {f for f in kind.__dataclass_fields__}
            return kind(**{k: v for k, v in (payload or {}).items() if k in fields})
        index = pick(IndexConfig, data.get('index'))
        retrieval = pick(RetrievalConfig, data.get('retrieval'))
        generation = pick(GenerationConfig, data.get('generation'))
        return cls(index=index.normalized(), retrieval=retrieval.normalized(),
                   generation=generation, label=data.get('label', ''))

    def to_dict(self) -> dict:
        return {'index': asdict(self.index), 'retrieval': asdict(self.retrieval),
                'generation': asdict(self.generation), 'label': self.label}

    def validate(self) -> list[str]:
        bad = []
        checks = ((self.index.chunker, CHUNKERS, 'chunker'),
                  (self.index.embedder, EMBEDDERS, 'embedder'),
                  (self.retrieval.retriever, RETRIEVERS, 'retriever'),
                  (self.retrieval.reranker, RERANKERS, 'reranker'),
                  (self.retrieval.grader, GRADERS, 'grader'),
                  (self.generation.answerer, ANSWERERS, 'answerer'))
        for value, allowed, name in checks:
            if value not in allowed:
                bad.append(f'unknown {name}: {value!r} (expected one of '
                           f'{", ".join(allowed)})')
        if self.retrieval.k < 1:
            bad.append('k must be >= 1')
        # A model belongs to exactly one backend. Loading the backend's default
        # instead of the model that was asked for would produce a run labelled
        # with one encoder that measured a different one — the single worst
        # failure a lab can have.
        wanted = self.index.embed_model
        if wanted and self.index.embedder in MODEL_EMBEDDERS:
            from .embedding import EMBED_MODELS
            served_by = {model.id: model.backend for model in EMBED_MODELS}
            backend = served_by.get(wanted)
            if backend and backend != self.index.embedder:
                bad.append(f'{wanted} is served by {backend}, not '
                           f'{self.index.embedder}: set the embedder to '
                           f'{backend} or pick one of its models')
        return bad


# Every knob explains itself, in the panel, next to the control. This lives here
# rather than in the frontend because it describes *these* definitions: a field
# added above without a line below fails test_every_configuration_factor_has_an_
# explainer, so a knob cannot ship unexplained. Keys are '<group>.<field>'; the
# model fields are explained by models.ROLES instead, and 'run.*' describes the
# controls that belong to one run rather than to a configuration.
HELP = {
    'index.chunker': (
        'How a day of chat is cut into the pieces that get embedded. '
        '"fixed" packs 500 characters regardless of meaning; "message" keeps one '
        'message per piece; "turn-pair" keeps a question with its answer; '
        '"session" stores the whole day; "semantic-drift" cuts where the topic '
        'actually changes, which is what the default measures best on.'),
    'index.chunk_chars': (
        'Target size of one piece. Small pieces retrieve precisely but often lose '
        'the sentence that answers the question; large ones keep the answer but '
        'drag unrelated text into the context and dilute precision.'),
    'index.overlap': (
        'Characters repeated between neighbouring pieces, so a sentence sitting '
        'on a boundary is not cut in half. Only the "fixed-overlap" chunker uses '
        'it.'),
    'index.contextual': (
        'Prepend a one-line header — date, mood, storyline — to every chunk '
        'before embedding it (Anthropic call this contextual retrieval). A diary '
        'chunk that says "بهتر شد" is unsearchable without knowing what "it" was. '
        'Built from metadata alone, so it costs no model call and no summary.'),
    'index.embedder': (
        'Turns text into the vector the index is searched by, and the one choice that '
        'decides whether anything else matters. Each option says which languages '
        'it can represent: "ascii-hash" is what the brain ships today and reads '
        'Latin script only, so Farsi embeds to the zero vector — measured at 0.01 '
        'recall, i.e. chance. The other two hash embedders see any script but '
        'only as letters, never as meaning. Two options load a real model, and '
        'they differ in what that costs: "fastembed" runs its own short ONNX list '
        'locally; "sentence-transformers" runs any HuggingFace checkpoint, which '
        'is the only way to reach the Persian-tuned encoders — it needs '
        'the local-embeddings extra and downloads weights. Whichever you pick, '
        'the model below decides the languages.'),
    'index.embed_model': (
        'Which real embedding model the chosen backend loads. This is where Farsi '
        'is won '
        'or lost: the famous ones (bge-small-en, all-MiniLM-L6) are English-only '
        'and will return confident numbers that measure nothing on a Persian '
        'diary, so every entry states its coverage and the multilingual ones are '
        'listed by name. Bigger is not automatically better — e5 is trained for '
        'retrieval while the paraphrase models are trained for similarity — and '
        'the E5 family needs its "query:"/"passage:" prefixes, which the lab '
        'applies for you. Changing this rebuilds the index, because it changes '
        'what is stored.'),
    'retrieval.retriever': (
        '"dense" searches vectors (meaning), "bm25" searches words (exact names, '
        'numbers, rare terms), "hybrid-rrf" runs both and fuses the two rankings '
        'with Reciprocal Rank Fusion. Hybrid wins here because a diary is full of '
        'proper nouns a vector blurs.'),
    'retrieval.k': (
        'How many chunks the answerer finally sees. Raising it finds more evidence '
        'and lowers precision; it is the single knob that moves recall and '
        'precision in opposite directions.'),
    'retrieval.candidates': (
        'How deep each retriever looks before fusion and reranking. Cheap to '
        'raise — nothing reads these yet — and it is what gives the reranker '
        'something to find.'),
    'retrieval.rrf_k': (
        'The constant in Reciprocal Rank Fusion (1/(k+rank)). Higher flattens the '
        'ranking, so agreement between the two retrievers matters more than either '
        'one being confident.'),
    'retrieval.time_filter': (
        'Reads Farsi time words — «آذر», «تابستون», «سه ماه پیش» — as a Jalali '
        'date range and restricts the search to it. Without this, "what happened '
        'in آذر" retrieves the whole year.'),
    'retrieval.multi_query': (
        'Searches several rewrites of the question (stripped of question words, '
        'keywords only) and merges the hits. Rule-based, so it costs nothing: '
        'measured quote recall 0.489 → 0.512 with no model call.'),
    'retrieval.hyde': (
        'Writes a hypothetical diary answer with a model and searches with that '
        'instead of the question, because an answer looks more like the text you '
        'are hunting for than a question does. Costs one LLM call per query.'),
    'retrieval.mmr_lambda': (
        'Maximal Marginal Relevance. At 1.0 the top k are simply the best-scoring '
        'chunks, which on a diary often means five chunks from one afternoon. '
        'Lower it to trade some relevance for spread across days.'),
    'retrieval.reranker': (
        'Re-scores the candidates before the cut to k. "lexical" is free IDF '
        'coverage; "recency" prefers recent entries; "agentic" is the Generative '
        'Agents mix of relevance + recency + importance; "cross-encoder" reads '
        'question and chunk together with a real model; "llm" asks a model to '
        'score each one.'),
    'retrieval.rerank_depth': (
        'How many candidates the reranker actually reads. The reranker is the '
        'expensive stage, so this is the cost dial: depth 20 with k 8 means '
        'twenty chunks scored to choose eight.'),
    'retrieval.recency_half_life_days': (
        'How fast the recency reranker forgets. At 180 days an entry from six '
        'months ago counts half as much as today — right for "how am I doing '
        'lately", wrong for "what happened last summer".'),
    'retrieval.agentic_weights': (
        'The three weights of the agentic reranker: relevance, recency, '
        'importance. Importance comes from the chunk itself (decisions, promises '
        'and dated commitments score higher than small talk).'),
    'retrieval.grader': (
        'The gate that makes abstention possible: chunks scoring below the '
        'threshold are dropped, and if nothing survives the pipeline refuses '
        'instead of answering from noise. "none" means every question gets an '
        'answer, including the ones the diary never mentions.'),
    'retrieval.grade_threshold': (
        'The score a chunk must clear to survive the gate. Measured: the lexical '
        'gate has no usable setting (0.6 caught 6 of 8 unanswerable questions but '
        'wrongly refused 52% of the answerable ones), while an LLM gate at 0.4 '
        'refused 5 of 5 with 3% false refusals.'),
    'retrieval.max_context_chars': (
        'Budget for the assembled context. When it is exceeded whole chunks are '
        'dropped, never truncated — half a diary entry reads as a complete one '
        'and invites an answer from a sentence whose second half changed the '
        'meaning.'),
    'generation.answerer': (
        '"none" measures retrieval alone. "extractive" quotes the longest '
        'sentence from each top chunk — deterministic, free, and honest about '
        'being quoting rather than answering. "llm" actually writes the answer.'),
    'generation.key_facts_judge': (
        'Scores each answer against the ground truth\'s atomic key facts with a '
        'model. The facts are English and the answers Farsi, so no lexical metric '
        'can do this — and it is the metric that exposed generation as the '
        'bottleneck (coverage 0.261 against faithfulness 0.743).'),
    'run.mode': (
        'Where the LLM stages run. "Local (Ollama)" is the lab default — free '
        'and private — and resets every stage to the lab\'s own defaults. '
        '"OpenRouter" switches the backend and presets the full pipeline onto '
        'gpt-5-nano (HyDE, LLM reranker, relevance gate, answerer and both '
        'judges); the relevance gate prefers a purpose-built reranker when '
        'OpenRouter\'s model list verifies one. The embedder stays the local '
        'Persian-tuned encoder either way. Picking a mode overwrites those '
        'stage choices; every knob can still be changed afterwards.'),
    'run.ragas_mode': (
        '"offline" scores the retrieved context against the ground-truth quotes '
        'with string similarity — no model, no key, no variance. "judged" adds '
        'faithfulness, answer relevancy and factual correctness, which need a '
        'model. "off" skips RAGAS.'),
    'run.ragas_limit': (
        'How many questions RAGAS scores, when judged metrics make the full set '
        'too slow or too expensive.'),
    'run.limit': (
        'How many ground-truth questions to score. The subset is taken by '
        'striding, not truncating, so a limit of 10 still covers every question '
        'type instead of ten single-hop ones.'),
    'run.types': (
        'Restrict the run to certain question types — single-hop, multi-hop, '
        'temporal, counting, latest-state, unanswerable, adversarial. The type '
        'breakdown is usually more informative than the headline.'),
    'run.difficulty': 'Restrict the run to easy, medium or hard questions.',
    'run.balance': (
        'How a limited run chooses its questions. "difficulty" takes an equal '
        'share of easy, medium and hard; "stride" spreads across the set as it '
        'is, which means medium — 57 of the 112 — gets about half the sample. '
        'That matters because the four deciding metrics are means over '
        'questions, so a skewed sample measures one band and reports it as the '
        'pipeline. The sweep uses 49 balanced (17/16/16); runs before '
        '2026-07-31 used stride, which is why the setting is recorded on every '
        'row rather than assumed.'),
    'run.workers': (
        'How many questions are scored in parallel. Only worth raising when a '
        'stage calls a model, where wall-clock is dominated by waiting.'),
    'run.label': (
        'What this run is called in the leaderboard. Worth writing: a row named '
        '"semantic-drift" tells you nothing three days later.'),
}
