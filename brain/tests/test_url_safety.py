"""Where a search result leads is checked before it is offered.

Asked for a guardrail on unlawful material, the obvious build is a keyword screen
on the *query* — and it is the wrong one twice over. It refuses the user's own
legitimate questions (this board holds cards about health, money and a marriage;
"unlawful eviction, what are my rights" is a question it must answer), and it
says nothing about what comes back, which is where the actual harm is.

So the check is on the **destination**, against a reputation API, and it is a seam
like every other backend: `BRAIN_URL_SAFETY` names it, an unknown value raises at
boot, and there is no `auto`. Two consequences the tests below pin down:

- an unsafe result is dropped, and the safe ones are the alternative — the model
  answers from what is left rather than being handed a link and a warning;
- a query naming one specific site is answered about that site: **"this website is
  not safe"**, with no search performed, because "check this URL for me" deserves
  a verdict rather than a list of substitutes.

Fail-closed, deliberately: a checker that cannot answer is not a checker that says
yes. That is why the default backend refuses to *build* without a key rather than
quietly degrading into no protection — the failure this project keeps having is
the silent downgrade, not the loud stop.
"""
from __future__ import annotations

import httpx
import pytest

from lodestar_brain.config import Settings
from lodestar_brain.safety import make_url_safety
from lodestar_brain.tools.websearch import make_search_tool

# Whatever the fake is asked about, a host carrying this word is the unsafe one.
# A scripted set rather than a real lookup: offline, and the decision under test
# is what the tool does with a verdict, not how the verdict was reached.
BAD = 'malware-example.invalid'
GOOD = 'example.invalid'


class StubSearch:
    """Returns whatever it was handed, so a test can plant a bad link."""

    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, max_results=5):
        self.queries.append(query)
        return list(self.results)


def _rows():
    return [{'title': 'Free downloads', 'url': f'https://{BAD}/get',
             'snippet': 'click here'},
            {'title': 'Morning routines', 'url': f'https://{GOOD}/routines',
             'snippet': 'cortisol peaks after waking'}]


def _safety():
    return make_url_safety('fake', Settings(llm_provider='fake'))


# This is a configuration invariant: no auto modes, and no silent downgrade.
def test_an_unknown_backend_raises_and_the_default_needs_a_key():
    with pytest.raises(ValueError, match='url safety'):
        make_url_safety('probably-fine', Settings(llm_provider='fake'))

    # The default is the real backend, as with every other seam. Without a key it
    # must stop at boot: a reputation check that always answers "safe" because it
    # was never configured is worse than none, since the UI would report a check
    # that did not happen.
    with pytest.raises(ValueError, match='SAFE_BROWSING'):
        make_url_safety('google-safe-browsing',
                        Settings(llm_provider='fake', safe_browsing_key=''))

    # Turning it off is allowed, but only by name.
    assert make_url_safety('off', Settings(llm_provider='fake')) is not None


# This is a unit test.
def test_an_unsafe_result_is_dropped_and_the_safe_ones_are_the_alternative():
    provider = StubSearch(_rows())
    tool = make_search_tool(provider, safety=_safety())

    rows = tool.run({'query': 'morning routines'})

    assert [r['url'] for r in rows] == [f'https://{GOOD}/routines']
    assert provider.queries == ['morning routines'], 'the search still ran'


# This is a unit test.
def test_a_query_naming_an_unsafe_site_gets_a_verdict_not_a_search():
    """"Is malware-example.invalid safe?" is a question about that site.

    Answering it with a list of other sites would be answering something else, so
    the tool reports the verdict and never calls the provider — which also means
    the user's insistence costs no request to a place already known to be bad.
    """
    provider = StubSearch(_rows())
    tool = make_search_tool(provider, safety=_safety())

    rows = tool.run({'query': f'is https://{BAD}/get safe to download from?'})

    assert len(rows) == 1
    assert rows[0]['unsafe'] is True
    assert 'not safe' in rows[0]['note']
    assert BAD in rows[0]['url']
    assert provider.queries == [], 'a known-bad destination is not searched for'


# This is a unit test.
def test_every_result_being_unsafe_leaves_a_note_rather_than_silence():
    """An empty list reads as "nothing was written about this", which is a
    different and misleading fact. The model needs to be able to say why it has
    nothing to offer."""
    provider = StubSearch([r for r in _rows() if BAD in r['url']])
    tool = make_search_tool(provider, safety=_safety())

    rows = tool.run({'query': 'free downloads'})

    assert len(rows) == 1 and rows[0]['unsafe'] is True
    assert 'not safe' in rows[0]['note']


# This is a unit test.
def test_the_off_backend_passes_everything_through():
    """Named explicitly, so a board running without the check is a board someone
    chose to run that way."""
    provider = StubSearch(_rows())
    tool = make_search_tool(
        provider, safety=make_url_safety('off', Settings(llm_provider='fake')))

    assert len(tool.run({'query': 'morning routines'})) == 2


# This is a unit test.
def test_a_transient_lookup_failure_is_retried_before_failing_closed(monkeypatch):
    """Fail-closed turns a network blip into a user-visible refusal, so the
    lookup is the one POST worth retrying: it is an idempotent *question*, not a
    write. Both directions pinned — a 500-then-200 lookup answers, and two 500s
    still fail closed. The retry widens the window, never the verdict."""
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

        assert checker.check(f'https://{GOOD}/routines').safe is True

        verdict = checker.check(f'https://{GOOD}/routines')
        assert verdict.safe is False, 'both attempts down still fails closed'
        assert 'could not be checked' in verdict.reason
        assert route.call_count == 4, 'each lookup made exactly two attempts'


# This is a unit test.
def test_a_tool_built_without_a_checker_still_works():
    """`make_search_tool(provider)` keeps its one-argument form, so the eval
    harness is not forced to construct a checker it has no opinion about."""
    assert len(make_search_tool(StubSearch(_rows())).run(
        {'query': 'morning routines'})) == 2
