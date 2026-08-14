"""The tracing seam: a named backend, and an `off` that survives a stale shell.

Tracing ships a private board's conversations to a third party, so the test that
matters here is not "does it dispatch" but "does off mean off when the
environment disagrees".
"""
import os

import pytest
from langsmith import run_trees, utils

from lodestar_brain.config import Settings
from lodestar_brain.middleware import configure_tracing

# Everything this seam touches is process-global — `configure()` sets a module
# global as well as a context var, `get_env_var` is `lru_cache`d, and the module
# writes to `os.environ` — so each test starts from a known state and leaves
# none behind for the rest of the suite.
_TRACING_VARS = ('LANGSMITH_TRACING', 'LANGCHAIN_TRACING_V2',
                 'LANGCHAIN_TRACING', 'LANGSMITH_API_KEY')


@pytest.fixture(autouse=True)
def clean_tracing_state(monkeypatch):
    for name in _TRACING_VARS:
        monkeypatch.delenv(name, raising=False)
    utils.get_env_var.cache_clear()
    yield
    run_trees.configure(enabled=None)   # None clears it, False would persist
    utils.get_env_var.cache_clear()


# This is a unit test.
def test_the_seam_dispatches_on_the_named_backend_and_rejects_anything_else():
    configure_tracing(Settings(tracing='langsmith', langsmith_api_key='ls-test'))
    assert utils.tracing_is_enabled() is True
    # The client that ships the traces reads the environment, never Settings, so
    # a key that arrived in code has to land there.
    assert os.environ['LANGSMITH_API_KEY'] == 'ls-test'

    configure_tracing(Settings(tracing='off'))
    assert utils.tracing_is_enabled() is False

    # No 'auto' and no fallback: an unknown value names the two that exist
    # rather than quietly choosing one of them.
    with pytest.raises(ValueError, match="'langsmith' or 'off'"):
        configure_tracing(Settings(tracing='langsmith-cloud'))


# This is a unit test.
def test_langsmith_without_a_key_raises_at_boot():
    # Keyless, langsmith warns and then calls out anyway, so a session would run
    # believing it is recorded while the traces go nowhere. Refuse instead, and
    # name the way out.
    with pytest.raises(ValueError, match='LANGSMITH_API_KEY'):
        configure_tracing(Settings(tracing='langsmith'))
    # The failed boot leaves nothing half-enabled behind it.
    assert utils.tracing_is_enabled() is False


# This is a unit test.
def test_off_outranks_a_stale_langchain_tracing_v2_in_the_environment():
    """`off` is applied in code because the environment cannot express it.

    A developer who once exported `LANGCHAIN_TRACING_V2=true` for another project
    has a shell in which this board traces: `tracing_is_enabled` asks for the
    `TRACING_V2` name first and `get_env_var` searches the LANGSMITH *and*
    LANGCHAIN namespaces for it, so the suffix resolves before the namespace does
    and the legacy variable beats an explicit `LANGSMITH_TRACING=false`.
    """
    os.environ['LANGCHAIN_TRACING_V2'] = 'true'
    os.environ['LANGSMITH_TRACING'] = 'false'
    utils.get_env_var.cache_clear()
    assert utils.tracing_is_enabled() is True, 'the trap this test exists for'

    configure_tracing(Settings(tracing='off'))

    assert utils.tracing_is_enabled() is False
    assert 'LANGCHAIN_TRACING_V2' not in os.environ


# This is a configuration invariant: tracing ships a private journal's
# metadata to a third party, so it is opt-in by name — never a default. This
# machine's untracked .env once turned it on with a live key; an untracked
# file cannot be asserted, but the shipped default can.
def test_the_shipped_default_sends_no_trace_anywhere():
    from lodestar_brain.config import load_settings

    assert load_settings({}).tracing == 'off', (
        'BRAIN_TRACING defaults to something other than off; a conversation '
        'must never leave this machine unless someone chose that by name')


# This is an integration test.
def test_create_app_applies_the_tracing_seam_so_off_means_off():
    """The wiring, not the function: `configure_tracing` passed every test above
    while being called by nothing in production, so `BRAIN_TRACING=off`
    configured nothing and a stale shell export kept tracing — the exact failure
    this seam exists to stop. The composition root must apply it.

    Side effect worth knowing: once wired, every `create_app` in the suite
    mutates process-global tracing state (safe direction — off) and pops the
    legacy env vars; this file's autouse fixture restores them."""
    from lodestar_brain.server import create_app

    os.environ['LANGCHAIN_TRACING_V2'] = 'true'
    utils.get_env_var.cache_clear()
    assert utils.tracing_is_enabled() is True, 'the stale shell, before boot'

    create_app(Settings(tracing='off', llm_provider='fake', embedder='fake',
                        transcriber='fake'))

    assert utils.tracing_is_enabled() is False
    assert 'LANGCHAIN_TRACING_V2' not in os.environ
