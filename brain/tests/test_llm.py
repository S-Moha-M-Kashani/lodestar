"""The LLM seam: Settings -> a LangChain chat model, plus the offline fake.

The old OpenRouterProvider tests lived here and are gone with it: ChatOpenAI
replaces those 29 lines, and its wire format is langchain-openai's problem to
test, not ours. What is ours is the dispatch, the no-auto-modes rule, and every
string FakeChat is contractually obliged to produce.
"""
import httpx
import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from lodestar_brain.config import Settings
from lodestar_brain.llm import (LOCAL_TIMEOUT, REMOTE_TIMEOUT, FakeChat,
                                make_chat_model, served_models)


def _settings(**over) -> Settings:
    base = dict(llm_provider='fake', model='openai/gpt-5-nano',
                openrouter_api_key='sk-test',
                openrouter_base_url='https://openrouter.ai/api/v1')
    return Settings(**{**base, **over})


# This is a unit test.
def test_factory_builds_fake_chat():
    assert isinstance(make_chat_model(_settings()), FakeChat)


# This is a unit test.
def test_factory_builds_chat_openai_with_base_url_and_model():
    llm = make_chat_model(_settings(llm_provider='openrouter'))
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == 'openai/gpt-5-nano'


# This is a unit test.
def test_factory_honours_the_per_request_model_override():
    # The Assistant view's model picker sends a model per request, so the
    # factory has to take one that overrides BRAIN_MODEL.
    llm = make_chat_model(_settings(llm_provider='openrouter'),
                          'anthropic/claude-sonnet-4.5')
    assert llm.model_name == 'anthropic/claude-sonnet-4.5'


# This is a unit test.
def test_factory_builds_a_model_even_with_no_api_key():
    # create_app builds the agent at import time; a brain with no key must
    # still boot and serve the board tools. The 401 comes at call time.
    llm = make_chat_model(_settings(llm_provider='openrouter',
                                    openrouter_api_key=''))
    assert isinstance(llm, ChatOpenAI)


# This is a unit test.
def test_factory_builds_ollama_against_the_local_base_url():
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='qwen3.5:2b',
                                    ollama_base_url='http://localhost:11434/v1'))
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == 'qwen3.5:2b'
    assert str(llm.openai_api_base) == 'http://localhost:11434/v1'


# This is a unit test.
def test_factory_never_sends_the_openrouter_key_to_a_local_model():
    # The request leaves for localhost. A real credential must not go somewhere
    # it was not issued for, however harmless the listener — and Ollama
    # authenticates nothing, so there is no reason to send one.
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    openrouter_api_key='sk-real-secret'))
    assert llm.openai_api_key.get_secret_value() == 'ollama'


# This is a unit test.
def test_factory_gives_a_local_model_a_far_longer_timeout():
    # Measured: a local judge under three concurrent requests took 80–92s per
    # call, so the 90s that suits a remote API is the exact reason a judged run
    # lost three of its four deciding metrics to TimeoutError.
    local = make_chat_model(_settings(llm_provider='ollama'))
    remote = make_chat_model(_settings(llm_provider='openrouter'))
    assert local.request_timeout == LOCAL_TIMEOUT
    assert remote.request_timeout == REMOTE_TIMEOUT
    assert LOCAL_TIMEOUT > REMOTE_TIMEOUT


# This is a unit test.
def test_factory_honours_the_per_request_model_override_on_ollama():
    # A judged run names the judge's model per stage and binds it at
    # construction — so this override is the only way a judge slug reaches the
    # wire.
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='gemma4:e2b'), 'qwen3.5:2b')
    assert llm.model_name == 'qwen3.5:2b'


# This is a unit test.
def test_factory_can_switch_a_local_default_to_nano_per_request():
    """The UI may opt into a remote model without changing the local default."""
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='4skl/gemma4-e2b-mtp'),
                          'openai/gpt-5-nano', provider='openrouter')
    assert llm.model_name == 'openai/gpt-5-nano'
    assert str(llm.openai_api_base) == 'https://openrouter.ai/api/v1'
    assert llm.request_timeout == REMOTE_TIMEOUT


