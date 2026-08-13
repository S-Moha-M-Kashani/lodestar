"""Cross-cutting behaviour the agent graph wears, rather than each tool.

One place per rule, so a tool added next year cannot forget it. `untrusted`
fences every tool result on its way back to the model and `errors` turns a
raising tool into something the model can read; `cache` answers a repeated tool
call once. Their order is load-bearing and is written where the graph is built,
not here. `summarize` touches the model call instead: what a long conversation
is allowed to cost. `tracing` is the odd member: it is a boot-time composition-root call rather than
an `AgentMiddleware`, and it lives here because it is the same kind of thing — a
decision about the whole turn that belongs to no single tool.
"""
from .cache import NEVER_CACHED, ToolResultCache
from .summarize import make_context_editor, make_summarizer
from .tracing import configure_tracing
from .untrusted import (BEGIN, END, PROMPT_RULE, UntrustedToolOutput, decode,
                        fence, result_of)

__all__ = ['BEGIN', 'END', 'NEVER_CACHED', 'PROMPT_RULE', 'ToolResultCache',
           'UntrustedToolOutput', 'configure_tracing', 'decode', 'fence',
           'make_context_editor', 'make_summarizer', 'result_of']
