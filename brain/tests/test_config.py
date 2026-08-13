import re
from pathlib import Path

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
    # knob measured was worth 2%. It costs the 'local-embeddings' extra and a
    # ~2.2 GB download on first boot; 'fake' is the offline-test value.
    assert s.embedder == 'sentence-transformers'
    assert s.embed_model == ''        # '' = that backend's own default
    # The reranker defaults the *other* way round from the embedder, and the
    # asymmetry is the point: the embedder's expensive default is measured, this
    # one is not measured at all yet. 'lexical' is free, offline, deterministic
    # and what the shipped precision numbers were taken with, where 'openrouter'
    # bills a search per question and exports card text. rerank.py names the run
    # that would move it; until then the cheap one holds.
    assert s.reranker == 'lexical'
    assert s.rerank_model == ''       # '' = cohere/rerank-4-fast, hosted only
    # The chosen architecture's one change after retrieval, at the measured
    # threshold. It follows the main chat model, so it costs no second setting.
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
    # every dictation came back a hallucinated apology.
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
        'BRAIN_RERANKER': 'fake',
        'BRAIN_RERANK_MODEL': 'cohere/rerank-4-pro',
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
    assert s.reranker == 'fake'
    assert s.rerank_model == 'cohere/rerank-4-pro'
    assert s.grader == 'none'
    assert s.grade_threshold == 0.6
    assert s.board_api_url == 'http://board.test'
    assert s.max_agent_steps == 3
    assert s.transcriber == 'fake'
    assert s.omni_model == 'google/gemini-2.5-flash'
    assert s.parakeet_model == 'mlx-community/parakeet-tdt-1.1b'


# ---- chat memory lives on the project's own Chroma server -----------------
# One Chroma server (the compose `chroma` service over databases/chroma-data,
# host port 8003 — 8001/8002 belong to the unrelated ~/vectordb-lab stack, see
# tests/ports.test.js), one `lodestar` database, and one collection per board.
# The brain talking to :3000 (board.db) and the brain talking to :3001
# (board-3001.db) must never share a collection.

# This is a unit test.
def test_chroma_url_defaults_to_the_projects_own_chroma():
    assert load_settings(env={}).chroma_url == 'http://localhost:8003'


# This is a unit test.
def test_chroma_url_env_override_wins():
    s = load_settings(env={'BRAIN_CHROMA_URL': 'http://host.docker.internal:8001'})
    assert s.chroma_url == 'http://host.docker.internal:8001'


# This is a unit test.
def test_chroma_url_can_select_the_offline_memory_backend():
    # e2e and CI run without the container: 'memory' keeps them offline.
    assert load_settings(env={'BRAIN_CHROMA_URL': 'memory'}).chroma_url == 'memory'


# This is a configuration invariant: the product checks where links lead, and a
# Settings built in code does not — the same split `chroma_url` already uses.
def test_url_safety_is_real_from_the_environment_and_inert_in_code():
    from lodestar_brain.config import Settings
    assert load_settings(env={}).url_safety == 'google-safe-browsing'
    assert Settings().url_safety == 'off'
    # Opting out is a named choice, and the key travels with the backend.
    assert load_settings(env={'BRAIN_URL_SAFETY': 'off'}).url_safety == 'off'
    assert load_settings(
        env={'GOOGLE_SAFE_BROWSING_KEY': 'k'}).safe_browsing_key == 'k'


# This is a configuration invariant: the product traces, and a Settings built in
# code ships nothing anywhere — the same split url_safety and chroma_url use.
def test_tracing_is_real_from_the_environment_and_inert_in_code():
    assert load_settings(env={}).tracing == 'langsmith'
    # Unit tests and evals build Settings directly; none of them may put a
    # private board's conversations on a third party's server by default.
    assert Settings().tracing == 'off'
    assert load_settings(env={'BRAIN_TRACING': 'off'}).tracing == 'off'
    assert load_settings(
        env={'LANGSMITH_API_KEY': 'ls-k'}).langsmith_api_key == 'ls-k'