# This is a unit test.
def test_a_ui_provider_with_no_model_gets_that_providers_own_default():
    """Switching provider in the picker without naming a model must not carry the
    other backend's slug across: it is the same rule BRAIN_LLM already follows."""
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='4skl/gemma4-e2b-mtp'),
                          None, provider='openrouter')
    assert llm.model_name == 'openai/gpt-5-nano'


# This is a unit test.
def test_an_unknown_ui_provider_raises_instead_of_falling_back():
    """The no-auto-modes rule reaches the per-request seam too. A browser sending
    a provider this brain cannot serve is a bug to surface, not something to
    quietly serve from whichever backend happens to be configured."""
    with pytest.raises(ValueError):
        make_chat_model(_settings(llm_provider='ollama'), 'x',
                        provider='anthropic')


# This is a unit test.
def test_the_fake_backend_ignores_a_ui_provider_so_offline_stays_offline():
    """`fake` is the backend the whole suite and the e2e board run on. A browser
    naming 'ollama' must not move a brain configured as fake onto a real daemon.
    The guard lives here, where the configured backend is known, rather than in
    the browser choosing when to omit the field — a client cannot be responsible
    for protecting the server's own offline contract."""
    assert isinstance(make_chat_model(_settings(llm_provider='fake'),
                                      'anything', provider='ollama'), FakeChat)
    assert isinstance(make_chat_model(_settings(llm_provider='fake'),
                                      'anything', provider='openrouter'), FakeChat)


# This is a unit test.
def test_choosing_the_local_backend_is_enough_to_get_a_local_default_model():
    # BRAIN_LLM=ollama on its own has to produce a working brain. The remote
    # default slug is not something the daemon can load, so every chat turn
    # failed with 404 for a model the user never chose.
    from lodestar_brain.config import PROVIDER_MODELS, load_settings
    settings = load_settings({'BRAIN_LLM': 'ollama'})
    assert settings.model == PROVIDER_MODELS['ollama']
    assert make_chat_model(settings).model_name == PROVIDER_MODELS['ollama']


# This is a unit test.
def test_an_explicit_brain_model_survives_the_provider_default():
    from lodestar_brain.config import load_settings
    settings = load_settings({'BRAIN_LLM': 'ollama', 'BRAIN_MODEL': 'gemma4:e2b'})
    assert settings.model == 'gemma4:e2b'


# This is a unit test.
def test_factory_raises_on_unknown_provider():
    # No auto modes, by repository rule: a typo must not silently become openrouter,
    # which is what the old create_app branch did.
    with pytest.raises(ValueError, match='typo'):
        make_chat_model(_settings(llm_provider='typo'))


# This is a unit test.
def test_fake_chat_scripted_pops_turns_in_order():
    llm = FakeChat(script=[AIMessage(content='first'), AIMessage(content='second')])
    assert llm.invoke([HumanMessage(content='x')]).content == 'first'
    assert llm.invoke([HumanMessage(content='x')]).content == 'second'


# This is a unit test.
def test_fake_chat_add_heuristic_calls_create_card_then_replies():
    # tests/e2e_test.py:961,1001 drive this path through the real UI.
    llm = FakeChat()
    msgs = [HumanMessage(content='add: What is Leiden clustering?')]
    turn = llm.invoke(msgs)
    assert turn.tool_calls
    assert turn.tool_calls[0]['name'] == 'create_card'
    assert turn.tool_calls[0]['args'] == {'title': 'What is Leiden clustering?'}
    # once the tool result is in the transcript it must produce a final reply
    msgs += [turn, ToolMessage(content='{}', tool_call_id=turn.tool_calls[0]['id'])]
    assert llm.invoke(msgs).content == 'FAKE: created "What is Leiden clustering?"'


# This is a unit test.
def test_fake_chat_echoes_everything_else():
    # tests/e2e_test.py:955 asserts this exact string.
    llm = FakeChat()
    assert llm.invoke([HumanMessage(content='hello brain')]).content == 'FAKE: hello brain'


# This is a unit test.
def test_fake_chat_accepts_bind_tools():
    # create_agent calls bind_tools on the model; BaseChatModel's default
    # implementation raises, which would break every offline test.
    llm = FakeChat()
    assert llm.bind_tools([]) is llm


