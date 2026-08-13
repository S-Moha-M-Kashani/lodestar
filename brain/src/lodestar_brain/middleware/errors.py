"""A tool that raises becomes an error the model can read.

`create_agent` lets a tool exception escape the graph, so without this one
unreachable board is a 500 for the whole chat turn. The handler and the
middleware that carries it live here so the graph names the rule rather than
spelling it out; the ordering — this one *inside* the fence — stays in `graph`,
where the middleware list is written.
"""
from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware import ToolErrorMiddleware


def _tool_error(exc: Exception, request: Any) -> str:
    """A raising tool becomes {'error': str(exc)} fed back to the model.

    `create_agent` lets tool exceptions escape the graph, so one unreachable
    board would turn into a 500 for the whole chat turn. `ToolErrorMiddleware`
    is opt-in — returning None would propagate — so this handles everything,
    which is what the hand-rolled loop did before it. It also serves the async
    path, since the middleware falls back to `on_error` when no `aon_error` is
    given, and astream is the path the route actually takes.

    The message is `str(exc)` rather than the exception's type: these are our
    own tools failing against the user's own board, and "board unreachable at
    127.0.0.1:3000" is what lets the model say something useful about it.
    """
    return json.dumps({'error': str(exc)})


__all__ = ['ToolErrorMiddleware', '_tool_error']
