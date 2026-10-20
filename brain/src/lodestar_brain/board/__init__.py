"""The board, as the brain sees it: one HTTP client, and which board a call is for.

| Module | Job |
| --- | --- |
| `client.py` | `BoardClient` — the Node board API, with no way to write a card |
| `snapshot.py` | `board_of` — which board a tool call is scoped to |

**The direction is one way.** `snapshot` reads nothing from `client`. Nothing
here imports the tools that use either.
"""
from .client import BoardClient
from .snapshot import board_of

__all__ = ['BoardClient', 'board_of']
