"""Agent registry seam.

One entry today ("default"). New agents register here by adding a builder —
never by editing call sites — matching the project's substitutability invariant.
"""
from __future__ import annotations

from collections.abc import Callable

from .loop import Agent, SYSTEM_PROMPT


def _build_default(*, llm, tools, max_steps: int, system_prompt: str = SYSTEM_PROMPT) -> Agent:
    return Agent(llm, tools, system_prompt=system_prompt, max_steps=max_steps)


AGENT_BUILDERS: dict[str, Callable[..., Agent]] = {
    "default": _build_default,
}


def list_agents() -> list[str]:
    return sorted(AGENT_BUILDERS)


def build_agent(name: str, *, llm, tools, max_steps: int = 8) -> Agent:
    try:
        builder = AGENT_BUILDERS[name]
    except KeyError:
        raise ValueError(f"unknown agent {name!r}; known: {list_agents()}") from None
    return builder(llm=llm, tools=tools, max_steps=max_steps)
