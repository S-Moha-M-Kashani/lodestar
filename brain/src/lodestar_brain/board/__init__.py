"""The board, as the brain sees it: one HTTP client and one per-turn view.

| Module | Job |
| --- | --- |
| `client.py` | `BoardClient` — the Node board API, with no way to write a card |
| `snapshot.py` | `BoardSnapshot` — one `/api/state` fetch per turn, shared |

**The direction is one way.** `snapshot` reads `client`; `client` knows nothing
about turns, and nothing here imports the tools that use either. `tools/board.py`
keeps `make_board_tools` and imports `BoardClient` from here, so
`from lodestar_brain.tools.board import BoardClient` still resolves exactly as it
did — the name moved house, it did not change.
"""
from .client import BoardClient
from .snapshot import BoardSnapshot, board_of, turn_of

__all__ = ['BoardClient', 'BoardSnapshot', 'board_of', 'turn_of']
