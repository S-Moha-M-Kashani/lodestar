"""What a turn carries besides its messages.

Two schemas, and the difference between them is the whole point.

`LodestarState` is the graph's own state: checkpointed, resumed, and grown by
every turn on the thread. `TurnContext` is the *request's* — typed, passed to
each invocation, and never written to a checkpoint. Which chat a turn belongs to
is a fact about the request, so it lives in the context; the fact that a thread
belongs to that chat is a fact about the thread, so it lives in the state.

Session as context rather than as `configurable` buys three things a dict did
not. It is typed, so a misspelt key is an error and not a silently absent
session. It is stripped from the tool schema the model sees (`ToolRuntime`), so
the rule `recall_chat` exists for — the model must never be able to name or spoof
the conversation it is in — is enforced by the framework rather than by a
convention. And it does not ride along in the run config, which is what a
checkpoint records: a session id in `configurable` is a session id in the
thread's saved metadata, and this brain has no reason to persist one twice.

This module imports nothing from the agent package.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired

from langchain.agents import AgentState


class LodestarState(AgentState):
    """`AgentState` plus whose conversation this thread is.

    `NotRequired`, because a turn that names no session is a first-class caller
    here — the evals, any curl, sixteen tests — and must not have to invent one.
    """

    session_id: NotRequired[str]


@dataclass(frozen=True)
class TurnContext:
    """The request's context, handed to every tool through `ToolRuntime`.

    Frozen: a tool reads which conversation it is serving and can neither
    rewrite it for the tools that run after it nor hand a different one back.
    """

    session_id: str = ''


__all__ = ['LodestarState', 'TurnContext']
