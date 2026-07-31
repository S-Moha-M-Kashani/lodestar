"""The LLM seam: Settings -> a LangChain chat model.

One function, chosen by BRAIN_LLM. Adding a backend means adding a branch here,
never editing a call site — the project's substitutability invariant.

Two backends serve a real model, and they differ only in where it runs:
'openrouter' pays a remote API, 'ollama' talks to a model on this machine. Both
go through ChatOpenAI because Ollama serves an OpenAI-compatible /v1, so a local
model costs the brain no new dependency — and it keeps working under the lab's
`npm run raglab` pin of `langchain-openai<1`, which ChatOllama is not covered by.
langchain-ollama would buy native /api/chat and the keep-alive knob; nothing here
needs either yet, and the day it does, that is one more branch.
"""
from langchain_core.language_models import BaseChatModel

from ..config import Settings
from .fake import FakeChat

# A remote API answers in seconds; a local model on a laptop does not. Measured
# on gemma4:e2b judging this project's RAG lab: a single call took ~8s, but under
# three concurrent requests individual calls reached 80–92s — so the 90s that is
# generous for OpenRouter is the exact reason a local judged run lost three of
# its four deciding metrics to TimeoutError. Two constants rather than one
# setting, because the right value is a property of *where the model runs*.
REMOTE_TIMEOUT = 90
LOCAL_TIMEOUT = 600


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
                          timeout=REMOTE_TIMEOUT)
    if settings.llm_provider == 'ollama':
        from langchain_openai import ChatOpenAI
        # Ollama authenticates nothing, but ChatOpenAI still demands a key, so a
        # placeholder goes on the wire. Deliberately *not* the OpenRouter key:
        # this request leaves for localhost, and a real credential must never be
        # sent somewhere it was not issued for, however harmless the listener.
        return ChatOpenAI(model=model or settings.model,
                          base_url=settings.ollama_base_url,
                          api_key='ollama',
                          timeout=LOCAL_TIMEOUT)
    # No auto modes (see CLAUDE.md): an unknown backend is an error, not a
    # silent downgrade to openrouter — which is exactly what it used to be. In
    # particular there is no "local if Ollama is up, remote otherwise": that is
    # the old embedder footgun, one config quietly billing an API on whichever
    # machine happens to have the daemon down.
    raise ValueError(f'unknown BRAIN_LLM {settings.llm_provider!r}')


def served_models(settings: Settings) -> dict:
    """What the active backend can actually serve, for the model picker.

    The browser sends a model with every chat turn, so a picker offering slugs
    the backend cannot load is a broken Assistant with no way out of it from the
    UI — the exact failure the RETIRED_MODELS comment in app.js describes, where
    delisting a model left it selected for whoever had chosen it.

    `verified` is the honest part. For Ollama the daemon's own tag list is
    authoritative in both directions, so the picker can offer exactly those and
    deselect anything else. For OpenRouter nothing is probed — it is a paid API
    with hundreds of models and a request on every settings render would be
    absurd — so `verified` is False and the frontend's curated list stands.
    """
    if settings.llm_provider != 'ollama':
        return {'provider': settings.llm_provider, 'default': settings.model,
                'verified': False, 'models': []}
    root = settings.ollama_base_url.rstrip('/').removesuffix('/v1')
    try:
        import httpx
        res = httpx.get(f'{root}/api/tags', timeout=4.0)
        res.raise_for_status()
        tags = sorted(m['name'] for m in res.json().get('models', [])
                      if m.get('name'))
    except Exception:
        # A daemon that is down is a normal state, not an error: say "cannot
        # verify" rather than "serves nothing", which would empty the picker.
        return {'provider': 'ollama', 'default': settings.model,
                'verified': False, 'models': []}
    return {'provider': 'ollama', 'default': settings.model,
            'verified': True, 'models': tags}
