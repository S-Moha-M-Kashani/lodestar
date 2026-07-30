"""Agent registry seam.

One entry today ("default"). New agents register here by adding a builder —
never by editing call sites — matching the project's substitutability invariant.
"""
from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from .runner import SYSTEM_PROMPT, LodestarAgent


def _build_default(*, settings: Settings, tools, max_steps: int,
                   system_prompt: str = SYSTEM_PROMPT, llm=None) -> LodestarAgent:
    return LodestarAgent(settings=settings, tools=tools, llm=llm,
                         system_prompt=system_prompt, max_steps=max_steps)


AGENT_BUILDERS: dict[str, Callable[..., LodestarAgent]] = {
    "default": _build_default,
}


def list_agents() -> list[str]:
    return sorted(AGENT_BUILDERS)


def build_agent(name: str, *, settings: Settings, tools, max_steps: int = 8,
                llm=None) -> LodestarAgent:
    """`settings` rather than a built model, because the chat model is created
    per request from it. `llm` is a test seam (the eval harness scripts a
    FakeChat); create_app never passes it."""
    try:
        builder = AGENT_BUILDERS[name]
    except KeyError:
        raise ValueError(f"unknown agent {name!r}; known: {list_agents()}") from None
    return builder(settings=settings, tools=tools, max_steps=max_steps, llm=llm)
