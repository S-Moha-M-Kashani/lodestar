"""Which board a tool call is scoped to.

The per-turn `BoardSnapshot` — one `/api/state` fetch shared by several readers
— left with Widget 1. Widget 2 has one reader, so there is no second fetch to
collapse, and a snapshot would buy a turn-long staleness window for no saving.
What remains is `board_of`: which board the owner is looking at is the run
config's fact, never a tool argument the model can name or be talked into.
"""
from __future__ import annotations

from typing import Any


def board_of(config: Any) -> str:
    """Which board this call is about. '' means the board API's own default.

    From the run config, never from the model: the user chose a board in the
    picker, and that choice is not something a tool call should be able to name,
    mistype or be argued out of.
    """
    return (config or {}).get('configurable', {}).get('board_id') or ''


__all__ = ['board_of']
