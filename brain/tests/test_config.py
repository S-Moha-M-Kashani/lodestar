from lodestar_brain.config import load_settings


def test_defaults():
    s = load_settings(env={})
    assert s.openrouter_base_url == 'https://openrouter.ai/api/v1'
    assert s.llm_provider == 'openrouter'
    assert s.embedder == 'auto'
    assert s.board_api_url == 'http://127.0.0.1:3000'
    assert s.max_agent_steps == 8


def test_env_overrides():
    s = load_settings(env={
        'OPENROUTER_API_KEY': 'sk-test',
        'BRAIN_MODEL': 'anthropic/claude-sonnet-4.5',
        'BRAIN_LLM': 'fake',
        'BRAIN_EMBEDDER': 'hash',
        'BOARD_API_URL': 'http://board.test',
        'BRAIN_MAX_STEPS': '3',
    })
    assert s.openrouter_api_key == 'sk-test'
    assert s.model == 'anthropic/claude-sonnet-4.5'
    assert s.llm_provider == 'fake'
    assert s.embedder == 'hash'
    assert s.board_api_url == 'http://board.test'
    assert s.max_agent_steps == 3
