"""Eval harness: run a registered agent against a scenario file with a
scripted fake LLM and an in-memory board. Deterministic and fully offline."""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from lodestar_brain.agent.registry import build_agent
from lodestar_brain.llm.base import AssistantTurn, ToolCall
from lodestar_brain.llm.fake import FakeProvider
from lodestar_brain.tools.board import make_board_tools

SCENARIO_DIR = Path(__file__).parent / "scenarios"

_id_counter = itertools.count(1)


class InMemoryBoard:
    """Duck-typed BoardClient: full-list read-modify-write, no HTTP.

    Mirrors the real BoardClient contract (save_cards receives the FULL list).
    The real Node server assigns id/num to newly created cards (board tools'
    create_question sends a card dict with no 'id' and relies on the server
    to add one) — this fake mirrors that by assigning an id to any card that
    arrives without one, so create_question's post-save id lookup works the
    same way it does against the real server."""

    def __init__(self, cards=None):
        self._cards = [dict(c) for c in (cards or [])]
        self.saves = []  # record each full-list save for assertions

    def list_cards(self):
        return [dict(c) for c in self._cards]

    def save_cards(self, cards):
        saved = []
        for c in cards:
            c = dict(c)
            if not c.get('id'):
                c['id'] = f'new-{next(_id_counter)}'
            saved.append(c)
        self._cards = saved
        self.saves.append(self.list_cards())
        return self.list_cards()


def _turn_from_spec(spec):
    """A scenario 'turn' is either {'content': str} or
    {'tool_calls': [{'name': str, 'arguments': {...}}]}."""
    if "tool_calls" in spec:
        calls = [ToolCall(id=f"s{i}", name=c["name"], arguments=c.get("arguments", {}))
                 for i, c in enumerate(spec["tool_calls"])]
        return AssistantTurn(tool_calls=calls)
    return AssistantTurn(content=spec.get("content", ""))


def load_scenario(path):
    return json.loads(Path(path).read_text())


def run_scenario(scenario):
    """Returns (result, board) so tests can assert on reply, steps, and board state."""
    board = InMemoryBoard(scenario.get("board", []))
    tools = make_board_tools(board)
    script = [_turn_from_spec(t) for t in scenario["script"]]
    llm = FakeProvider(script=script)
    agent = build_agent("default", llm=llm, tools=tools,
                        max_steps=scenario.get("max_steps", 8))
    result = agent.run(scenario["messages"])
    return result, board


def all_scenarios():
    return sorted(SCENARIO_DIR.glob("*.json"))
