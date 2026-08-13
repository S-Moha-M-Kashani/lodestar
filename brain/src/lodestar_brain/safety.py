"""Where a link leads, checked before the link is offered.

The guardrail asked for was "no searching for unlawful things". Screening the
*query* is the wrong build: this board holds a private life, so "unlawful
eviction, what are my rights" and "is 6.5 hours of sleep enough" are questions it
exists to answer, and a keyword list that refuses them has protected nobody from
anything. It also says nothing about what comes *back*, which is where the harm
actually is — a malware host does not announce itself in the search terms.

So the check is on the destination, against a reputation service, behind a seam
like every other backend (`BRAIN_URL_SAFETY`). Three implementations: the real
one, a deterministic offline fake for tests, and `off` — named explicitly,
because a board running without the check should be a board somebody chose to run
that way.

**Fail closed.** A checker that cannot answer is not a checker that says yes, so a
lookup that errors is an unsafe verdict. It costs a search that would have worked;
the alternative costs a link nobody vetted while the UI implies one did.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .net import retry

log = logging.getLogger(__name__)

# Anything with a scheme, or a bare host with a plausible TLD. Deliberately
# generous: a false positive here only costs one extra lookup, because a *safe*
# verdict changes nothing about what happens next. Only an unsafe one short
# circuits, so over-detecting is the cheap direction.
_URL_IN_TEXT = re.compile(
    r'https?://\S+|\b(?:[\w-]+\.)+[a-z]{2,}\b', re.IGNORECASE)

NOT_SAFE = 'this website is not safe'


@dataclass(frozen=True)
class Verdict:
    safe: bool
    reason: str = ''


class UrlSafety(Protocol):
    def check(self, url: str) -> Verdict: ...


def host_of(url: str) -> str:
    """The host, whether or not the text carried a scheme. `urlparse` puts a
    bare `example.com/x` entirely in `path`, so a checker that trusted `netloc`
    would silently pass every domain a model mentioned without one."""
    parsed = urlparse(url if '//' in url else f'//{url}')
    return (parsed.netloc or parsed.path.split('/')[0]).split('@')[-1].lower()


def urls_in(text: str) -> list[str]:
    """Every destination named in a query, in order, without repeats."""
    # finditer, not findall: the pattern has a group, so findall would return the
    # group rather than the whole match and lose the scheme.
    seen: dict[str, None] = {}
    for match in _URL_IN_TEXT.finditer(text):
        seen.setdefault(match.group(0).rstrip('.,;:!?)"\''), None)
    return list(seen)


class OffUrlSafety:
    """No checking, by explicit configuration."""

    def check(self, url: str) -> Verdict:
        return Verdict(True, 'url safety checking is switched off')


class FakeUrlSafety:
    """Offline and deterministic: a host carrying `mark` is the unsafe one.

    A scripted verdict rather than a recorded HTTP fixture, because what the
    tests are about is the *consequence* of a verdict — dropped, refused, noted —
    not the wire format of somebody's reputation API.
    """

    def __init__(self, mark: str = 'malware'):
        self.mark = mark

    def check(self, url: str) -> Verdict:
        if self.mark in host_of(url):
            return Verdict(False, f'listed as {self.mark} by the offline checker')
        return Verdict(True)


class GoogleSafeBrowsing:
    """Safe Browsing v4 `threatMatches:find`, one URL per call.

    One URL at a time rather than batched: a search returns five results, the
    call is small, and batching would mean holding a result list to reassemble
    verdicts against — complexity for a saving nobody measured.
    """

    ENDPOINT = 'https://safebrowsing.googleapis.com/v4/threatMatches:find'
    THREATS = ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE',
               'POTENTIALLY_HARMFUL_APPLICATION']

    def __init__(self, key: str, timeout: float = 5.0):
        self._key = key
        self._timeout = timeout

    def check(self, url: str) -> Verdict:
        body = {
            'client': {'clientId': 'lodestar', 'clientVersion': '1'},
            'threatInfo': {
                'threatTypes': self.THREATS,
                'platformTypes': ['ANY_PLATFORM'],
                'threatEntryTypes': ['URL'],
                'threatEntries': [{'url': url}]}}
        # A POST, but an idempotent *question* — so it gets net.retry's one
        # bounded retry. Fail-closed turns a transient blip into a user-visible
        # refusal, which makes this the lookup most worth a second attempt.
        def lookup() -> dict:
            res = httpx.post(self.ENDPOINT, params={'key': self._key},
                             json=body, timeout=self._timeout)
            res.raise_for_status()
            return res.json()

        try:
            matches = retry(lookup).get('matches') or []
        except Exception as exc:
            # Fail closed, and say why: a swallowed lookup would present an
            # unchecked link as a checked one.
            log.warning('safe browsing lookup failed for %s: %s', url, exc)
            return Verdict(False, f'could not be checked ({exc})')
        if matches:
            kinds = ', '.join(sorted({m.get('threatType', '?') for m in matches}))
            return Verdict(False, f'listed by Google Safe Browsing as {kinds}')
        return Verdict(True)


def make_url_safety(kind: str, settings) -> UrlSafety:
    """The seam. Unknown value raises, and the real backend refuses to build
    without its key — a reputation check that always answers "safe" because it
    was never configured is worse than none, since the interface would report a
    check that never happened."""
    if kind == 'off':
        return OffUrlSafety()
    if kind == 'fake':
        return FakeUrlSafety()
    if kind == 'google-safe-browsing':
        if not getattr(settings, 'safe_browsing_key', ''):
            raise ValueError(
                'BRAIN_URL_SAFETY=google-safe-browsing needs '
                'GOOGLE_SAFE_BROWSING_KEY; set it, or choose BRAIN_URL_SAFETY=off '
                'to run without checking where results lead')
        return GoogleSafeBrowsing(settings.safe_browsing_key)
    raise ValueError(f'unknown url safety backend: {kind!r}; expected '
                     "'google-safe-browsing', 'fake' or 'off'")


__all__ = ['NOT_SAFE', 'FakeUrlSafety', 'GoogleSafeBrowsing', 'OffUrlSafety',
           'UrlSafety', 'Verdict', 'host_of', 'make_url_safety', 'urls_in']

"""Alternatives considered
========================

