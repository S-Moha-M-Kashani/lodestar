"""Suite-wide guards.

Both are here rather than in the test files because the thing they protect
against is a test *forgetting* them.
"""
import os

import pytest


@pytest.fixture(autouse=True, scope='session')
def _the_lab_suite_does_not_read_the_machine():
    """Pin the lab's backend so the suite measures the code, not the laptop.

    `LabSettings.llm_provider` defaults to `ollama` — right for a lab that is
    meant to be runnable for free, wrong for a test suite, because
    `models.provider_problems` asks a live `/api/tags` what that backend
    serves. So `test_a_per_task_model_is_accepted_by_the_query_endpoint` passed
    or failed according to whether Ollama happened to be running on the
    machine, which surfaced the moment /api/queries started applying the same
    screen /api/evaluations always had. `fake` is what CLAUDE.md already calls
    the offline test env; a test that wants a real backend states it, as the
    OLLAMA_SETTINGS cases do.
    """
    saved = {key: os.environ.get(key)
             for key in ('RAGLAB_LLM', 'RAGLAB_MODEL', 'BRAIN_LLM')}
    os.environ['RAGLAB_LLM'] = 'fake'
    os.environ['BRAIN_LLM'] = 'fake'
    os.environ.pop('RAGLAB_MODEL', None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True, scope='session')
def _runs_dir_is_never_the_real_one(tmp_path_factory):
    """No test may write into the lab's real `.runs/`.

    `evaluate.run_eval` ends in `save_run`, so every test that evaluates
    anything deposits a JSON run file. Eleven tests redirected RUNS_DIR to
    tmp_path by hand and one did not, which is the arithmetic of a guard
    repeated per test: eleven chances to remember, one miss, and by 2026-08-02
    two thirds of the 154 files in `.runs/` were suite leakage rather than runs
    anybody asked for. The leaderboard's own quarantine rules (no judge, no
    recorded sample) kept them from corrupting a comparison, which is why it
    went unnoticed for so long — the cost was a durable artefact directory that
    was mostly noise.

    Session-scoped so the per-test `monkeypatch.setattr(evaluate, 'RUNS_DIR',
    tmp_path)` calls still win where a test wants its own empty directory, and
    so reads and writes within one test agree. `config.RUNS_DIR` is left
    pointing at the real path on purpose: it is what the invariant test
    compares against, and the panel reports it as a location.
    """
    from .raglab import evaluate, sweep
    runs = tmp_path_factory.mktemp('raglab-runs')
    saved = {module: module.RUNS_DIR for module in (evaluate, sweep)}
    for module in saved:
        module.RUNS_DIR = runs
    yield
    for module, original in saved.items():
        module.RUNS_DIR = original
