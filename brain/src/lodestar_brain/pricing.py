"""What a turn cost, in dollars.

The board already reported tokens, and tokens are not a price: the same 4k turn
is two orders of magnitude dearer on one slug than another, so "1,234 in · 567
out" tells a user everything except the thing they would change their mind over.
This resolves the active model's per-token rates and turns a turn's usage into
money.

Three decisions, all of them about not lying:

**A price is never invented.** Unknown slug, unreachable catalogue, or a model
that reported no usage all return None, and the Assistant then shows no figure at
all. A confident 0.000$ on a paid turn reads as "that was free", which is worse
than silence — it is a measurement nobody made, presented as one.

**A local model's zero is a different fact from a missing price**, which is why
this returns a `Prices` pair rather than a bare float. Ollama has no per-token
bill; that zero is true and gets shown.

**Input and output are billed at their own rates.** Completion tokens cost
several times prompt tokens, so a blended rate against `total_tokens` would be
wrong for every turn, and wrong in the flattering direction for precisely the
turns that cost the most.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# One remote document shared by every slug, so it is fetched once per process and
# reused. Keyed by base url because a test or a proxy can point somewhere else,
# and a cache that ignored that would answer with the wrong provider's prices.
_CACHE: dict[str, dict[str, Prices]] = {}

# Failures are deliberately NOT cached: a price list that was unreachable once
# should be tried again on the next turn, not disabled until the process
# restarts. The cost of retrying is one GET on a turn that already crossed the
# network to a language model.


@dataclass(frozen=True)
class Prices:
    """USD per token, one rate for each direction."""
    prompt: float
    completion: float


def forget_prices() -> None:
    """Drop the cached catalogue. For tests, and for nothing else — a running
    brain has no reason to re-fetch prices that do not change mid-process."""
    _CACHE.clear()


def _catalogue(base_url: str) -> dict[str, Prices] | None:
    """Every slug the provider serves, with its rates. None if it cannot be read.

    OpenRouter publishes this without authentication, so no key is spent and no
    key is needed — which also means the price shown never depends on whose key
    is configured.
    """
    if base_url in _CACHE:
        return _CACHE[base_url]
    try:
        res = httpx.get(f'{base_url.rstrip("/")}/models', timeout=6.0)
        res.raise_for_status()
        rows = res.json().get('data') or []
    except Exception as exc:
        # Warned rather than raised: a chat turn that answered correctly must not
        # fail because the price of it could not be looked up.
        log.warning('model prices unavailable from %s: %s', base_url, exc)
        return None
    catalogue: dict[str, Prices] = {}
    for row in rows:
        slug, rates = row.get('id'), row.get('pricing') or {}
        if not slug:
            continue
        try:
            catalogue[slug] = Prices(float(rates['prompt']),
                                     float(rates['completion']))
        except (KeyError, TypeError, ValueError):
            # A row without usable numbers is a slug with no known price, which
            # is exactly what a missing entry already means. Skipping keeps
            # "unknown" as the single way of not knowing.
            continue
    _CACHE[base_url] = catalogue
    return catalogue


def model_prices(settings, model: str | None = None) -> Prices | None:
    """The rates for the model that answered, or None if they are not known.

    `model` is the slug the request named, because the picker can move between
    models within one conversation and the price of a turn is the price of the
    model that actually served it — not of whatever the brain booted with.
    """
    if settings.llm_provider != 'openrouter':
        # Local and fake backends have no per-token bill. A known zero.
        return Prices(0.0, 0.0)
    catalogue = _catalogue(settings.openrouter_base_url)
    if catalogue is None:
        return None
    return catalogue.get(model or settings.model)


def turn_cost(usage: dict | None, prices: Prices | None) -> float | None:
    """USD for one turn, or None when either half of the sum is missing.

    Not rounded here. How many decimals to show is the reader's question and the
    frontend's answer; rounding at the source would make a session total the sum
    of rounded parts, which drifts.
    """
    if not usage or prices is None:
        return None
    return (usage.get('input_tokens', 0) * prices.prompt
            + usage.get('output_tokens', 0) * prices.completion)


__all__ = ['Prices', 'forget_prices', 'model_prices', 'turn_cost']

"""Alternatives considered
========================

Why is this price table yours?
------------------------------

Because there is no library to import, and the interesting part is not the
transport. OpenRouter's price list is one unauthenticated GET returning
`pricing.prompt` and `pricing.completion` as decimal strings; the code above is
that call plus a dict. What actually needed deciding is what happens when the
answer is missing, and no dependency can decide that.

**Why the obvious option fails.** The obvious option is a table in the source —
half a dozen slugs the picker offers, with their rates. It is offline, instant,
and needs no error path. It is also wrong the first time a provider changes a
price or the picker gains a model, and wrong *silently*: the Assistant keeps
printing dollar figures with total confidence. A stale price is worse than no
price, because nothing about the display says how old it is.

**Why not the framework.** LangChain does carry cost callbacks —
`get_openai_callback` and the community `OpenAICallbackHandler` — with a
hardcoded per-model rate table for OpenAI, which is the stale-table failure
above wearing a dependency. It also does not know about OpenRouter slugs, which
is what this board bills through. `usage_metadata` on the message is the part of
the framework that *is* used here; it is where `_usage_from` gets its tokens.

**The libraries that would do it** (checked 2026-08-03):

- **`tokencost`** — a maintained per-model price table, updated by releases. Good
  for a batch job, still a pinned snapshot at runtime, and OpenRouter's catalogue
  is broader than its coverage.
- **`litellm`** — carries `cost_per_token()` and a large model map, and genuinely
  solves this. Rejected on weight: it is a very large dependency for one GET in a
  service whose whole point is a small, readable seam per backend.
- **`langchain-community`'s OpenAI callback** — already installed, and its rate
  table is OpenAI-only and hardcoded.
- **OpenRouter's own generation endpoint** (`/generation?id=…`) — returns the
  *actual* charge for a specific request rather than a computed estimate. This is
  the better answer and the one to prefer on a greenfield build; it needs the
  request id threaded out of LangChain, which is not exposed today.

**Why they were not adopted.** Decisively: the live catalogue cannot go stale,
and staleness is the only failure mode that hurts here — every alternative
except the generation endpoint ships a snapshot.

**What would change the decision:** a measured gap between this estimate and the
real invoice. Token counts come from the provider and the rates come from the
provider, so the arithmetic should agree, but cached prompts, tool-call overhead
and per-request minimums are all places it could quietly diverge. One month of
turns compared against one OpenRouter statement would settle it; if the estimate
is off by more than a percent or two, thread the request id through and read the
charge instead of computing it.
"""
