"""The reputation checker, as a seam of its own.

The agent no longer searches the web, so nothing in a turn consults this
checker. The seam stays: `BRAIN_URL_SAFETY` names a backend, an unknown value
raises, and a checker that cannot answer fails closed rather than saying yes.

Fail-closed, deliberately: a checker that cannot answer is not a checker that
says yes. The default backend refuses to *build* without a key rather than
quietly degrading into no protection.
"""
from __future__ import annotations

import httpx
import pytest

from lodestar_brain.config import Settings
from lodestar_brain.safety import make_url_safety


# This is a configuration invariant: no auto modes, and no silent downgrade.
def test_an_unknown_backend_raises_and_the_default_needs_a_key():
    with pytest.raises(ValueError, match='url safety'):
        make_url_safety('probably-fine', Settings(llm_provider='fake'))

    # The default is the real backend, as with every other seam. Without a key it
    # must stop when someone asks for it: a reputation check that always answers
    # "safe" because it was never configured is worse than none.
    with pytest.raises(ValueError, match='SAFE_BROWSING'):
        make_url_safety('google-safe-browsing',
                        Settings(llm_provider='fake', safe_browsing_key=''))

    # Turning it off is allowed, but only by name.
    assert make_url_safety('off', Settings(llm_provider='fake')) is not None


# This is a unit test.
def test_a_transient_lookup_failure_is_retried_before_failing_closed(monkeypatch):
    """Fail-closed turns a network blip into a refusal, so the lookup is the one
    POST worth retrying: it is an idempotent *question*, not a write. Both
    directions pinned — a 500-then-200 lookup answers, and two 500s still fail
    closed. The retry widens the window, never the verdict."""
    import respx

    from lodestar_brain import net
    from lodestar_brain.safety import GoogleSafeBrowsing

    monkeypatch.setattr(net, 'RETRY_BASE_DELAY', 0.0)
    checker = GoogleSafeBrowsing('a-key')

    with respx.mock:
        route = respx.post(GoogleSafeBrowsing.ENDPOINT).mock(side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={}),   # no matches → safe
            httpx.Response(500),
            httpx.Response(500),
        ])

        assert checker.check('https://example.invalid/routines').safe is True

        verdict = checker.check('https://example.invalid/routines')
        assert verdict.safe is False, 'both attempts down still fails closed'
        assert 'could not be checked' in verdict.reason
        assert route.call_count == 4, 'each lookup made exactly two attempts'
