from lodestar_brain.config import Settings, load_settings


def test_defaults():
    s = load_settings(env={})
    assert s.openrouter_base_url == 'https://openrouter.ai/api/v1'
    assert s.llm_provider == 'openrouter'
    # No 'auto' anywhere: a mode that silently degrades hides which backend is
    # actually running. 'hash' is the honest default because fastembed is an
    # optional extra — a machine without it must say so, not quietly downgrade.
    # The composed brain pins BRAIN_EMBEDDER=fastembed (compose.test.js).
    assert s.embedder == 'hash'
    assert s.board_api_url == 'http://127.0.0.1:3000'
    assert s.max_agent_steps == 8
    # Likewise explicit: the local, free, private backend is the default, and
    # Docker pins BRAIN_TRANSCRIBER=openrouter because mlx cannot install there.
    assert s.transcriber == 'parakeet'
    # The cheap tier is the default: a request that arrives without a model
    # (evals, direct API use) should not silently buy the expensive one.
    assert s.model == 'openai/gpt-5-nano'
    # The old default, nvidia/nemotron-3-nano-omni-...:free, is advertised as
    # audio-capable but its provider silently discards the input_audio part —
    # every dictation came back a hallucinated apology. The default must be a
    # model verified to actually receive audio.
    assert s.omni_model == 'google/gemini-2.5-flash-lite'
    assert s.parakeet_model == 'mlx-community/parakeet-tdt-0.6b-v3'


def test_env_overrides():
    s = load_settings(env={
        'OPENROUTER_API_KEY': 'sk-test',
        'BRAIN_MODEL': 'anthropic/claude-sonnet-4.5',
        'BRAIN_LLM': 'fake',
        'BRAIN_EMBEDDER': 'hash',
        'BOARD_API_URL': 'http://board.test',
        'BRAIN_MAX_STEPS': '3',
        'BRAIN_TRANSCRIBER': 'fake',
        'BRAIN_OMNI_MODEL': 'google/gemini-2.5-flash',
        'BRAIN_PARAKEET_MODEL': 'mlx-community/parakeet-tdt-1.1b',
    })
    assert s.openrouter_api_key == 'sk-test'
    assert s.model == 'anthropic/claude-sonnet-4.5'
    assert s.llm_provider == 'fake'
    assert s.embedder == 'hash'
    assert s.board_api_url == 'http://board.test'
    assert s.max_agent_steps == 3
    assert s.transcriber == 'fake'
    assert s.omni_model == 'google/gemini-2.5-flash'
    assert s.parakeet_model == 'mlx-community/parakeet-tdt-1.1b'


# ---- chat memory lives on the shared Chroma server ------------------------
# No on-disk store any more: one Chroma server (Docker, :8001), one `lodestar`
# database, and one collection per board. The brain talking to :3000 (board.db)
# and the brain talking to :3001 (board-3001.db) must never share a collection.

def test_chroma_url_defaults_to_the_local_docker_server():
    assert load_settings(env={}).chroma_url == 'http://localhost:8001'


def test_chroma_url_env_override_wins():
    s = load_settings(env={'BRAIN_CHROMA_URL': 'http://host.docker.internal:8001'})
    assert s.chroma_url == 'http://host.docker.internal:8001'


def test_chroma_url_can_select_the_offline_memory_backend():
    # e2e and CI run without the container: 'memory' keeps them offline.
    assert load_settings(env={'BRAIN_CHROMA_URL': 'memory'}).chroma_url == 'memory'


# Real data and non-real data live in *different Chroma databases*, so all
# non-production memory can be wiped with a single database drop:
#   :3000 (board.db)      -> database 'lodestar'      collection chat-board-3000
#   :3001 (board-3001.db) -> database 'lodestar-test' collection chat-board-3001
#   pytest                -> database 'lodestar-test' collection chat-test-<uuid>

def test_the_real_board_gets_the_production_database():
    assert load_settings(env={}).chroma_database == 'lodestar'


def test_the_test_board_gets_the_test_database():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert s.chroma_database == 'lodestar-test'


def test_any_other_board_is_treated_as_non_production():
    # Only :3000 is the real board; an unknown port must never be able to
    # write into the production database.
    for url in ('http://127.0.0.1:3999', 'http://board.test'):
        assert load_settings(env={'BOARD_API_URL': url}).chroma_database \
            == 'lodestar-test'


def test_chroma_database_env_override_wins():
    s = load_settings(env={'BRAIN_CHROMA_DATABASE': 'lodestar-staging'})
    assert s.chroma_database == 'lodestar-staging'


def test_real_and_test_boards_never_share_a_database():
    real = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3000'})
    test = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert real.chroma_database != test.chroma_database
    assert real.chat_collection != test.chat_collection


def test_chat_collection_defaults_to_the_board_port():
    assert load_settings(env={}).chat_collection == 'chat-board-3000'


def test_chat_collection_follows_the_test_board():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert s.chat_collection == 'chat-board-3001'


def test_chat_collection_env_override_wins():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001',
                           'BRAIN_CHAT_COLLECTION': 'chat-scratch'})
    assert s.chat_collection == 'chat-scratch'


def test_chat_collection_without_a_port_uses_default():
    s = load_settings(env={'BOARD_API_URL': 'http://board.test'})
    assert s.chat_collection == 'chat-board-default'


def test_directly_constructed_settings_leave_chat_memory_off():
    # Settings() built in code (unit tests, evals) must not reach any Chroma
    # unless a url is given explicitly.
    assert Settings().chroma_url == ''


def test_the_on_disk_persist_dir_setting_is_gone():
    # The dir-per-board store was replaced by the server; a stale attribute
    # would silently keep writing chroma/ dirs.
    assert not hasattr(Settings(), 'chat_memory_dir')
