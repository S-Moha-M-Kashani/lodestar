from lodestar_brain.config import Settings, load_settings


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


# ---- chat memory dir pairs with the board ---------------------------------
# One Chroma store per board: the brain talking to :3000 (board.db) and the
# brain talking to :3001 (board-3001.db) must never share a persist dir.

def test_chat_memory_dir_defaults_to_the_board_port():
    assert load_settings(env={}).chat_memory_dir == 'chroma/board-3000'


def test_chat_memory_dir_follows_the_test_board():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert s.chat_memory_dir == 'chroma/board-3001'


def test_chat_memory_dir_env_override_wins():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001',
                           'BRAIN_CHAT_MEMORY_DIR': '/data/chroma-test'})
    assert s.chat_memory_dir == '/data/chroma-test'


def test_chat_memory_dir_without_a_port_uses_default():
    s = load_settings(env={'BOARD_API_URL': 'http://board.test'})
    assert s.chat_memory_dir == 'chroma/board-default'


def test_directly_constructed_settings_leave_chat_memory_off():
    # Settings() built in code (unit tests, evals) must not touch the disk
    # unless a dir is given explicitly.
    assert Settings().chat_memory_dir == ''
