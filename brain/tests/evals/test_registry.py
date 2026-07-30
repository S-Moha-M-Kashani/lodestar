from lodestar_brain.agent.registry import list_agents, build_agent
from lodestar_brain.agent.runner import LodestarAgent
from lodestar_brain.config import Settings


def test_default_agent_is_registered():
    assert "default" in list_agents()


def test_build_agent_returns_agent_with_tools():
    # The seam now takes the whole Settings, because the chat model is built
    # per request from it — new agents still register a builder, never edit
    # call sites.
    agent = build_agent("default", settings=Settings(llm_provider='fake'),
                        tools=[], max_steps=5)
    assert isinstance(agent, LodestarAgent)
    assert agent.max_steps == 5
