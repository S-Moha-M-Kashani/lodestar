"""Env-driven settings. Every swappable module is selected here."""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

# Chat memory lives on a shared Chroma server, so real and non-real data are
# separated by *database*: only the board on PRODUCTION_BOARD_PORT writes to the
# production one. Everything else — the paired test board, e2e's throwaway
# ports, anything added later — lands in the test database, which can be dropped
# whole without touching real memory.
PRODUCTION_BOARD_PORT = 3000
PRODUCTION_DATABASE = 'lodestar'
NON_PRODUCTION_DATABASE = 'lodestar-test'

# The default chat model per backend. A slug only means something to the backend
# that serves it, so `BRAIN_LLM=ollama` has to move the model too — otherwise
# every chat turn asks the local daemon for a model it cannot load, naming one the
# user never chose. `BRAIN_MODEL` still wins: an explicit model must never be
# replaced by a default, or the answer came from something other than what the
# picker says it did.
PROVIDER_MODELS = {'openrouter': 'openai/gpt-5-nano',
                   'ollama': '4skl/gemma4-e2b-mtp',
                   'fake': 'openai/gpt-5-nano'}

# What a long conversation is allowed to cost, and in which order the two
# defences fire. The argument for these four numbers is in
# `middleware/summarize.py`; they live here because a threshold nobody can move
# without editing code is a threshold nobody moves. Either trigger set to 0
# switches its middleware off entirely.
#
# CLEAR_TOOLS_TOKENS is deliberately half of SUMMARY_TOKENS: dropping tool
# output the model has already read back is cheap and reversible, and summarising
# the conversation is neither, so the cheap one gets first refusal on the
# request. It does not get first refusal on the *trigger* — the summariser counts
# the thread, which the clearing never edits, so half is not a stay of execution.
# Measured 2026-08-13 in tests/evals/test_context_budget.py; the values are still
# a judgement call, and summarize.py says what would settle them.
SUMMARY_TOKENS = 8_000
SUMMARY_KEEP = 20
CLEAR_TOOLS_TOKENS = 4_000
CLEAR_TOOLS_KEEP = 3


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    # '' = the backend's own default (PROVIDER_MODELS), resolved in
    # __post_init__ so every reader sees a concrete slug. It cannot be a literal
    # here: Settings(llm_provider='openrouter') is how the unit tests, the evals
    # and create_app's callers build settings, and a hard-coded slug from one
    # backend under a provider field naming another is the exact mismatch
    # PROVIDER_MODELS exists to prevent.
    model: str = ''
    llm_provider: str = 'ollama'       # 'openrouter' | 'ollama' | 'fake'
    # Where a locally served model lives. Ollama's OpenAI-compatible surface, so
    # the '/v1' is part of the URL rather than something the factory appends —
    # pointing this at any other local OpenAI-compatible server (llama.cpp, vLLM)
    # then needs no code change at all.
    ollama_base_url: str = 'http://localhost:11434/v1'
    # 'sentence-transformers' | 'fastembed' | 'fake'. No 'auto': probing for an
    # optional wheel and silently taking the hash embedder when it was missing
    # meant a machine without the extra ran token-overlap hashing while
    # believing it had embeddings.
    #
    # The default is the *measured* winner, because the embedder is the
    # architecture: hash embedding scored ~0.01 recall on a Farsi diary corpus
    # against 0.617 for heydariAI/persian-embeddings — a ~60× effect, where no
    # other knob measured was worth 2%. It costs the 'local-embeddings'
    # extra and a ~2.2 GB download on first boot. 'fake' is the offline-test
    # value: deterministic *lexical* hashing, never semantic.
    embedder: str = 'sentence-transformers'
    # '' = that backend's own default (retrieval.BACKEND_DEFAULTS). An
    # explicitly named model is never replaced, or the configuration and the
    # model that answered would disagree.
    embed_model: str = ''
    # 'lexical' | 'openrouter' | 'fake' — how the fused candidates are re-ordered
    # before anything reads them (`retrieval/rerank.py`). No 'auto', like every
    # other seam.
    #
    # The default is the *cheap* one, which is the opposite of the embedder's
    # default and for a reason the embedder does not have: that choice was
    # measured (~60× recall) and this one has not been measured at all. Until it
    # is, 'lexical' is what the shipped precision numbers were taken with, and
    # 'openrouter' bills a search per question — including the /rag/recall box,
    # which is built to wait for nothing — and sends a private board's card text
    # to a third party. rerank.py says exactly which run would move this.
    reranker: str = 'lexical'
    # '' = that backend's own default (retrieval.RERANK_MODEL_DEFAULTS ->
    # cohere/rerank-4-fast). Only the hosted backend has a model at all; an
    # explicitly named one is never replaced, as with embed_model.
    rerank_model: str = ''
    # 'llm' | 'none' — the chosen architecture's relevance gate between
    # retrieval and generation. It follows the main chat model, so it needs no
    # model setting of its own; the threshold is the measured one.
    grader: str = 'llm'
    grade_threshold: float = 0.4
    board_api_url: str = 'http://127.0.0.1:3000'
    # Chroma server for chat memory. '' = off, so Settings built directly in
    # code (unit tests, evals) reach no store at all; 'memory' = in-process
    # client; any http(s) url = the real server. load_settings pairs the
    # database and collection with the board.
    chroma_url: str = ''
    chroma_database: str = PRODUCTION_DATABASE
    chat_collection: str = 'chat'
    max_agent_steps: int = 8
    # 'parakeet' | 'openrouter' | 'fake'. No 'auto' either: it picked the local
    # model on Apple Silicon and billed OpenRouter everywhere else from the same
    # config. Default to the free, offline, private backend; Docker pins
    # 'openrouter' because mlx cannot be installed there at all.
    transcriber: str = 'parakeet'
    # Audio/photo/video → text, for the *remote* transcriber only; Parakeet owns
    # its own checkpoint and ignores this. Must be a model OpenRouter serves and
    # one that genuinely *receives* audio: nemotron-3-nano-omni:free advertises
    # audio input but its provider discards the input_audio part, so every
    # dictation came back an invented apology.
    omni_model: str = 'google/gemini-2.5-flash-lite'
    # Local checkpoint for the Parakeet backend (Apple Silicon, MLX).
    parakeet_model: str = 'mlx-community/parakeet-tdt-0.6b-v3'
    # 'google-safe-browsing' | 'fake' | 'off' — where a search result leads is
    # checked before the model may cite it (`safety.py`).
    #
    # Inert here and real in `load_settings`, exactly as `chroma_url` is: a
    # Settings built in code is a test or a script, and one built from the
    # environment is the product. So the *env* default is the real backend, which
    # raises at boot without a key rather than reporting a check that never ran,
    # and `off` is how you say you meant to run without one.
    url_safety: str = 'off'
    safe_browsing_key: str = ''
    # 'langsmith' | 'off' — where a turn's trace goes (`middleware/tracing.py`).
    #
    # Inert here and real in `load_settings`, the same split `url_safety` and
    # `chroma_url` use: a Settings built in code is a test or an eval and must
    # ship nothing anywhere, while one built from the environment is the product
    # and traces. `off` is not merely "no key" — it is a named choice that turns
    # egress off at the source, because a missing key makes langsmith warn and
    # call out anyway.
    tracing: str = 'off'
    langsmith_api_key: str = ''
    # The agent's own working memory: LangGraph's checkpointer (one thread per
    # chat, so reopening a conversation resumes it) and its long-term store,
    # sharing one sqlite file. Never board.db and never assistant.db — those are
    # the *record*, and losing this file costs resume, never a card or a turn.
    #
    # Inert in code and real from the environment, the same split url_safety,
    # tracing and chroma_url use: a Settings built directly is a test, an eval or
    # a script, and none of those may write into databases/real. ':memory:' is
    # per-process and disappears with it.
    checkpoint_db: str = ':memory:'
    # Context budget. Real in code as well as from the environment — unlike
    # tracing or the checkpoint file, these change what the *model* is sent, so a
    # test or an eval that ran with different thresholds than the product would
    # be measuring a different agent. `middleware/summarize.py` argues the
    # numbers; 0 on either trigger switches that middleware off.
    summary_tokens: int = SUMMARY_TOKENS
    summary_keep: int = SUMMARY_KEEP
    clear_tools_tokens: int = CLEAR_TOOLS_TOKENS
    clear_tools_keep: int = CLEAR_TOOLS_KEEP

    def __post_init__(self):
        # An unknown provider gets the remote slug and is rejected by
        # make_chat_model, which is where that error belongs — resolving a model
        # for it here would turn a clear "unknown backend" into a confusing
        # "unknown model". An explicitly named model is never replaced, or the
        # picker's label and the model that answered would disagree.
        if not self.model:
            object.__setattr__(self, 'model', PROVIDER_MODELS.get(
                self.llm_provider, PROVIDER_MODELS['openrouter']))


