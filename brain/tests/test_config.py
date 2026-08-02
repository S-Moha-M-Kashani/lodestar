from lodestar_brain.config import Settings, load_settings


# This is a unit test.
def test_defaults():
    s = load_settings(env={})
    assert s.openrouter_base_url == 'https://openrouter.ai/api/v1'
    assert s.llm_provider == 'ollama'
    # No 'auto' anywhere: a mode that silently degrades hides which backend is
    # actually running. The default is the *measured* embedder, because the
    # embedder is the architecture — hash embedding scored ~0.01 recall on the
    # Farsi corpus against 0.617 for this one, a ~60× effect, where no other
    # sweep knob was worth 2%. It costs the 'local-embeddings' extra and a
    # ~2.2 GB download on first boot; 'fake' is the offline-test value.
    assert s.embedder == 'sentence-transformers'
    assert s.embed_model == ''        # '' = that backend's own default
    # Candidate F's one change after retrieval, at the threshold the lab
    # measured. It follows the main chat model, so it costs no second setting.
    assert s.grader == 'llm'
    assert s.grade_threshold == 0.4
    assert s.board_api_url == 'http://127.0.0.1:3000'
    assert s.max_agent_steps == 8
    # Likewise explicit: the local, free, private backend is the default, and
    # Docker pins BRAIN_TRANSCRIBER=openrouter because mlx cannot install there.
    assert s.transcriber == 'parakeet'
    # The local screened model is the default: a request that arrives without a
    # model never silently spends API credit.
    assert s.model == '4skl/gemma4-e2b-mtp'
    # Ollama's OpenAI-compatible surface, '/v1' included: the factory forwards
    # this verbatim, so any other local server (llama.cpp, vLLM) is a URL change
    # rather than a code change.
    assert s.ollama_base_url == 'http://localhost:11434/v1'
    # The remote dictation default has to be a model OpenRouter actually serves
    # *and* one verified to receive the audio. nvidia/nemotron-3-nano-omni:free
    # advertises audio input but its provider discards the input_audio part, so
    # every dictation came back a hallucinated apology. openai/whisper-* is a
    # different failure and was reverted for it: measured 2026-07-31, OpenRouter's
    # published catalogue is 337 models and contains no whisper, embedding or
    # rerank entry at all, so a whisper default cannot transcribe anything.
    assert s.omni_model == 'google/gemini-2.5-flash-lite'
    assert s.parakeet_model == 'mlx-community/parakeet-tdt-0.6b-v3'


# This is a unit test.
def test_choosing_the_backend_chooses_the_model_on_a_hand_built_settings():
    """`Settings(llm_provider=...)` is how the unit tests, the evals and
    create_app's callers build settings — load_settings is only the env path. A
    dataclass default that hard-codes one backend's slug while the provider field
    names another is exactly the mismatch PROVIDER_MODELS exists to prevent: it
    made /agent/models answer 'openrouter' with a model only the local daemon can
    load, and no other field on the response contradicts it."""
    assert Settings(llm_provider='openrouter').model == 'openai/gpt-5-nano'
    assert Settings(llm_provider='ollama').model == '4skl/gemma4-e2b-mtp'
    assert Settings(llm_provider='fake').model == 'openai/gpt-5-nano'
    # An explicitly named model is never replaced, or the picker's label and the
    # model that answered would disagree.
    assert Settings(llm_provider='ollama',
                    model='openai/gpt-5-nano').model == 'openai/gpt-5-nano'


# This is a unit test.
def test_env_overrides():
    s = load_settings(env={
        'OPENROUTER_API_KEY': 'sk-test',
        'BRAIN_MODEL': 'anthropic/claude-sonnet-4.5',
        'BRAIN_LLM': 'fake',
        'BRAIN_EMBEDDER': 'fake',
        'BRAIN_EMBED_MODEL': 'intfloat/multilingual-e5-small',
        'BRAIN_GRADER': 'none',
        'BRAIN_GRADE_THRESHOLD': '0.6',
        'BOARD_API_URL': 'http://board.test',
        'BRAIN_MAX_STEPS': '3',
        'BRAIN_TRANSCRIBER': 'fake',
        'BRAIN_OMNI_MODEL': 'google/gemini-2.5-flash',
        'BRAIN_PARAKEET_MODEL': 'mlx-community/parakeet-tdt-1.1b',
        'BRAIN_OLLAMA_BASE_URL': 'http://gpu.lan:11434/v1',
    })
    assert s.ollama_base_url == 'http://gpu.lan:11434/v1'
    assert s.openrouter_api_key == 'sk-test'
    assert s.model == 'anthropic/claude-sonnet-4.5'
    assert s.llm_provider == 'fake'
    assert s.embedder == 'fake'
    assert s.embed_model == 'intfloat/multilingual-e5-small'
    assert s.grader == 'none'
    assert s.grade_threshold == 0.6
    assert s.board_api_url == 'http://board.test'
    assert s.max_agent_steps == 3
    assert s.transcriber == 'fake'
    assert s.omni_model == 'google/gemini-2.5-flash'
    assert s.parakeet_model == 'mlx-community/parakeet-tdt-1.1b'


