"""What has to be true before a live eval is allowed to spend anything.

The live tier had exactly one door: OpenRouter, plus a key. The owner's decision
is that live model use goes through the subscriptions this machine is already
logged in to — `BRAIN_LLM=claude-cli` or `codex-cli`, no API key anywhere — so
the question the gate asks is about the *backend*, not about one provider's
credential. `live_unready()` is the single answer to it, and both live files ask
it rather than each spelling out a condition of its own.

Nothing here calls a model. It is the guard on the guard, and it is in a
`test_*.py` file rather than beside the helper because **pytest does not collect
tests from `conftest.py`** — verified on this repo's configuration, which sets no
`python_files` override: a test written there is silently never run, which is the
worst thing a guard can be.

The helper itself stays under `brain/tests/evals/` for a reason that is not
taste: `test_config.py::test_env_example_documents_every_variable_the_code_reads`
scans exactly `server.js`, `scripts/*.mjs`, `brain/src/**` and
`brain/tests/evals/**`, and asserts in both directions. The live opt-in is read
nowhere else in that set, so moving the read one directory up turns a documented
variable into one "read by nothing" and fails that invariant.
"""
from pathlib import Path

import pytest

from .conftest import live_unready

# The two files that may spend money or subscription quota. Named here so the
# wiring assert below fails by name when a third one is added and forgets.
LIVE_FILES = ('test_injection.py', 'test_tool_calling.py')


# This is a configuration invariant: the live tier must be reachable without any
# API key when a CLI backend is named — the owner's decision is that live runs go
# through subscriptions, never OpenRouter by default.
@pytest.mark.eval
def test_a_cli_backend_unlocks_the_live_tier_without_a_key(monkeypatch):
    monkeypatch.setenv('BRAIN_EVAL_LIVE', '1')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)

    monkeypatch.setenv('BRAIN_LLM', 'claude-cli')
    assert live_unready() is None
    # Both subscriptions, not only the one the npm script happens to name: a
    # helper that knew `claude-cli` alone would pass the line above and still
    # lock out the other half of the decision.
    monkeypatch.setenv('BRAIN_LLM', 'codex-cli')
    assert live_unready() is None

    # A backend that cannot answer is still shut out. `fake` replies instantly
    # and deterministically, so a live tier that admitted it would report a
    # measurement nobody made.
    monkeypatch.setenv('BRAIN_LLM', 'fake')
    assert live_unready() is not None

    # The old door stays open. OpenRouter with a key is no longer the only way
    # in, which is not the same as it being closed.
    monkeypatch.setenv('BRAIN_LLM', 'openrouter')
    assert live_unready() is not None
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-not-a-real-key')
    assert live_unready() is None

    # The opt-in outranks every backend: an ordinary offline suite run must not
    # start spending quota merely because this machine is logged in to a CLI.
    monkeypatch.delenv('BRAIN_EVAL_LIVE')
    monkeypatch.setenv('BRAIN_LLM', 'claude-cli')
    assert live_unready() is not None

    # A gate nothing consults is decoration, and the assert above would pass
    # just as happily with both live files still gating on OPENROUTER_API_KEY
    # themselves — in which case a CLI backend unlocks nothing at all.
    here = Path(__file__).parent
    for name in LIVE_FILES:
        assert 'live_unready' in (here / name).read_text(), (
            f'{name} still decides on its own whether the live tier may run')
