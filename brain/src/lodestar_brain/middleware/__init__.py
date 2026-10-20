"""Cross-cutting behaviour the agent graph wears, rather than each tool.

One place per rule, so a tool added next year cannot forget it. `untrusted`
fences every tool result on its way back to the model and `errors` turns a
raising tool into something the model can read. Their order is load-bearing
and is written where the graph is built, not here. `tracing` is the odd
member: it is a boot-time composition-root call rather than an
`AgentMiddleware`, and it lives here because it is the same kind of thing — a
decision about the whole turn that belongs to no single tool.

The result cache, the long-term memory injection and the summariser were
Widget 1's. They were dropped with it (stage 3 recorded each reason) and are
not imported here: an eager import would keep a deleted module alive.
"""
from langchain.agents.middleware import AgentMiddleware

from .tracing import configure_tracing
from .untrusted import (BEGIN, END, PROMPT_RULE, UntrustedToolOutput, decode,
                        fence, result_of)


def count_hooks(middleware: list, hook: str) -> int:
    """How many of these middlewares become a graph node at `hook`.

    The test `create_agent` itself applies, restated: a hook counts when the
    class overrides either the sync or the async form. Asking the instances
    rather than hard-coding a number means a middleware added later is counted
    without anyone remembering to.

    Here rather than in the agent, because it is a fact about middleware and
    not about the agent. `recursion_limit` counts *nodes*, so a middleware with
    a state hook silently shortens the tool budget: adding two of them once cut
    a `max_steps=2` run to one tool call, which reads as the model having got
    worse. The agent derives its limit from the stack it actually installed.
    """
    return sum(1 for m in middleware
               if getattr(m.__class__, hook) is not getattr(AgentMiddleware, hook)
               or getattr(m.__class__, f'a{hook}')
               is not getattr(AgentMiddleware, f'a{hook}'))


__all__ = ['BEGIN', 'END', 'PROMPT_RULE', 'UntrustedToolOutput',
           'configure_tracing', 'count_hooks', 'decode', 'fence', 'result_of']
