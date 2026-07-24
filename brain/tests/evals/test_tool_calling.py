import pytest

from .harness import all_scenarios, load_scenario, run_scenario

SCENARIOS = all_scenarios()


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
    if "answered_id" in expect:
        moved = next(c for c in cards if c["id"] == expect["answered_id"])
        assert moved["columnId"] == "answered"


@pytest.mark.eval
def test_at_least_one_scenario_exists():
    assert SCENARIOS, "no eval scenarios found"