# This is a unit test.
@respx.mock
def test_an_unreachable_ollama_daemon_is_logged_not_silent(caplog):
    """`served_models` fails open by design — a daemon that is down empties
    nothing — but it used to fail silently, so a misconfigured base URL and a
    stopped daemon produced identical output with nothing in the log. The
    degradation stays; it now leaves one warning naming the root it tried."""
    respx.get('http://ollama.invalid/api/tags').mock(
        return_value=httpx.Response(500))

    with caplog.at_level('WARNING', logger='lodestar_brain.llm'):
        out = served_models(_settings(
            llm_provider='ollama', ollama_base_url='http://ollama.invalid/v1'))

    assert out['verified'] is False and out['models'] == []
    assert 'ollama.invalid' in caplog.text, 'the warning names the root it tried'


# This is a unit test.
def test_the_browser_can_choose_a_cli_backend_and_gets_that_cli():
    """A CLI subscription is a per-board choice, not only a boot-time one.

    Until now `claude-cli` and `codex-cli` were reachable through `BRAIN_LLM`
    alone, so one brain served one backend to every board it had. Two people on
    two boards want two different subscriptions against the same endpoint, and
    the picker is where that choice belongs — the seam OpenRouter and Ollama
    already travel through.

    Everything the UI path already guarantees is asserted here too, because a
    new provider in `UI_PROVIDERS` is a new way to reach it: the model follows
    the provider (a CLI must never inherit an OpenRouter slug), the offline
    contract still belongs to the server, and an unknown provider still raises.
    """
    from lodestar_brain.config import PROVIDER_MODELS
    from lodestar_brain.llm_cli import ClaudeCliChatModel, CodexCliChatModel

    llm = make_chat_model(_settings(llm_provider='ollama'), None,
                          provider='claude-cli')
    assert isinstance(llm, ClaudeCliChatModel)
    # Naming no model gets that backend's own default, never the one the brain
    # booted with — PROVIDER_MODELS is the rule, and it applies per request.
    assert llm.model == PROVIDER_MODELS['claude-cli']

    # Codex deliberately names no model: the owner's choice is "whatever codex
    # defaults to", so the picker must not smuggle a slug in behind it.
    codex = make_chat_model(_settings(llm_provider='openrouter'), None,
                            provider='codex-cli')
    assert isinstance(codex, CodexCliChatModel) and codex.model == ''

    # An explicit model still wins, exactly as it does for the API backends.
    assert make_chat_model(_settings(llm_provider='ollama'), 'opus',
                           provider='claude-cli').model == 'opus'

    # The offline contract is the server's, not the client's: a browser naming a
    # live subscription must not move a brain configured as fake onto it.
    assert isinstance(make_chat_model(_settings(llm_provider='fake'), None,
                                      provider='claude-cli'), FakeChat)
    # And no auto modes — a CLI this brain cannot serve is an error, not the
    # nearest one it can.
    with pytest.raises(ValueError):
        make_chat_model(_settings(llm_provider='ollama'), None,
                        provider='gemini-cli')


# This is a unit test.
def test_served_models_says_which_cli_backends_this_machine_can_serve(
        tmp_path, monkeypatch):
    """The picker may only offer a subscription that is installed here.

    `verified`'s rule, one backend further out. A CLI backend has no model list
    worth probing — the subscription decides that — but it does have a binary,
    and whether the binary is there is knowable for certain and for free.
    Offering `claude-cli` on a machine with no `claude` would fail every turn
    with no way out from the UI, which is the failure `served_models` exists to
    prevent.

    Both halves are asserted in one run, because the bug worth catching is a
    check that answers the same way whether the binary is there or not.
    """
    installed = tmp_path / 'claude'
    installed.write_text('#!/bin/sh\nexit 0\n')
    installed.chmod(0o755)
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', str(installed))
    monkeypatch.setenv('BRAIN_CODEX_CLI_BIN', str(tmp_path / 'no-such-codex'))

    out = served_models(_settings(llm_provider='fake'))

    assert out['cli'] == {'claude-cli': True, 'codex-cli': False}
    # The existing answer is untouched: `cli` is an addition, and a fake brain
    # still claims to verify nothing. Two questions, two answers, neither of
    # them something the frontend has to disentangle from the other.
    assert out['provider'] == 'fake' and out['verified'] is False
