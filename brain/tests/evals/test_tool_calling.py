import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from lodestar_brain.config import load_settings
from lodestar_brain.server import create_app

from .conftest import LIVE
from .harness import all_scenarios, load_scenario, run_scenario

SCENARIOS = all_scenarios()


# This is an eval.
@pytest.mark.eval
@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_scenario_tool_calls_and_effect(path):
    scenario = load_scenario(path)
    result, board = run_scenario(scenario)
    expect = scenario["expect"]

    called = [step.tool for step in result.steps]
    assert called == expect["tools_called"], f"{path.name}: tools {called}"

    if "reply_contains" in expect:
        assert expect["reply_contains"] in result.reply

    cards = asyncio.run(board.list_cards())
    if "board_size" in expect:
        assert len(cards) == expect["board_size"]
    if "board_titles_contain" in expect:
        assert any(expect["board_titles_contain"] in c["title"] for c in cards)
    # Proposed cards are stored but stay off the board until the user confirms.
    proposals = board.list_proposals()
    if "proposals_size" in expect:
        assert len(proposals) == expect["proposals_size"]
    if "proposals_titles_contain" in expect:
        assert any(expect["proposals_titles_contain"] in c["title"] for c in proposals)
    # A suggested edit changes nothing until the user saves, so the scenario
    # asserts the card stayed put *and* that the suggestion is waiting.
    if "unchanged_id" in expect:
        before = {c["id"]: c for c in scenario["board"]}[expect["unchanged_id"]]
        after = next(c for c in cards if c["id"] == expect["unchanged_id"])
        assert after["columnId"] == before["columnId"], "an agent edit must not apply"
    edits = board.list_edits()
    if "edits_size" in expect:
        assert len(edits) == expect["edits_size"]
    if "edits_field" in expect:
        key, value = expect["edits_field"]
        assert any(e["fields"].get(key) == value for e in edits)


# This is a configuration invariant: an empty scenario directory must fail, not pass silently.
@pytest.mark.eval
def test_at_least_one_scenario_exists():
    assert SCENARIOS, "no eval scenarios found"


# This is a live eval: it calls the real model, and is skipped without
# BRAIN_EVAL_LIVE=1 plus a backend that can answer (see conftest.live_unready).
@pytest.mark.live
@LIVE
def test_live_agent_answers_a_trivial_prompt():
    # Uses a real LLM but a fake board URL is fine — we only assert it replies.
    # 'fake' keeps the live run to one billed or metered thing, the chat model;
    # 'hash' used to sit here and is now retired by name.
    #
    # Built from the environment, because `Settings(embedder="fake")` took every
    # other field's default and the comment here claimed that meant OpenRouter.
    # It did not: `llm_provider` defaults to 'ollama' (config.py), so this case
    # gated on OPENROUTER_API_KEY, never read it, and pointed a "live" run at
    # localhost:11434 — green on a machine running Ollama, a connection error on
    # any other, and in neither case the model the gate was asking about.
    app = create_app(load_settings({**os.environ, "BRAIN_EMBEDDER": "fake"}))
    client = TestClient(app)
    res = client.post("/agent/chat", json={"messages": [
        {"role": "user", "content": "Say the word ready and nothing else."}]})
    assert res.status_code == 200
    assert isinstance(res.json()["reply"], str) and res.json()["reply"]
