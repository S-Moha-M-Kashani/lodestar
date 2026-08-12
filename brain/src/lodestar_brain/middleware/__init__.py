"""Cross-cutting behaviour the agent graph wears, rather than each tool.

One place per rule, so a tool added next year cannot forget it. `untrusted`
fences every tool result on its way back to the model and `errors` turns a
raising tool into something the model can read; their order is load-bearing and
is written where the graph is built, not here. `tracing` is the odd member: it
is a boot-time composition-root call rather than an `AgentMiddleware`, and it
lives here because it is the same kind of thing — a decision about the whole
turn that belongs to no single tool.
"""
from .tracing import configure_tracing
from .untrusted import (BEGIN, END, PROMPT_RULE, UntrustedToolOutput, decode,
                        fence, result_of)

__all__ = ['BEGIN', 'END', 'PROMPT_RULE', 'UntrustedToolOutput',
           'configure_tracing', 'decode', 'fence', 'result_of']