Why is this reputation client yours?
------------------------------------

Because the client is nine lines of `httpx.post` and the decisions worth arguing
about are not in it. `pysafebrowsing` and `safebrowsing-python` wrap the same
endpoint; what neither can decide is whether a failed lookup means yes or no,
whether an unsafe result is dropped or annotated, and whether a query naming one
site gets a verdict or a list of substitutes. Those three answers are the whole
guardrail, and they live at the call site regardless of who owns the transport.

**Why the obvious option fails.** The obvious option is the local database: Safe
Browsing's Update API ships hash prefixes you match offline, which is what
browsers do, and `google-safebrowsing` clients implement it. It is genuinely
better for a browser — thousands of lookups, no per-request latency, no URL
leaving the machine. Here it is worse on the one axis that matters: it wants
periodic downloads and a local store to stay fresh, and a stale local list on a
laptop that was closed for a fortnight answers "safe" with total confidence. The
Lookup API costs one round trip per result on a board that searches a handful of
times a day, and it is never stale.

**Why not the framework.** LangChain has no notion of a link being unsafe — its
web tools return what the provider returned. `SearchProvider` is ours already, so
this composes as a filter over it rather than a new integration. There was no
wrapper to import.

**The libraries that would do it** (checked 2026-08-03):

- **`pysafebrowsing`** — thin Lookup API wrapper, exactly this call. Adds a
  dependency to save nine lines and does not answer the fail-open question.
- **`google-safebrowsing`** — the full Update API with the local database.
  Correct for a browser, stale-by-default for an occasionally-run laptop app.
- **VirusTotal / urlscan.io** — richer verdicts from many engines, but rate limits
  that a per-result check would hit, and they ingest submitted URLs, which for a
  private board means the user's browsing intent leaves the machine twice.
- **Cloudflare / Quad9 DNS filtering** — free, no key, no code: point the resolver
  and unsafe hosts stop resolving. Genuinely the best answer for a *machine*, and
  useless as a *product* feature, because the agent cannot tell the user why a
  link vanished.
- Greenfield, on someone else's budget: Safe Browsing's Update API with a
  freshness assertion, so a stale list refuses rather than reassures.

**Why they were not adopted.** Decisively: none of them removes the need for the
three call-site decisions above, and the transport they replace is trivial.

**What would change the decision:** a measured false-negative rate. Nobody here
knows how much of what this board would surface is actually flagged by Safe
Browsing — plausibly almost none of it, in which case this guardrail is theatre
against the real risk, which is a *plausible* page with bad advice rather than a
*malicious* one. The eval that would settle it is a corpus of links this board's
own searches return, scored for how many verdicts differ from `off`. If that
number is zero, the honest move is to say so in the UI rather than imply a
protection that never fires.
"""
