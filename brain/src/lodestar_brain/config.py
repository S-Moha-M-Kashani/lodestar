"""Env-driven settings. Every swappable module is selected here."""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    model: str = 'openai/gpt-4o-mini'
    llm_provider: str = 'openrouter'   # 'openrouter' | 'fake'
    embedder: str = 'auto'             # 'auto' | 'fastembed' | 'hash'
    board_api_url: str = 'http://127.0.0.1:3000'
    # Chroma persist dir for chat memory; '' = off (direct construction in
    # tests/evals must not touch disk). load_settings pairs it with the board.
    chat_memory_dir: str = ''
    max_agent_steps: int = 8


def _chat_memory_dir_for(board_api_url: str) -> str:
    port = urlparse(board_api_url).port
    return f'chroma/board-{port}' if port else 'chroma/board-default'


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    board_api_url = env.get('BOARD_API_URL', 'http://127.0.0.1:3000')
    return Settings(
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
        model=env.get('BRAIN_MODEL', 'openai/gpt-4o-mini'),
        llm_provider=env.get('BRAIN_LLM', 'openrouter'),
        embedder=env.get('BRAIN_EMBEDDER', 'auto'),
        board_api_url=board_api_url,
        chat_memory_dir=env.get('BRAIN_CHAT_MEMORY_DIR')
                        or _chat_memory_dir_for(board_api_url),
        max_agent_steps=int(env.get('BRAIN_MAX_STEPS', '8')),
    )
