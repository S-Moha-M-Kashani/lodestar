"""The LLM seam: Settings -> a LangChain chat model, plus the offline fake.

The old OpenRouterProvider tests lived here and are gone with it: ChatOpenAI
replaces those 29 lines, and its wire format is langchain-openai's problem to
test, not ours. What is ours is the dispatch, the no-auto-modes rule, and every
string FakeChat is contractually obliged to produce.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from lodestar_brain.config import Settings
from lodestar_brain.llm import (LOCAL_TIMEOUT, REMOTE_TIMEOUT, FakeChat,
                                make_chat_model)


def _settings(**over) -> Settings:
    base = dict(llm_provider='fake', model='openai/gpt-5-nano',
                openrouter_api_key='sk-test',
                openrouter_base_url='https://openrouter.ai/api/v1')
    return Settings(**{**base, **over})


def test_factory_builds_fake_chat():
    assert isinstance(make_chat_model(_settings()), FakeChat)


def test_factory_builds_chat_openai_with_base_url_and_model():
    llm = make_chat_model(_settings(llm_provider='openrouter'))
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == 'openai/gpt-5-nano'


def test_factory_honours_the_per_request_model_override():
    # The Assistant view's model picker sends a model per request, so the
    # factory has to take one that overrides BRAIN_MODEL.
    llm = make_chat_model(_settings(llm_provider='openrouter'),
                          'anthropic/claude-sonnet-4.5')
    assert llm.model_name == 'anthropic/claude-sonnet-4.5'


def test_factory_builds_a_model_even_with_no_api_key():
    # create_app builds the agent at import time; a brain with no key must
    # still boot and serve the board tools. The 401 comes at call time.
    llm = make_chat_model(_settings(llm_provider='openrouter',
                                    openrouter_api_key=''))
    assert isinstance(llm, ChatOpenAI)


def test_factory_builds_ollama_against_the_local_base_url():
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='qwen3.5:2b',
                                    ollama_base_url='http://localhost:11434/v1'))
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == 'qwen3.5:2b'
    assert str(llm.openai_api_base) == 'http://localhost:11434/v1'


def test_factory_never_sends_the_openrouter_key_to_a_local_model():
    # The request leaves for localhost. A real credential must not go somewhere
    # it was not issued for, however harmless the listener — and Ollama
    # authenticates nothing, so there is no reason to send one.
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    openrouter_api_key='sk-real-secret'))
    assert llm.openai_api_key.get_secret_value() == 'ollama'


def test_factory_gives_a_local_model_a_far_longer_timeout():
    # Measured: a local judge under three concurrent requests took 80–92s per
    # call, so the 90s that suits a remote API is the exact reason a judged run
    # lost three of its four deciding metrics to TimeoutError.
    local = make_chat_model(_settings(llm_provider='ollama'))
    remote = make_chat_model(_settings(llm_provider='openrouter'))
    assert local.request_timeout == LOCAL_TIMEOUT
    assert remote.request_timeout == REMOTE_TIMEOUT
    assert LOCAL_TIMEOUT > REMOTE_TIMEOUT


def test_factory_honours_the_per_request_model_override_on_ollama():
    # The lab names the judge's model per stage, and RAGAS binds it at
    # construction — so this override is the only way a judge slug reaches the
    # wire.
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='gemma4:e2b'), 'qwen3.5:2b')
    assert llm.model_name == 'qwen3.5:2b'


def test_factory_can_switch_a_local_default_to_nano_per_request():
    """The UI may opt into a remote model without changing the local default."""
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='4skl/gemma4-e2b-mtp'),
                          'openai/gpt-5-nano', provider='openrouter')
    assert llm.model_name == 'openai/gpt-5-nano'
    assert str(llm.openai_api_base) == 'https://openrouter.ai/api/v1'
    assert llm.request_timeout == REMOTE_TIMEOUT


def test_a_ui_provider_with_no_model_gets_that_providers_own_default():
    """Switching provider in the picker without naming a model must not carry the
    other backend's slug across: it is the same rule BRAIN_LLM already follows."""
    llm = make_chat_model(_settings(llm_provider='ollama',
                                    model='4skl/gemma4-e2b-mtp'),
                          None, provider='openrouter')
    assert llm.model_name == 'openai/gpt-5-nano'


def test_an_unknown_ui_provider_raises_instead_of_falling_back():
    """The no-auto-modes rule reaches the per-request seam too. A browser sending
    a provider this brain cannot serve is a bug to surface, not something to
    quietly serve from whichever backend happens to be configured."""
    with pytest.raises(ValueError):
        make_chat_model(_settings(llm_provider='ollama'), 'x',
                        provider='anthropic')


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


def test_choosing_the_local_backend_is_enough_to_get_a_local_default_model():
    # BRAIN_LLM=ollama on its own has to produce a working brain. The remote
    # default slug is not something the daemon can load, so every chat turn
    # failed with 404 for a model the user never chose.
    from lodestar_brain.config import PROVIDER_MODELS, load_settings
    settings = load_settings({'BRAIN_LLM': 'ollama'})
    assert settings.model == PROVIDER_MODELS['ollama']
    assert make_chat_model(settings).model_name == PROVIDER_MODELS['ollama']


def test_an_explicit_brain_model_survives_the_provider_default():
    from lodestar_brain.config import load_settings
    settings = load_settings({'BRAIN_LLM': 'ollama', 'BRAIN_MODEL': 'gemma4:e2b'})
    assert settings.model == 'gemma4:e2b'


def test_factory_raises_on_unknown_provider():
    # No auto modes (CLAUDE.md): a typo must not silently become openrouter,
    # which is what the old create_app branch did.
    with pytest.raises(ValueError, match='typo'):
        make_chat_model(_settings(llm_provider='typo'))


def test_fake_chat_scripted_pops_turns_in_order():
    llm = FakeChat(script=[AIMessage(content='first'), AIMessage(content='second')])
    assert llm.invoke([HumanMessage(content='x')]).content == 'first'
    assert llm.invoke([HumanMessage(content='x')]).content == 'second'


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


def test_fake_chat_echoes_everything_else():
    # tests/e2e_test.py:955 asserts this exact string.
    llm = FakeChat()
    assert llm.invoke([HumanMessage(content='hello brain')]).content == 'FAKE: hello brain'


def test_fake_chat_accepts_bind_tools():
    # create_agent calls bind_tools on the model; BaseChatModel's default
    # implementation raises, which would break every offline test.
    llm = FakeChat()
    assert llm.bind_tools([]) is llm
