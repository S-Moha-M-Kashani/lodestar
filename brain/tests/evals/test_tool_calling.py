import os

import pytest
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app

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

    cards = board.list_cards()
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
    if "answered_id" in expect:
        moved = next(c for c in cards if c["id"] == expect["answered_id"])
        assert moved["columnId"] == "answered"


# This is a configuration invariant: an empty scenario directory must fail, not pass silently.
@pytest.mark.eval
def test_at_least_one_scenario_exists():
    assert SCENARIOS, "no eval scenarios found"


# This is a live eval: it calls the real model, and is skipped without BRAIN_EVAL_LIVE=1.
@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("BRAIN_EVAL_LIVE") != "1" or not os.environ.get("OPENROUTER_API_KEY"),
    reason="live eval: set BRAIN_EVAL_LIVE=1 and OPENROUTER_API_KEY to run")
def test_live_agent_answers_a_trivial_prompt():
    # Uses real LLM but a fake board URL is fine — we only assert it replies.
    # 'fake' keeps the live run to one paid thing — the chat model. 'hash' used
    # to sit here and is now retired by name, so this only ever ran live.
    app = create_app(Settings(embedder="fake"))  # llm_provider defaults to openrouter
    client = TestClient(app)
    res = client.post("/agent/chat", json={"messages": [
        {"role": "user", "content": "Say the word ready and nothing else."}]})
    assert res.status_code == 200
    assert isinstance(res.json()["reply"], str) and res.json()["reply"]
