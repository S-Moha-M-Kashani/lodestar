"""Eval harness: run a registered agent against a scenario file with a
scripted fake LLM and an in-memory board. Deterministic and fully offline."""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from langchain_core.messages import AIMessage

from lodestar_brain.agent.registry import build_agent
from lodestar_brain.config import Settings
from lodestar_brain.llm.fake import FakeChat
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
        self._proposals = []
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

    def create_proposal(self, card):
        """Mirrors POST /api/proposals: the card is stored but stays OFF the
        board until the user confirms it, so list_cards must not show it."""
        proposal = dict(card)
        proposal['id'] = f'new-{next(_id_counter)}'
        proposal['num'] = 0  # a ledger number is earned at confirmation
        self._proposals.append(proposal)
        return dict(proposal)

    def list_proposals(self):
        return [dict(c) for c in self._proposals]


def _turn_from_spec(spec):
    """A scenario 'turn' is either {'content': str} or
    {'tool_calls': [{'name': str, 'arguments': {...}}]}.

    The scenario JSON files are unchanged by the LangChain rewrite — that is the
    payoff for keeping AgentStep as the brain's own type."""
    if "tool_calls" in spec:
        return AIMessage(content='', tool_calls=[
            {"name": c["name"], "args": c.get("arguments", {}), "id": f"s{i}"}
            for i, c in enumerate(spec["tool_calls"])])
    return AIMessage(content=spec.get("content", ""))


def load_scenario(path):
    return json.loads(Path(path).read_text())


def run_scenario(scenario):
    """Returns (result, board) so tests can assert on reply, steps, and board state."""
    board = InMemoryBoard(scenario.get("board", []))
    tools = make_board_tools(board)
    script = [_turn_from_spec(t) for t in scenario["script"]]
    agent = build_agent("default", settings=Settings(llm_provider='fake'),
                        tools=tools, llm=FakeChat(script=script),
                        max_steps=scenario.get("max_steps", 8))
    result = agent.run(scenario["messages"])
    return result, board


def all_scenarios():
    return sorted(SCENARIO_DIR.glob("*.json"))
