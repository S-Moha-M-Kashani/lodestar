"""The LLM seam: Settings -> a LangChain chat model, plus the offline fake.

One function, chosen by BRAIN_LLM. Adding a backend means adding a branch here,
never editing a call site — the project's substitutability invariant.

Two backends serve a real model, and they differ only in *where it runs*:
'openrouter' pays a remote API, 'ollama' talks to a model on this machine. Both
are built through `init_chat_model` as OpenAI-compatible endpoints, because
Ollama serves an OpenAI-compatible /v1 — so a local model costs the brain no new
dependency, where `ChatOllama` would have been one. Since the two differ
in nothing but the endpoint and the patience it deserves, `_endpoint` holds that
difference and there is one construction site rather than two: a knob that
applies to both cannot be added to one and forgotten on the other.

A backend that is not OpenAI-compatible is a new `model_provider` string here
(langchain-anthropic, langchain-google-genai, …) rather than a new import.
"""
import os
import shutil
from typing import Any, Optional, Sequence

from dataclasses import replace

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .config import PROVIDER_MODELS, Settings

# A remote API answers in seconds; a local model on a laptop does not. Measured
# on gemma4:e2b as a local judge (2026-07): a single call took ~8s, but under
# three concurrent requests individual calls reached 80–92s — so the 90s that is
# generous for OpenRouter is the exact reason a local judged run lost three of
# its four deciding metrics to TimeoutError. Two constants rather than one
# setting, because the right value is a property of *where the model runs*.
REMOTE_TIMEOUT = 90
LOCAL_TIMEOUT = 600
# The third of the same kind, and here beside the other two for that reason: a
# CLI subscription backend runs behind a whole coding agent, which boots a
# session and may take several model turns of its own before it answers. Neither
# the remote API's patience nor the local daemon's describes that.
CLI_TIMEOUT = 300.0

# The backends that authenticate themselves, through a CLI this machine has
# already logged in to. No key, no base URL, no `init_chat_model` — so they
# leave before `_endpoint` is ever asked where the model lives.
CLI_PROVIDERS = {'claude-cli', 'codex-cli'}
# What a browser may ask for. The CLI backends are in it because a subscription
# is a per-board choice and not only a boot-time one: one brain serves several
# boards, and two people on two boards want two different subscriptions against
# the same endpoint. `served_models` is what stops the picker offering one this
# machine cannot run.
UI_PROVIDERS = {'ollama', 'openrouter'} | CLI_PROVIDERS


def _endpoint(settings: Settings) -> tuple[str, str, int]:
    """Where this backend's model lives, what to authenticate with, how long to
    wait. Raises for anything else — see the no-auto-modes note in make_chat_model."""
    if settings.llm_provider == 'openrouter':
        # `or 'missing'`: init_chat_model refuses to build without a key, but
        # create_app builds the agent at boot — a brain with no key must still
        # start and serve the board tools. The failure lands as a 401 at call
        # time, which is what happens today.
        return (settings.openrouter_base_url,
                settings.openrouter_api_key or 'missing', REMOTE_TIMEOUT)
    if settings.llm_provider == 'ollama':
        # Ollama authenticates nothing, but the client still demands a key, so a
        # placeholder goes on the wire. Deliberately *not* the OpenRouter key:
        # this request leaves for localhost, and a real credential must never be
        # sent somewhere it was not issued for, however harmless the listener.
        return settings.ollama_base_url, 'ollama', LOCAL_TIMEOUT
    # No auto modes (see CLAUDE.md): an unknown backend is an error, not a
    # silent downgrade to openrouter — which is exactly what it used to be. In
    # particular there is no "local if Ollama is up, remote otherwise": that is
    # the old embedder footgun, one config quietly billing an API on whichever
    # machine happens to have the daemon down.
    raise ValueError(f'unknown BRAIN_LLM {settings.llm_provider!r}')


def _cli_binary(backend: str) -> str:
    """Which binary this CLI backend runs, env override applied.

    A branch rather than a lookup table, for the same reason `_endpoint` is one:
    the env var has to appear here as a literal string. `.env.example` is
    checked against the code by scanning for exactly this call
    (`test_config.py::test_env_example_documents_every_variable_the_code_reads`),
    and a name assembled from a table is a name that invariant cannot see — so
    the override would quietly stop being documented.
    """
    if backend == 'claude-cli':
        return os.environ.get('BRAIN_CLAUDE_CLI_BIN', 'claude')
    return os.environ.get('BRAIN_CODEX_CLI_BIN', 'codex')


def _cli_model(settings: Settings, backend: str,
               model: str | None) -> BaseChatModel:
    """Build a CLI-subscription wrapper. The import is local because `llm_cli`
    imports this module for CLI_TIMEOUT.

    Three sources for the model, in the order that keeps the label and the
    answer in agreement: the request's own pick, then `BRAIN_MODEL` — but only
    when the brain is *configured* for this backend, since a slug meant for
    Ollama must not be handed to Claude — then the backend's own default.
    """
    from .llm_cli import ClaudeCliChatModel, CodexCliChatModel

    configured = settings.model if settings.llm_provider == backend else ''
    if backend == 'claude-cli':
        return ClaudeCliChatModel(
            binary=_cli_binary(backend),
            # PROVIDER_MODELS rather than a literal, so the picker's default and
            # `/agent/models`' answer cannot disagree about what will serve.
            model=model or configured or PROVIDER_MODELS['claude-cli'])
    # Deliberately no model, even when one is named: `codex exec` is never
    # passed -m here — the choice made was codex's own default — so carrying a
    # slug on the wrapper would describe a model that did not answer.
    return CodexCliChatModel(binary=_cli_binary(backend))


