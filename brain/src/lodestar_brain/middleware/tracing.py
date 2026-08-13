"""Where a turn's trace goes — a backend seam, and an `off` that is really off.

`BRAIN_TRACING` names a backend like every other seam in this brain: `langsmith`
sends the graph, its tool calls and its token counts to LangSmith, `off` sends
nothing anywhere, and an unknown value raises at boot rather than picking one for
you. There is no `auto`, because "traced or not" is the one property of a run
nobody should have to infer from which machine it happened on.

**Off is a switch, not an absence.** Three verified behaviours of langsmith
0.10.13 mean that leaving the environment alone does not turn tracing off:

- `LANGCHAIN_TRACING_V2=true` silently beats `LANGSMITH_TRACING=false`.
  `tracing_is_enabled` asks for the `TRACING_V2` name first and `get_env_var`
  searches *both* namespaces for it, so the legacy spelling wins across the newer
  one — the suffix resolves before the namespace does.
- Removing the API key does not stop egress. It warns and then builds a client
  that calls out regardless, so an unset key buys a log line, not silence.
- The comparison is a strict `== "true"`, so `LANGSMITH_TRACING=1` reads as
  unset — the shape of a config people write and never see fail.

So `off` calls `langsmith.run_trees.configure(enabled=False)`, which sets the
global fallback `tracing_is_enabled` consults *before* it looks at the
environment at all, and is therefore immune both to a stale shell export and to
the `lru_cache` on `get_env_var` that would otherwise freeze whatever the
environment said the first time anything asked.
"""
from __future__ import annotations

import logging
import os

from langsmith import run_trees

log = logging.getLogger(__name__)

# Written by this module, never read by it. `BRAIN_TRACING` is the knob; these
# are the wire underneath it, and `configure_tracing` overwrites whatever a
# shell export left behind. They are deliberately absent from `.env.example` for
# that reason — setting one by hand configures nothing.
_TRACING_ENV = ('LANGSMITH_TRACING', 'LANGCHAIN_TRACING_V2', 'LANGCHAIN_TRACING')


def configure_tracing(settings) -> None:
    """The seam, called once from the composition root.

    Unknown value raises, and `langsmith` refuses to boot without a key — a
    tracing backend that was named but cannot reach its service would otherwise
    run a whole session believing it is being recorded.
    """
    kind = getattr(settings, 'tracing', 'off')
    if kind == 'off':
        # First, because it outranks every variable below it and needs no
        # cooperation from a cache that may already be warm.
        run_trees.configure(enabled=False)
        for name in _TRACING_ENV:
            os.environ.pop(name, None)
        return
    if kind == 'langsmith':
        key = getattr(settings, 'langsmith_api_key', '')
        if not key:
            raise ValueError(
                'BRAIN_TRACING=langsmith needs LANGSMITH_API_KEY; set it, or '
                'choose BRAIN_TRACING=off to run without sending traces')
        # The key too: a Settings built in code carries one the environment may
        # not, and the client that ships the traces only ever reads the
        # environment.
        os.environ.update({'LANGSMITH_API_KEY': key, 'LANGSMITH_TRACING': 'true'})
        run_trees.configure(enabled=True)
        log.info('tracing: langsmith')
        return
    raise ValueError(f'unknown tracing backend: {kind!r}; expected '
                     "'langsmith' or 'off'")


__all__ = ['configure_tracing']

"""Alternatives considered
========================

**"Why did you write your own tracing switch instead of just setting the
environment variable?"**

Because the environment variable does not answer the question the switch has to
answer. `LANGSMITH_TRACING` says what one variable says; it does not say whether
this process is shipping conversations to a third party, which is the only thing
anybody actually wants to know. This module is nine lines of dispatch around
`langsmith`'s own `configure()` — it does not reimplement tracing, it makes the
*decision* nameable, single-valued and testable, the same way `BRAIN_LLM`,
`BRAIN_EMBEDDER`, `BRAIN_TRANSCRIBER` and `BRAIN_URL_SAFETY` already are.

**Why the obvious option fails.** The obvious option is to document
`LANGSMITH_TRACING=false` and stop. It fails concretely: a developer who once ran
`export LANGCHAIN_TRACING_V2=true` for another project has a shell in which this
board's private chat transcripts leave the machine, and nothing in this repo says
otherwise, because `get_env_var("TRACING_V2")` searches the `LANGSMITH` *and*
`LANGCHAIN` namespaces before `TRACING` is consulted in either. It fails a second
way: `get_env_var` is `lru_cache`d, so a variable set after the first read is a
variable that does nothing, and the failure is silent in the safe direction only
half the time. Deleting the API key does not rescue it either — langsmith warns
and calls out anyway. Every one of those is a failure that produces working
software and unintended egress; none of them raises.

**Why not the framework.** LangChain has no tracing switch of its own — it reads
langsmith's environment through langsmith. langsmith itself supplies the two
mechanisms used here, and both are the right ones: `run_trees.configure()` is the
documented "do this once at startup to configure the global settings in code",
and `tracing_context` is its per-invocation sibling, which is the wrong scope for
a boot decision. This module imports both ideas and adds only the seam contract
the rest of the brain already keeps: a named value, no `auto`, and an unknown
value raising at boot. `LANGSMITH_HIDE_INPUTS`/`LANGSMITH_HIDE_OUTPUTS` are left
entirely to langsmith for exactly that reason — they need no code, so they get
none.

**The libraries that would do it** (checked 2026-08-13):

- **langsmith 0.10.13** — already a hard dependency of `langchain-core`, first
  class support for LangGraph node/tool spans. This is the one in use; the module
  is a wrapper over it, not a replacement.
- **OpenTelemetry, via langsmith's own `LANGSMITH_OTEL_ENABLED`** — same spans
  emitted over OTLP to any collector. Vendor-neutral, and the answer if traces
  ever have to land somewhere other than LangSmith.
- **Langfuse** — self-hostable, so a private board's transcripts can be traced
  without leaving the machine at all. The strongest argument against LangSmith
  here, and it costs a container plus a callback handler.
- **Arize Phoenix** — runs locally too, and is the better *evaluation* surface;
  it overlaps this repo's own eval harness rather than complementing it.
- **W&B Weave / Traceloop** — both fine, both a second vendor account for a
  single-user local app.
- Greenfield, on someone else's budget: OTLP out of langsmith into a local
  collector, with LangSmith as one of several exporters — the seam then names a
  destination rather than a vendor.

**Why they were not adopted.** Decisively: langsmith is already installed, so
every alternative is a *new* dependency and a *new* account bought against
LangSmith's advantage of costing neither. Beyond that, the complaints this
tracing exists to answer — the turn burns tokens, the turn fetches the board
three times — are read off per-call token counts and a span tree, which every
option on the list draws equally well. There is no measurement here that favours
one, so the cheapest correct one wins.

**What would change the decision:** the moment a trace of this board becomes
something that must not leave the machine. The board holds a private life, and
`LANGSMITH_HIDE_INPUTS`/`LANGSMITH_HIDE_OUTPUTS` redact payloads while keeping
shape, latency and token counts — which is deliberately enough to answer both
complaints without shipping a single message body. If that turns out to be too
much anyway, or if hiding payloads is measured to hide the thing that explains a
slow turn, Langfuse in a container is the move, and the seam is already the place
that names it: one more branch, no call site touched.
"""
