"""What a turn cost, in dollars, or nothing at all.

The board already reports tokens. Tokens are not a price: the same 4k turn costs
two orders of magnitude more on one slug than another, and the number the user
actually wants to see before choosing a model is money. So the brain resolves the
active model's per-token prices and puts a `cost` on the turn.

Why the brain and not the browser: the model is chosen per request and the price
list is a remote document, so a client doing this arithmetic would need its own
copy of the price table and its own idea of which model answered. The brain knows
both for certain.

The rule these tests exist to hold: **a price is never invented.** Unknown model,
unreported usage, or a price list that cannot be fetched all yield `None`, and the
Assistant then shows no figure rather than a confident 0.000$ — a bill nobody
measured is worse than no bill, because it reads as "this was free".
"""
from __future__ import annotations

import httpx
import pytest
import respx

from lodestar_brain.config import Settings
from lodestar_brain.pricing import Prices, model_prices, turn_cost

MODELS_URL = 'https://openrouter.ai/api/v1/models'

# Two slugs and a deliberately absurd price gap, because the point of showing
# money rather than tokens is that the gap is the decision.
CATALOGUE = {'data': [
    {'id': 'openai/gpt-5-nano',
     'pricing': {'prompt': '0.00000005', 'completion': '0.0000004'}},
    {'id': 'anthropic/claude-opus-4',
     'pricing': {'prompt': '0.000015', 'completion': '0.000075'}},
]}

USAGE = {'input_tokens': 10_000, 'output_tokens': 2_000, 'total_tokens': 12_000}


@pytest.fixture(autouse=True)
def _no_cache_between_tests():
    """The cache is process-wide on purpose — see the module note — so each test
    starts from empty rather than inheriting whichever test ran first."""
    from lodestar_brain import pricing
    pricing.forget_prices()
    yield
    pricing.forget_prices()


def _openrouter():
    return Settings(llm_provider='openrouter', model='openai/gpt-5-nano')


# This is a unit test.
@respx.mock
def test_prices_are_read_per_token_and_the_list_is_fetched_once():
    """The price list is one remote document shared by every slug.

    Fetched once and reused, asserted by the request count rather than by
    inspecting the cache: a per-turn fetch would put a network round trip on the
    path of every reply, and the failure would be invisible — correct numbers,
    quietly slower chat.
    """
    route = respx.get(MODELS_URL).mock(
        return_value=httpx.Response(200, json=CATALOGUE))
    settings = _openrouter()

    assert model_prices(settings, 'openai/gpt-5-nano') == Prices(5e-8, 4e-7)
    assert model_prices(settings, 'anthropic/claude-opus-4') == Prices(1.5e-5, 7.5e-5)
    assert route.call_count == 1, 'the catalogue is fetched once, not per lookup'

    # A slug the catalogue does not carry is unknown, not free. This is the case
    # a hardcoded table would get wrong every time the picker gained a model.
    assert model_prices(settings, 'someone/unreleased-model') is None


# This is a unit test.
@respx.mock
def test_a_price_is_never_invented():
    """Every way of not knowing, and all of them decline to guess.

    Kept as one test because they are one rule, and it is the rule the feature
    lives or dies by: the Assistant is about to put a dollar figure in front of
    someone, and a fabricated 0.000$ says "that was free" about a turn that was
    not.
    """
    # A local model genuinely has no per-token bill. That is a *known* zero, and
    # distinguishing it from "unknown" is the whole reason this returns Prices
    # rather than a bare float.
    local = model_prices(Settings(llm_provider='ollama', model='gemma3'), 'gemma3')
    assert local == Prices(0.0, 0.0)
    assert turn_cost(USAGE, local) == 0.0

    # A CLI subscription is NOT that known zero, and this is the assert that
    # says so. `claude-cli` spends Claude Max quota and `codex-cli` spends a
    # ChatGPT plan; the turn cost real money, just not per token and not on this
    # invoice. Reporting it as 0.000$ tells the reader the turn was free, which
    # is the single thing this module exists not to do — and the response even
    # carries `total_cost_usd`, so the temptation is concrete.
    #
    # It read as a known zero for as long as the rule was "anything that is not
    # openrouter": the CLI backends arrived under that rule and inherited a
    # price nobody measured. So the zero-bill backends are named now, and
    # everything else the catalogue cannot price says nothing at all.
    for cli in ('claude-cli', 'codex-cli'):
        assert model_prices(Settings(llm_provider=cli)) is None, cli
        assert turn_cost(USAGE, model_prices(Settings(llm_provider=cli))) is None

    # `fake` keeps the known zero it has always had: an offline test turn really
    # did cost nothing, and every e2e assertion on a rendered cost rides on it.
    assert model_prices(Settings(llm_provider='fake', model='x')) == Prices(0.0, 0.0)

    # An unreachable catalogue is not a free turn.
    respx.get(MODELS_URL).mock(return_value=httpx.Response(503))
    assert model_prices(_openrouter(), 'openai/gpt-5-nano') is None

    # Nor is a model that reported no usage, even when the price is known.
    assert turn_cost(None, Prices(5e-8, 4e-7)) is None
    assert turn_cost(USAGE, None) is None


# This is a unit test.
@respx.mock
def test_the_cost_is_input_and_output_billed_at_their_own_rates():
    """Output tokens cost several times what input tokens cost, so one blended
    rate against `total_tokens` would be wrong for every turn — and wrong in the
    flattering direction for exactly the turns that cost the most.

    10_000 in at 5e-8 = 0.0005; 2_000 out at 4e-7 = 0.0008; 0.0013 together.
    """
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=CATALOGUE))
    prices = model_prices(_openrouter(), 'openai/gpt-5-nano')

    assert turn_cost(USAGE, prices) == pytest.approx(0.0013)
    # Not the blended shortcut, which would read 12_000 × 5e-8 = 0.0006.
    assert turn_cost(USAGE, prices) != pytest.approx(0.0006)
