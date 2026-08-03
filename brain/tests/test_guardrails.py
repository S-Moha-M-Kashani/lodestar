"""What an Assistant turn cannot do, however the prompt is phrased.

The catalogue of things a prompt — typed by the user or planted in a tool result
— must not be able to achieve, and where each one is actually proven. Most were
already covered when this file was written; duplicating those assertions would
buy nothing, so they are cited rather than copied. The two tests here are the
gaps: the *aggregate* claims that no per-tool test can make.

| A prompt must not be able to…              | Proven by                                 |
| ------------------------------------------ | ----------------------------------------- |
| destroy a card                             | **here** — no such tool is registered     |
| reach SQLite, or the board off its API     | **here** — nothing in the package imports it |
| put a card on the board unconfirmed        | `test_board_tools.py` (proposal, not save)|
| record a habit completion                  | `test_board_tools.py` (no ticking tool)   |
| widen the confirmation gate                | `test_server.py` (PROPOSING/MUTATING sets)|
| run forever                                | `test_agent.py` (step limit, sync + async)|
| have planted text read as instruction      | `test_agent.py` (the fence)               |
| …and be obeyed anyway                      | `evals/test_injection.py` (rate unmeasured)|
| send the API key to a local model          | `test_llm.py`                             |
| flood the assistant surface                | `tests/server.test.js` (429 + Retry-After)|

The distinction that decides what belongs here: these are **code-enforced**
limits, true whatever the model decides, so they are asserted rather than
measured. The fence is the other kind — it marks a channel and relies on the
model honouring the marking, which is why its row is an eval with a pending
number rather than a test with a bound.

Not a guardrail, deliberately: text the *user* types. They are the principal, so
their input reaches the instruction channel unfenced and unfiltered by design.
"""
from __future__ import annotations

import re
from pathlib import Path

import lodestar_brain
from lodestar_brain import server
from lodestar_brain.config import Settings
from lodestar_brain.tools.board import BoardClient

# What create_app is allowed to hand the agent. Nothing here can delete a card,
# write a habit completion, or reach the database.
CORE_TOOLS = {'list_cards', 'create_card', 'update_card', 'web_search',
              'find_related'}
# The one conditional tool: appended only when Chroma answers, since chat memory
# is optional infrastructure.
OPTIONAL_TOOLS = {'recall_chat'}

# Offline and keyless. 'fake' transcriber because the default would import mlx.
SETTINGS = Settings(llm_provider='fake', embedder='fake', transcriber='fake')


def _tool_names(settings, monkeypatch):
    """The tools `create_app` really hands the agent.

    Captured by standing in for LodestarAgent rather than rebuilt here: a test
    that reconstructed the list would be asserting its own copy of the
    composition and would still pass if create_app grew a seventh tool.
    """
    captured = {}

    class Recorder:
        def __init__(self, *, settings, tools, max_steps):
            captured['tools'] = tools

    monkeypatch.setattr(server, 'LodestarAgent', Recorder)
    server.create_app(settings)
    return {tool.name for tool in captured['tools']}


# This is a configuration invariant: the agent's capabilities are an allocation,
# and widening it must break a test rather than pass quietly.
def test_the_agents_tool_surface_is_closed(monkeypatch):
    """No prompt can invoke a capability that was never registered.

    This is the guardrail every "delete all my cards" phrasing meets, and the
    only one that cannot be argued around: `create_agent` binds a fixed tool
    list, so a tool absent from it is absent from the model's options entirely.
    Asserted as an exact set, in both configurations, because a *new* tool is
    exactly the change this has to catch — a subset check would wave it through.
    """
    without_memory = _tool_names(
        Settings(**{**vars(SETTINGS), 'chroma_url': ''}), monkeypatch)
    assert without_memory == CORE_TOOLS

    with_memory = _tool_names(
        Settings(**{**vars(SETTINGS), 'chroma_url': 'memory'}), monkeypatch)
    assert with_memory == CORE_TOOLS | OPTIONAL_TOOLS, (
        'recall_chat is the only tool Chroma may add')

    # Stated as intent, not just as an absence: these are the verbs a tool would
    # carry if someone added the capability this design deliberately withholds —
    # destroying a card, or writing a completion the user did not perform.
    forbidden = ('delete', 'remove', 'purge', 'destroy', 'drop', 'wipe',
                 'complete', 'tick', 'done')
    assert not [name for name in with_memory
                if any(verb in name for verb in forbidden)]


# This is a configuration invariant: invariant 2 of the root CLAUDE.md ("the
# brain never touches SQLite — all writes via the Node API") had no test.
def test_the_brain_reaches_the_board_only_through_its_http_api():
    """A prompt cannot get at the database, because no code path leads there.

    The durability promise — a card dies only via Trash → "Delete permanently" —
    holds because that route exists once, in `server.js`. It would stop holding
    the moment anything in this package opened the file directly, and no
    behavioural test would notice: a second write path is invisible until it is
    used. So this reads the source.
    """
    package = Path(lodestar_brain.__file__).parent
    imports_sqlite = [
        path.relative_to(package).as_posix()
        for path in package.rglob('*.py')
        # Import lines only. `retrieval.py` discusses sqlite-vec in prose, and an
        # alternatives note weighing a library is not a dependency on it.
        if re.search(r'^\s*(?:import|from)\s+\S*sqlite', path.read_text(),
                     re.MULTILINE)]
    assert not imports_sqlite, f'the brain must not open the board: {imports_sqlite}'

    # The board seam offers no way to destroy anything either — rejecting a
    # proposal soft-deletes through the same API, so there is no second path.
    destructive = [name for name in dir(BoardClient)
                   if any(verb in name for verb in ('delete', 'purge', 'remove'))]
    assert not destructive, f'BoardClient must expose no hard delete: {destructive}'
