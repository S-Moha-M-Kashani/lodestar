"""Lab settings and the three config objects the whole pipeline is driven by.

Splitting the knobs into IndexConfig / RetrievalConfig / GenerationConfig is not
cosmetic: only IndexConfig changes what is stored, so its fingerprint names the
Chroma collection. Retrieval and generation can then be swept for free against
an index that is already built — which is what makes the settings panel usable.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]        # the lodestar repo root
RUNS_DIR = Path(__file__).resolve().parent / '.runs'

# The lab's own Chroma database. Never 'lodestar' (real chat memory) and never
# 'lodestar-test' (the paired test board's memory) — an experiment that drops
# and rebuilds collections 40 times must not be able to touch either.
LAB_DATABASE = 'lodestar-raglab'
FORBIDDEN_DATABASES = ('lodestar',)


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
    chroma_url: str = 'http://localhost:8001'
    chroma_database: str = LAB_DATABASE
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    llm_model: str = 'openai/gpt-5-nano'
    fastembed_model: str = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
    # Its own key, deliberately not the OpenRouter one: OpenRouter serves no
    # /embeddings endpoint, so the chat key cannot stand in here. Absent means the
    # OpenAI embedding models show as NA rather than failing mid-sweep.
    openai_api_key: str = ''
    openai_base_url: str = 'https://api.openai.com/v1'
    # Multilingual on purpose: fastembed's default rerankers (ms-marco-MiniLM,
    # jina-reranker-v1-*-en) are English-only and score Farsi pairs as noise.
    # ~1.1 GB on first use; override with RAGLAB_CROSS_ENCODER.
    cross_encoder_model: str = 'jinaai/jina-reranker-v2-base-multilingual'

    def __post_init__(self):
        if self.chroma_database in FORBIDDEN_DATABASES:
            raise ValueError(
                f'refusing to run the lab against Chroma database '
                f'{self.chroma_database!r}: that is production chat memory')


def load_lab_settings(env: dict | None = None) -> LabSettings:
    load_env_file()
    env = os.environ if env is None else env
    return LabSettings(
        chroma_url=env.get('BRAIN_CHROMA_URL', 'http://localhost:8001'),
        chroma_database=env.get('RAGLAB_CHROMA_DATABASE', LAB_DATABASE),
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL',
                                    'https://openrouter.ai/api/v1'),
        llm_model=env.get('RAGLAB_MODEL', 'openai/gpt-5-nano'),
        fastembed_model=env.get(
            'RAGLAB_FASTEMBED_MODEL',
            'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'),
        openai_api_key=env.get('OPENAI_API_KEY', ''),
        openai_base_url=env.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        cross_encoder_model=env.get('RAGLAB_CROSS_ENCODER',
                                    'jinaai/jina-reranker-v2-base-multilingual'),
    )


CHUNKERS = ('fixed', 'fixed-overlap', 'message', 'turn-pair', 'session',
            'semantic-drift')
# Three of these load a named model: 'fastembed' (its own ONNX list),
# 'sentence-transformers' (any HuggingFace checkpoint — the only way to reach
# Qwen3 and the Persian-tuned encoders) and 'openai' (an API call, no download).
# The hash embedders take no model at all.
EMBEDDERS = ('ascii-hash', 'token-hash', 'char-hash', 'fastembed',
             'sentence-transformers', 'openai')
MODEL_EMBEDDERS = ('fastembed', 'sentence-transformers', 'openai')
SUMMARIZERS = ('extractive', 'llm')
LAYERS = ('chunk', 'session', 'month', 'thread', 'commitment')
RETRIEVERS = ('dense', 'bm25', 'hybrid-rrf')
RERANKERS = ('none', 'lexical', 'recency', 'agentic', 'cross-encoder', 'llm')
GRADERS = ('none', 'lexical', 'llm')
EXPANSIONS = ('none', 'neighbors', 'session')
ANSWERERS = ('none', 'extractive', 'llm')


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
    """What gets written to Chroma. Its fingerprint names the collection."""
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
    summarizer: str = 'extractive'
    summarizer_model: str = ''  # '' = LabSettings.llm_model; see models.ROLES
    layers: tuple[str, ...] = LAYERS

    def normalized(self) -> 'IndexConfig':
        # Models that are not consulted are blanked, because this object's
        # fingerprint names the collection: a model nobody calls must not
        # invalidate an index and cost a 157-session rebuild.
        return replace(self, layers=tuple(l for l in LAYERS if l in self.layers),
                       embed_model=(self.embed_model
                                    if self.embedder in MODEL_EMBEDDERS else ''),
                       summarizer_model=(self.summarizer_model
                                         if self.summarizer == 'llm' else ''))

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
    search_layers: tuple[str, ...] = LAYERS
    rollup_boost: float = 1.0        # >1 favours summary layers over raw chunks
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
    parent_expansion: str = 'none'
    max_context_chars: int = 6000

    def normalized(self) -> 'RetrievalConfig':
        return replace(self,
                       search_layers=tuple(l for l in LAYERS
                                           if l in self.search_layers),
                       agentic_weights=tuple(self.agentic_weights))


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
                  (self.index.summarizer, SUMMARIZERS, 'summarizer'),
                  (self.retrieval.retriever, RETRIEVERS, 'retriever'),
                  (self.retrieval.reranker, RERANKERS, 'reranker'),
                  (self.retrieval.grader, GRADERS, 'grader'),
                  (self.retrieval.parent_expansion, EXPANSIONS, 'parent_expansion'),
                  (self.generation.answerer, ANSWERERS, 'answerer'))
        for value, allowed, name in checks:
            if value not in allowed:
                bad.append(f'unknown {name}: {value!r} (expected one of '
                           f'{", ".join(allowed)})')
        if not self.index.layers:
            bad.append('at least one index layer is required')
        if not self.retrieval.search_layers:
            bad.append('at least one search layer is required')
        missing = set(self.retrieval.search_layers) - set(self.index.layers)
        if missing:
            bad.append(f'searching layers that were never indexed: '
                       f'{", ".join(sorted(missing))}')
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
        'chunk that says "بهتر شد" is unsearchable without knowing what "it" was.'),
    'index.embedder': (
        'Turns text into the vector Chroma searches, and the one choice that '
        'decides whether anything else matters. Each option says which languages '
        'it can represent: "ascii-hash" is what the brain ships today and reads '
        'Latin script only, so Farsi embeds to the zero vector — measured at 0.01 '
        'recall, i.e. chance. The other two hash embedders see any script but '
        'only as letters, never as meaning. Three options load a real model, and '
        'they differ in what that costs: "fastembed" runs its own short ONNX list '
        'locally; "sentence-transformers" runs any HuggingFace checkpoint, which '
        'is the only way to reach the Persian-tuned encoders and Qwen3 — it needs '
        'the local-embeddings extra and downloads weights; "openai" downloads '
        'nothing but calls an API, so it needs OPENAI_API_KEY and spends money '
        'per run. Whichever you pick, the model below decides the languages.'),
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
    'index.summarizer': (
        '"extractive" picks the most informative real sentences out of a session '
        '— free, deterministic, no model. "llm" writes new prose, which reads '
        'better and paraphrases away the exact words a keyword search needs.'),
    'index.layers': (
        'Which rollups get stored beside the raw chunks. Layers are additive on '
        'purpose: raw text always stays, because replacing it with a summary '
        'makes "how many times did I say X" permanently unanswerable.'),
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
    'retrieval.search_layers': (
        'Which layers a query is allowed to hit. Searching only summaries is fast '
        'and vague; searching only raw chunks cannot answer "what happened in '
        'آذر".'),
    'retrieval.rollup_boost': (
        'Multiplies the score of summary layers before the candidate cut, so a '
        'session summary can compete with twenty raw chunks from the same day. '
        'Applied before the cut deliberately: after it, a summary that had not '
        'already survived could never be promoted.'),
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
    'retrieval.parent_expansion': (
        'Small-to-big: retrieve a precise little chunk, then hand the model the '
        'text around it. "neighbors" adds the pieces either side; "session" adds '
        'the rest of that day.'),
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
    'run.workers': (
        'How many questions are scored in parallel. Only worth raising when a '
        'stage calls a model, where wall-clock is dominated by waiting.'),
    'run.label': (
        'What this run is called in the leaderboard. Worth writing: a row named '
        '"semantic-drift" tells you nothing three days later.'),
}