def cli_backends() -> dict:
    """Which CLI subscriptions this machine can actually run.

    `served_models`' honesty rule, one backend further out. A CLI backend has no
    model list worth probing — the subscription decides that — but whether its
    binary is here is knowable for certain and for nothing. Offering `claude-cli`
    on a machine with no `claude` would fail every turn with no way out of it
    from the UI, which is the failure `served_models` exists to prevent.
    """
    return {backend: shutil.which(_cli_binary(backend)) is not None
            for backend in sorted(CLI_PROVIDERS)}


def make_chat_model(settings: Settings, model: str | None = None,
                    provider: str | None = None) -> BaseChatModel:
    """Build a model for the configured backend or an explicit UI selection.

    A provider is never inferred from model text. Choosing OpenRouter in the
    UI is deliberate (and potentially billed); choosing a local-looking slug
    must not quietly redirect to it.
    """
    # Ahead of the UI selection on purpose: 'fake' is the backend the whole test
    # suite and the e2e board run on, and a browser naming a real provider must
    # not be able to move a brain configured as offline onto a live daemon or a
    # paid API. The offline contract belongs to the server, not to a client
    # deciding when to leave the field out.
    if settings.llm_provider == 'fake':
        return FakeChat()
    if provider is not None and provider not in UI_PROVIDERS:
        raise ValueError(f'unsupported UI provider {provider!r}')
    # A CLI backend leaves here whether it was configured or chosen, and it
    # leaves ahead of everything else: it has no base url and no key, so
    # `_endpoint` has nothing to say about it. Behind `fake` on purpose — a
    # browser naming a live subscription must not move an offline brain onto it.
    backend = provider or settings.llm_provider
    if backend in CLI_PROVIDERS:
        return _cli_model(settings, backend, model)
    if provider is not None:
        # The model has to follow the provider: switching backend in the picker
        # without naming a model must not carry the other backend's slug across.
        settings = replace(settings, llm_provider=provider,
                           model=model or PROVIDER_MODELS[provider])
    base_url, api_key, timeout = _endpoint(settings)
    return init_chat_model(model or settings.model, model_provider='openai',
                           base_url=base_url, api_key=api_key, timeout=timeout)


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

    `cli` answers a different question and therefore travels as its own field:
    not "which models" but "which subscriptions could serve this board at all".
    It is reported whatever the configured backend is, because the picker offers
    a choice rather than a description of the boot flag — and reported even under
    `fake`, where the offline contract in `make_chat_model` is what keeps the
    choice from taking effect.
    """
    answer = {'provider': settings.llm_provider, 'default': settings.model,
              'verified': False, 'models': [], 'cli': cli_backends()}
    if settings.llm_provider != 'ollama':
        return answer
    root = settings.ollama_base_url.rstrip('/').removesuffix('/v1')
    try:
        import httpx
        res = httpx.get(f'{root}/api/tags', timeout=4.0)
        res.raise_for_status()
        tags = sorted(m['name'] for m in res.json().get('models', [])
                      if m.get('name'))
    except Exception as exc:
        # A daemon that is down is a normal state, not an error: say "cannot
        # verify" rather than "serves nothing", which would empty the picker.
        import logging
        logging.getLogger(__name__).warning(
            'ollama tags unreachable at %s (%s); the picker gets no verified '
            'list', root, exc)
        return answer
    return {**answer, 'verified': True, 'models': tags}


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # Some providers return content blocks; only the text parts matter here.
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


class FakeChat(BaseChatModel):
    """Deterministic offline chat model for unit tests, e2e, and CI.

    Scripted mode pops pre-baked messages in order. Heuristic mode (no script):
    - a user message starting with 'add:' yields one create_card tool call,
      then a '... created ...' reply once a ToolMessage is in the transcript;
    - anything else echoes back as 'FAKE: <text>'.

    The 'FAKE: ...' strings and the 'add:' prefix are asserted by
    tests/e2e_test.py — do not change them.

    It is here rather than in the test tree because 'fake' is a *backend*: the
    e2e board and every offline run select it through BRAIN_LLM like any other.
    LangChain's own fakes cannot bind_tools, which create_agent requires.
    """

    script: Optional[list[BaseMessage]] = None

    @property
    def _llm_type(self) -> str:
        return 'fake'

    def bind_tools(self, tools: Sequence, **kwargs: Any) -> 'FakeChat':
        """create_agent binds tools to the model; this fake ignores them and
        decides what to call from the transcript alone."""
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        message = self._next(messages)
        # A fake that reports no usage leaves the whole token-reporting path
        # untestable offline, and the e2e board runs on this backend. Four
        # characters to the token: an estimate, and it only has to be non-zero
        # and additive for the reporting to be exercised.
        if message.usage_metadata is None:
            spent_in = sum(len(_text(m)) for m in messages) // 4
            spent_out = len(_text(message)) // 4
            message = message.model_copy(update={'usage_metadata': {
                'input_tokens': spent_in, 'output_tokens': spent_out,
                'total_tokens': spent_in + spent_out}})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        if self.script:
            return self.script.pop(0)
        last_user = next((m for m in reversed(messages)
                          if isinstance(m, HumanMessage)), None)
        text = _text(last_user).strip() if last_user is not None else ''
        tool_ran = any(isinstance(m, ToolMessage) for m in messages)
        if text.lower().startswith('add:'):
            title = text[4:].strip()
            if not tool_ran:
                return AIMessage(content='', tool_calls=[
                    {'name': 'create_card', 'args': {'title': title},
                     'id': 'fake-1'}])
            return AIMessage(content=f'FAKE: created "{title}"')
        return AIMessage(content=f'FAKE: {text}')