# This is a configuration invariant: the product keeps the agent's threads on
# disk, and a Settings built in code keeps them nowhere — the same split
# url_safety, tracing and chroma_url use.
def test_the_checkpoint_file_is_real_from_the_environment_and_inert_in_code():
    assert load_settings(env={}).checkpoint_db == 'databases/real/brain-checkpoints.db'
    # Unit tests, evals and scripts build Settings directly, and none of them may
    # write a file into the folder that holds the user's real data.
    assert Settings().checkpoint_db == ':memory:'
    assert load_settings(
        env={'BRAIN_CHECKPOINT_DB': '/tmp/cp.db'}).checkpoint_db == '/tmp/cp.db'


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


# This is a configuration invariant.
def test_no_whisper_model_is_named_anywhere_in_the_code():
    """A model that cannot transcribe is not worth a mention.

    OpenRouter serves no whisper entry at all, so the slug was never a setting
    anyone could use — only a name lying around in defaults, comments and one
    test payload, where the next reader has to re-learn that it is dead. The
    default is asserted in `test_defaults`; this is the other half of the same
    claim, and a scan rather than an assertion because the cost of the slug was
    always that it kept reappearing.

    Code and config only: `docs/` is the historical record and may say whatever
    was true when it was written. The pattern is the model slug, not the English
    word — the star sky's "seeded whisper of noise" is about noise, and a future
    local faster-whisper backend would be a real option, not this dead one. This
    file is skipped because it has to hold the pattern it looks for.
    """
    root = Path(__file__).resolve().parents[2]
    assert (root / '.env.example').exists(), 'scan is anchored at the repo root'
    slug = re.compile(r'openai/whisper|whisper-large', re.IGNORECASE)
    scanned = [root / '.env.example', root / 'docker-compose.yml',
               root / 'server.js', *sorted((root / 'js').rglob('*.js')),
               *sorted((root / 'brain' / 'src').rglob('*.py')),
               *sorted((root / 'brain' / 'tests').rglob('*.py'))]
    named = [f'{path.relative_to(root)}:{n}'
             for path in scanned if path != Path(__file__).resolve()
             for n, line in enumerate(path.read_text().splitlines(), 1)
             if slug.search(line)]
    assert named == []


# How this repo reads the environment: three shapes in Python, three in
# JavaScript. `envKey:` is the odd one — scripts/db-location.mjs takes the name
# as data, so a pattern that only matched `process.env.X` would miss BOARD_DB.
_ENV_READS = re.compile(r"""
    (?:os\.environ|environ|env)\.get\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]
  | (?:os\.environ|environ|env)\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*\]
  | getenv\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]
  | process\.env\.([A-Z][A-Z0-9_]{2,})
  | process\.env\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*\]
  | envKey:\s*['"]([A-Z][A-Z0-9_]{2,})['"]
""", re.VERBOSE)

# `#VAR=` or `VAR=` at the start of a line — the template comments every
# variable out, so both spellings count as documented.
_ENV_DOCUMENTED = re.compile(r'^#?\s*([A-Z][A-Z0-9_]{2,})=')


# This is a configuration invariant.
def test_env_example_documents_every_variable_the_code_reads():
    """`.env.example` is the only list of what this project can be configured
    with, so a variable missing from it is undiscoverable and one lingering in it
    after the code stopped reading it is a lie. Both directions are asserted for
    that reason.

    Scanned: the shipped code and the hand-run surfaces that read the
    environment on their own — the Node server, its scripts, the brain and the
    live evals. Not the unit tests: those set variables to exercise the readers
    above, and a value invented for one assertion is not configuration anyone
    should be told about.
    """
    root = Path(__file__).resolve().parents[2]
    sources = [root / 'server.js', *sorted((root / 'scripts').glob('*.mjs')),
               *sorted((root / 'brain' / 'src').rglob('*.py')),
               *sorted((root / 'brain' / 'tests' / 'evals').rglob('*.py'))]
    read = {name for path in sources
            for match in _ENV_READS.finditer(path.read_text())
            for name in match.groups() if name}
    documented = {match.group(1) for line in
                  (root / '.env.example').read_text().splitlines()
                  if (match := _ENV_DOCUMENTED.match(line))}
    # NODE_ENV and friends belong to the runtime, not to this project.
    read -= {'NODE_ENV', 'PATH', 'HOME'}
    assert read - documented == set(), 'read by the code, absent from .env.example'
    assert documented - read == set(), 'in .env.example, read by nothing'
