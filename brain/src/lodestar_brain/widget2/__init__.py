"""Widget 2 — the second agent behind the Assistant.

A sibling package rather than a mode of the first one. Version 1 answers turns
on every engine the picker offers, keeps one chat coherent across turns, and
holds no tools at all.

| Module | Job |
| --- | --- |
| `prompt.py` | the system prompt, which says what this agent cannot do |
| `graph.py` | `Widget2Agent` — `create_agent`, the graph cache, run/arun/astream |
| `tools/` | empty, until a tool is carried across on purpose |

Why a package and not a flag inside the existing agent: there would be nothing
to compare against, no rollback, no honest answer to "what does Widget 2 hold?",
and the eventual retirement would mean unpicking a condition from a dozen call
sites instead of deleting a directory.

Nothing here is a base class, a plugin interface or a capability registry shared
with Widget 1. The one-entry agent-builder registry was deliberately removed
from this project in August 2026, and re-adding one to manage a two-agent
overlap would be the same mistake with a better excuse. The two agents share
services, not scaffolding.
"""
from .graph import Widget2Agent
from .prompt import SYSTEM_PROMPT

__all__ = ['SYSTEM_PROMPT', 'Widget2Agent']
