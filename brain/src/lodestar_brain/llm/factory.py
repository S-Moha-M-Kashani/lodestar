"""The LLM seam: Settings -> a LangChain chat model.

One function, chosen by BRAIN_LLM. Adding a backend (Ollama next: ChatOllama
plus the langchain-ollama dependency) means adding a branch here, never editing
a call site — the project's substitutability invariant.
"""
from langchain_core.language_models import BaseChatModel

from ..config import Settings
from .fake import FakeChat


def make_chat_model(settings: Settings, model: str | None = None) -> BaseChatModel:
    if settings.llm_provider == 'fake':
        return FakeChat()
    if settings.llm_provider == 'openrouter':
        from langchain_openai import ChatOpenAI
        # `or 'missing'`: ChatOpenAI refuses to construct without a key, but
        # create_app builds the agent at import time — a brain with no key must
        # still boot and serve the board tools. The failure lands as a 401 at
        # call time, which is what happens today.
        return ChatOpenAI(model=model or settings.model,
                          base_url=settings.openrouter_base_url,
                          api_key=settings.openrouter_api_key or 'missing',
                          timeout=90)
    # No auto modes (see CLAUDE.md): an unknown backend is an error, not a
    # silent downgrade to openrouter — which is exactly what it used to be.
    raise ValueError(f'unknown BRAIN_LLM {settings.llm_provider!r}')
