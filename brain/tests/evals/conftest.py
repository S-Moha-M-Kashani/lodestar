"""What the live eval tier needs before it is allowed to spend anything.

The live tier had exactly one door: OpenRouter, plus a key. The owner's decision
is that live model use goes through the subscriptions this machine is already
logged in to — `BRAIN_LLM=claude-cli` or `codex-cli`, no API key anywhere — so
what the gate asks about is the *backend*, not one provider's credential.

There is one helper and one mark, in one file, because the condition used to be
written out inside each live file and the two had already drifted: they agreed on
the words and would have needed the CLI backends added twice. A third live file
can now only get this right.

**This helper must stay under `brain/tests/evals/`.**
`test_config.py::test_env_example_documents_every_variable_the_code_reads` scans
exactly `server.js`, `scripts/*.mjs`, `brain/src/**` and
`brain/tests/evals/**`, and asserts in both directions. The live opt-in is read
nowhere else in that set, so moving this read one directory up turns a documented
variable into one "read by nothing" and fails that invariant.

The test *for* the helper is in `test_live_gate.py`, not here: pytest does not
collect tests out of a `conftest.py`, so a guard written beside its subject would
silently never run.
"""
import os

import pytest

# The backends that answer through a subscription already logged in to on this
# machine, with no API key anywhere. `llm_cli.py` says what they cost and what
# they are hardened against.
CLI_BACKENDS = {'claude-cli', 'codex-cli'}


def live_unready() -> str | None:
    """The live tier needs BRAIN_EVAL_LIVE=1 plus a backend that can answer:
    a CLI subscription (no key), or OpenRouter with a key.

    Returns the skip reason, or None when the tier may run.
    """
    if os.environ.get('BRAIN_EVAL_LIVE') != '1':
        return 'live eval: set BRAIN_EVAL_LIVE=1'
    if os.environ.get('BRAIN_LLM') in CLI_BACKENDS:
        return None
    if os.environ.get('OPENROUTER_API_KEY'):
        return None
    return ('live eval: choose BRAIN_LLM=claude-cli/codex-cli, '
            'or set OPENROUTER_API_KEY')


# Read once, at import — the same moment a `skipif` condition of its own would
# have been evaluated. The environment for a live run is set before pytest is
# invoked, never from inside a test, which is also why the invariant test calls
# `live_unready()` directly rather than trying to monkeypatch this mark.
_UNREADY = live_unready()

# The one skip mark both live files wear. It sits beside their own
# `@pytest.mark.live` rather than replacing it: that marker is what `-m live`
# selects, this decides whether the selected test actually runs.
LIVE = pytest.mark.skipif(bool(_UNREADY),
                          reason=_UNREADY or 'the live tier can run')