# ---- chat memory lives on the shared Chroma server ------------------------
# No on-disk store any more: one Chroma server (Docker, :8001), one `lodestar`
# database, and one collection per board. The brain talking to :3000 (board.db)
# and the brain talking to :3001 (board-3001.db) must never share a collection.

# This is a unit test.
def test_chroma_url_defaults_to_the_local_docker_server():
    assert load_settings(env={}).chroma_url == 'http://localhost:8001'


# This is a unit test.
def test_chroma_url_env_override_wins():
    s = load_settings(env={'BRAIN_CHROMA_URL': 'http://host.docker.internal:8001'})
    assert s.chroma_url == 'http://host.docker.internal:8001'


# This is a unit test.
def test_chroma_url_can_select_the_offline_memory_backend():
    # e2e and CI run without the container: 'memory' keeps them offline.
    assert load_settings(env={'BRAIN_CHROMA_URL': 'memory'}).chroma_url == 'memory'


# Real data and non-real data live in *different Chroma databases*, so all
# non-production memory can be wiped with a single database drop:
#   :3000 (board.db)      -> database 'lodestar'      collection chat-board-3000
#   :3001 (board-3001.db) -> database 'lodestar-test' collection chat-board-3001
#   pytest                -> database 'lodestar-test' collection chat-test-<uuid>

# This is a unit test.
def test_the_real_board_gets_the_production_database():
    assert load_settings(env={}).chroma_database == 'lodestar'


# This is a unit test.
def test_the_test_board_gets_the_test_database():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert s.chroma_database == 'lodestar-test'


# This is a unit test.
def test_any_other_board_is_treated_as_non_production():
    # Only :3000 is the real board; an unknown port must never be able to
    # write into the production database.
    for url in ('http://127.0.0.1:3999', 'http://board.test'):
        assert load_settings(env={'BOARD_API_URL': url}).chroma_database \
            == 'lodestar-test'


# This is a unit test.
def test_chroma_database_env_override_wins():
    s = load_settings(env={'BRAIN_CHROMA_DATABASE': 'lodestar-staging'})
    assert s.chroma_database == 'lodestar-staging'


# This is a unit test.
def test_real_and_test_boards_never_share_a_database():
    real = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3000'})
    test = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert real.chroma_database != test.chroma_database
    assert real.chat_collection != test.chat_collection


# This is a unit test.
def test_chat_collection_defaults_to_the_board_port():
    assert load_settings(env={}).chat_collection == 'chat-board-3000'


# This is a unit test.
def test_chat_collection_follows_the_test_board():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001'})
    assert s.chat_collection == 'chat-board-3001'


# This is a unit test.
def test_chat_collection_env_override_wins():
    s = load_settings(env={'BOARD_API_URL': 'http://127.0.0.1:3001',
                           'BRAIN_CHAT_COLLECTION': 'chat-scratch'})
    assert s.chat_collection == 'chat-scratch'


# This is a unit test.
def test_chat_collection_without_a_port_uses_default():
    s = load_settings(env={'BOARD_API_URL': 'http://board.test'})
    assert s.chat_collection == 'chat-board-default'


# This is a unit test.
def test_directly_constructed_settings_leave_chat_memory_off():
    # Settings() built in code (unit tests, evals) must not reach any Chroma
    # unless a url is given explicitly.
    assert Settings().chroma_url == ''


# This is a unit test.
def test_the_on_disk_persist_dir_setting_is_gone():
    # The dir-per-board store was replaced by the server; a stale attribute
    # would silently keep writing chroma/ dirs.
    assert not hasattr(Settings(), 'chat_memory_dir')
