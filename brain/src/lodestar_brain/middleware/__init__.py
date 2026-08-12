"""Cross-cutting behaviour the agent graph wears, rather than each tool.

One place per rule, so a tool added next year cannot forget it. `tracing` is the
odd member: it is a boot-time composition-root call rather than an
`AgentMiddleware`, and it lives here because it is the same kind of thing — a
decision about the whole turn that belongs to no single tool.
"""
from .tracing import configure_tracing

__all__ = ['configure_tracing']
