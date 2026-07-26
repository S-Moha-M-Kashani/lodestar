from lodestar_brain.agent.registry import list_agents, build_agent
from lodestar_brain.agent.loop import Agent
from lodestar_brain.llm.fake import FakeProvider


def test_default_agent_is_registered():
    assert "default" in list_agents()


def test_build_agent_returns_agent_with_tools():
    agent = build_agent("default", llm=FakeProvider(), tools=[], max_steps=5)
    assert isinstance(agent, Agent)
    assert agent.max_steps == 5
