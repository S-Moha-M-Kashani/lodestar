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


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    model: str = 'openai/gpt-5-nano'
    llm_provider: str = 'openrouter'   # 'openrouter' | 'ollama' | 'fake'
    # Where a locally served model lives. Ollama's OpenAI-compatible surface, so
    # the '/v1' is part of the URL rather than something the factory appends —
    # pointing this at any other local OpenAI-compatible server (llama.cpp, vLLM)
    # then needs no code change at all.
    ollama_base_url: str = 'http://localhost:11434/v1'
    # 'fastembed' | 'hash'. No 'auto': probing for the optional fastembed wheel
    # and taking HashEmbedder when it was missing meant a machine without the
    # 'semantic' extra ran token-overlap hashing while believing it had
    # embeddings. Docker pins 'fastembed' (its image installs the extra).
    embedder: str = 'hash'
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
    # Audio/photo/video → text. Matches the Assistant view's omni picker default.
    # Must be a model that genuinely *receives* audio: nemotron-3-nano-omni:free
    # advertises audio input but its provider discards the input_audio part, so
    # every dictation came back an invented apology instead of a transcript.
    omni_model: str = 'google/gemini-2.5-flash-lite'
    # Local checkpoint for the Parakeet backend (Apple Silicon, MLX).
    parakeet_model: str = 'mlx-community/parakeet-tdt-0.6b-v3'


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
    return Settings(
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
        model=env.get('BRAIN_MODEL', 'openai/gpt-5-nano'),
        llm_provider=env.get('BRAIN_LLM', 'openrouter'),
        ollama_base_url=env.get('BRAIN_OLLAMA_BASE_URL',
                                'http://localhost:11434/v1'),
        embedder=env.get('BRAIN_EMBEDDER', 'hash'),
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
