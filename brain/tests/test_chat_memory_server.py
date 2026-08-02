"""Integration: chat memory against the real Chroma server in Docker — the
project's own (localhost:8003, persisting to databases/chroma-data). Skipped
when the container isn't running, so the offline unit suite stays green — but
this is the only place the HTTP client, the auto-created `lodestar` database,
and real cross-process persistence are exercised.

Run the server with: npm run chroma
"""
import urllib.error
import urllib.request
import uuid

import pytest

from lodestar_brain.retrieval import ChatStore, LexicalHashEmbeddings

CHROMA_URL = 'http://localhost:8003'
PRODUCTION_DATABASE = 'lodestar'   # board.db's memory — must stay untouched
DATABASE = 'lodestar-test'


# This is a configuration invariant: it guards the real board's memory from the test suite.
def test_this_suite_never_targets_the_production_database():
    # Guard rail: a careless edit to DATABASE would let pytest write into the
    # real board's chat memory. Real data is only destroyed on purpose.
    assert DATABASE != PRODUCTION_DATABASE


def _server_up() -> bool:
    try:
        urllib.request.urlopen(f'{CHROMA_URL}/api/v2/heartbeat', timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _server_up(), reason=f'Chroma server not reachable at {CHROMA_URL}')


@pytest.fixture
def collection_name():
    """A throwaway collection per test; removed afterwards so repeated runs
    never accumulate junk in the user's Chroma."""
    name = f'chat-test-{uuid.uuid4().hex[:8]}'
    yield name
    try:
        ChatStore(CHROMA_URL, LexicalHashEmbeddings(), collection=name,
                         database=DATABASE).drop()
    except Exception:
        pass


def store(collection: str) -> ChatStore:
    return ChatStore(CHROMA_URL, LexicalHashEmbeddings(), collection=collection,
                            database=DATABASE)


# ---- the database is created on demand ------------------------------------
# Chroma does NOT auto-create databases (unlike collections), so the store must
# POST it on first use or every fresh machine breaks.

# This is an integration test: a real Chroma server over HTTP, skipped when it is down.
def test_missing_database_is_created_on_first_use(collection_name):
    memory = store(collection_name)
    memory.record(['database bootstrap check'])
    assert memory.search('bootstrap', k=1)


# ---- real persistence across processes, the point of the server ------------

# This is an integration test: a real Chroma server over HTTP, skipped when it is down.
def test_records_persist_across_separate_client_instances(collection_name):
    store(collection_name).record(['the wifi password is hunter2'])
    reopened = store(collection_name)          # brand-new client, same server
    matches = reopened.search('wifi password', k=1)
    assert matches and 'hunter2' in matches[0]['text']


# This is an integration test: a real Chroma server over HTTP, skipped when it is down.
def test_search_ranks_by_relevance_on_the_server(collection_name):
    memory = store(collection_name)
    memory.record(['the wifi password is hunter2'], metadata={'role': 'user'})
    memory.record(['dentist appointment moved to friday'],
                  metadata={'role': 'assistant'})
    top = memory.search('dentist appointment', k=1)[0]
    assert 'dentist' in top['text']
    assert top['metadata']['role'] == 'assistant'


# ---- board isolation on a shared server -----------------------------------

# This is an integration test: a real Chroma server over HTTP, skipped when it is down.
def test_two_board_collections_do_not_leak_on_the_server():
    a, b = f'chat-a-{uuid.uuid4().hex[:8]}', f'chat-b-{uuid.uuid4().hex[:8]}'
    try:
        store(a).record(['production fact: rotate the api key'])
        store(b).record(['test fact: purple elephants'])
        assert all('purple' not in m['text']
                   for m in store(a).search('purple elephants', k=5))
        assert all('api key' not in m['text']
                   for m in store(b).search('rotate the api key', k=5))
    finally:
        for name in (a, b):
            try:
                store(name).drop()
            except Exception:
                pass


# ---- graceful degradation: a down server must not take the brain with it ---

# This is an integration test: a real Chroma server over HTTP, skipped when it is down.
def test_unreachable_server_raises_a_clear_error():
    with pytest.raises(Exception) as err:
        ChatStore('http://127.0.0.1:9', LexicalHashEmbeddings(),
                         collection='chat', database=DATABASE)
    assert '9' in str(err.value) or 'connect' in str(err.value).lower()
