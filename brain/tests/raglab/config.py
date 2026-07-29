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
        cross_encoder_model=env.get('RAGLAB_CROSS_ENCODER',
                                    'jinaai/jina-reranker-v2-base-multilingual'),
    )


CHUNKERS = ('fixed', 'fixed-overlap', 'message', 'turn-pair', 'session',
            'semantic-drift')
EMBEDDERS = ('ascii-hash', 'token-hash', 'char-hash', 'fastembed')
SUMMARIZERS = ('extractive', 'llm')
LAYERS = ('chunk', 'session', 'month', 'thread', 'commitment')
RETRIEVERS = ('dense', 'bm25', 'hybrid-rrf')
RERANKERS = ('none', 'lexical', 'recency', 'agentic', 'cross-encoder', 'llm')
GRADERS = ('none', 'lexical', 'llm')
EXPANSIONS = ('none', 'neighbors', 'session')
ANSWERERS = ('none', 'extractive', 'llm')


@dataclass(frozen=True)
class IndexConfig:
    """What gets written to Chroma. Its fingerprint names the collection."""
    chunker: str = 'semantic-drift'
    chunk_chars: int = 500
    overlap: int = 100          # fixed-overlap only
    contextual: bool = True     # prepend a situating header to every chunk
    embedder: str = 'char-hash'
    summarizer: str = 'extractive'
    layers: tuple[str, ...] = LAYERS

    def normalized(self) -> 'IndexConfig':
        return replace(self, layers=tuple(l for l in LAYERS if l in self.layers))

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
    mmr_lambda: float = 1.0          # 1.0 = pure relevance, lower = diversify
    reranker: str = 'lexical'
    rerank_depth: int = 20
    recency_half_life_days: float = 180.0
    agentic_weights: tuple[float, float, float] = (1.0, 0.3, 0.2)
    grader: str = 'none'             # gate that makes abstention possible
    grade_threshold: float = 0.0
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
        return bad
