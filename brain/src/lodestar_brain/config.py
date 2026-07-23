"""Env-driven settings. Every swappable module is selected here."""
import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str = ''
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    model: str = 'openai/gpt-4o-mini'
    llm_provider: str = 'openrouter'   # 'openrouter' | 'fake'
    embedder: str = 'auto'             # 'auto' | 'fastembed' | 'hash'
    board_api_url: str = 'http://127.0.0.1:3000'
    max_agent_steps: int = 8


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        openrouter_api_key=env.get('OPENROUTER_API_KEY', ''),
        openrouter_base_url=env.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
        model=env.get('BRAIN_MODEL', 'openai/gpt-4o-mini'),
        llm_provider=env.get('BRAIN_LLM', 'openrouter'),
        embedder=env.get('BRAIN_EMBEDDER', 'auto'),
        board_api_url=env.get('BOARD_API_URL', 'http://127.0.0.1:3000'),
        max_agent_steps=int(env.get('BRAIN_MAX_STEPS', '8')),
    )
