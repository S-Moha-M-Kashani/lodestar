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
    # architecture: hash embedding scored ~0.01 recall on the lab's Farsi corpus
    # against 0.617 for heydariAI/persian-embeddings — a ~60× effect, where no
    # other knob in the sweep was worth 2%. It costs the 'local-embeddings'
    # extra and a ~2.2 GB download on first boot. 'fake' is the offline-test
    # value: deterministic *lexical* hashing, never semantic.
    embedder: str = 'sentence-transformers'
    # '' = that backend's own default (retrieval.BACKEND_DEFAULTS). An
    # explicitly named model is never replaced, or the configuration and the
    # model that answered would disagree.
    embed_model: str = ''
    # 'llm' | 'none' — candidate F's relevance gate between retrieval and
    # generation. It follows the main chat model, so it needs no model setting
    # of its own; the threshold is the one the lab measured.
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
    # dictation came back an invented apology. openai/whisper-* fails earlier
    # still — measured 2026-07-31, OpenRouter's catalogue holds no whisper entry.
    omni_model: str = 'google/gemini-2.5-flash-lite'
    # Local checkpoint for the Parakeet backend (Apple Silicon, MLX).
    parakeet_model: str = 'mlx-community/parakeet-tdt-0.6b-v3'

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
        grader=env.get('BRAIN_GRADER', 'llm'),
        grade_threshold=float(env.get('BRAIN_GRADE_THRESHOLD', '0.4')),
        board_api_url=board_api_url,
        chroma_url=env.get('BRAIN_CHROMA_URL', 'http://localhost:8001'),
        chroma_database=env.get('BRAIN_CHROMA_DATABASE')
                        or _chroma_database_for(board_api_url),
        chat_collection=env.get('BRAIN_CHAT_COLLECTION')
                        or _chat_collection_for(board_api_url),
        max_agent_steps=int(env.get('BRAIN_MAX_STEPS', '8')),
        transcriber=env.get('BRAIN_TRANSCRIBER', 'parakeet'),
        omni_model=env.get('BRAIN_OMNI_MODEL', 'google/gemini-2.5-flash-lite'),
        parakeet_model=env.get('BRAIN_PARAKEET_MODEL',
                               'mlx-community/parakeet-tdt-0.6b-v3'),
    )