def _board_port(board_api_url: str) -> int | None:
    return urlparse(board_api_url).port


def _chat_collection_for(board_api_url: str) -> str:
    port = _board_port(board_api_url)
    return f'chat-board-{port}' if port else 'chat-board-default'


def _chroma_database_for(board_api_url: str) -> str:
    if _board_port(board_api_url) == PRODUCTION_BOARD_PORT:
        return PRODUCTION_DATABASE
    return NON_PRODUCTION_DATABASE


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    board_api_url = env.get('BOARD_API_URL', 'http://127.0.0.1:3000')
    provider = env.get('BRAIN_LLM', 'ollama')
    return Settings(
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
        # '' hands the choice to __post_init__, so the env path and a settings
        # object built in code resolve the backend's model by the same rule.
        model=env.get('BRAIN_MODEL', ''),
        llm_provider=provider,
        ollama_base_url=env.get('BRAIN_OLLAMA_BASE_URL',
                                'http://localhost:11434/v1'),
        embedder=env.get('BRAIN_EMBEDDER', 'sentence-transformers'),
        embed_model=env.get('BRAIN_EMBED_MODEL', ''),
        # Real in code as well as from the environment, unlike url_safety or
        # tracing: the reranker changes what the *answerer is given*, so an eval
        # or a test running a different one from the product would be measuring
        # a different pipeline — the same rule the context budget follows.
        reranker=env.get('BRAIN_RERANKER', 'lexical'),
        rerank_model=env.get('BRAIN_RERANK_MODEL', ''),
        grader=env.get('BRAIN_GRADER', 'llm'),
        grade_threshold=float(env.get('BRAIN_GRADE_THRESHOLD', '0.4')),
        board_api_url=board_api_url,
        # The project's own Chroma (compose service `chroma`, npm run chroma) —
        # :8003 because 8001/8002 belong to the unrelated ~/vectordb-lab stack.
        chroma_url=env.get('BRAIN_CHROMA_URL', 'http://localhost:8003'),
        chroma_database=env.get('BRAIN_CHROMA_DATABASE')
                        or _chroma_database_for(board_api_url),
        chat_collection=env.get('BRAIN_CHAT_COLLECTION')
                        or _chat_collection_for(board_api_url),
        max_agent_steps=int(env.get('BRAIN_MAX_STEPS', '8')),
        transcriber=env.get('BRAIN_TRANSCRIBER', 'parakeet'),
        omni_model=env.get('BRAIN_OMNI_MODEL', 'google/gemini-2.5-flash-lite'),
        url_safety=env.get('BRAIN_URL_SAFETY', 'google-safe-browsing'),
        safe_browsing_key=env.get('GOOGLE_SAFE_BROWSING_KEY', ''),
        tracing=env.get('BRAIN_TRACING', 'langsmith'),
        langsmith_api_key=env.get('LANGSMITH_API_KEY', ''),
        # Beside the other real databases, and covered by no backup: it is
        # derived working memory, not a record.
        checkpoint_db=env.get('BRAIN_CHECKPOINT_DB',
                              'databases/real/brain-checkpoints.db'),
        parakeet_model=env.get('BRAIN_PARAKEET_MODEL',
                               'mlx-community/parakeet-tdt-0.6b-v3'),
        summary_tokens=int(env.get('BRAIN_SUMMARY_TOKENS', SUMMARY_TOKENS)),
        summary_keep=int(env.get('BRAIN_SUMMARY_KEEP', SUMMARY_KEEP)),
        clear_tools_tokens=int(env.get('BRAIN_CLEAR_TOOLS_TOKENS',
                                       CLEAR_TOOLS_TOKENS)),
        clear_tools_keep=int(env.get('BRAIN_CLEAR_TOOLS_KEEP',
                                     CLEAR_TOOLS_KEEP)),
    )
