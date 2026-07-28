"""Chat memory: chunks from assistant chat are recorded with embeddings in a
per-board Chroma *collection* on a shared Chroma server. Offline here:
HashEmbedder + the 'memory' backend (EphemeralClient) — no server, no model
downloads, no network. The HTTP path is covered by test_chat_memory_server.py."""
import pytest

from lodestar_brain.rag.chat_memory import (ChromaChatMemory, chunk_text,
                                            make_recall_tool)
from lodestar_brain.rag.embedder import HashEmbedder


def memory(collection: str = 'chat') -> ChromaChatMemory:
    """Offline store: in-process Chroma, nothing written to disk."""
    return ChromaChatMemory('memory', HashEmbedder(), collection=collection)


# ---- chunk_text -----------------------------------------------------------

def test_chunk_text_short_text_is_a_single_chunk():
    assert chunk_text('what is leiden clustering', max_chars=500) == \
        ['what is leiden clustering']

def test_chunk_text_splits_long_text_without_losing_words():
    words = [f'word{i}' for i in range(300)]
    text = ' '.join(words)
    chunks = chunk_text(text, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert ' '.join(chunks).split() == words  # nothing dropped, order kept

def test_chunk_text_ignores_blank_input():
    assert chunk_text('', max_chars=500) == []
    assert chunk_text('   \n  ', max_chars=500) == []


# ---- ChromaChatMemory: record + search ------------------------------------

def test_record_then_search_returns_most_relevant_first():
    store = memory('chat-relevance')
    store.record(['the wifi password is hunter2'], metadata={'role': 'user'})
    store.record(['dentist appointment moved to friday'],
                 metadata={'role': 'user'})
    matches = store.search('what was the wifi password', k=2)
    assert matches
    assert 'wifi password' in matches[0]['text']
    assert matches[0]['metadata']['role'] == 'user'
    assert isinstance(matches[0]['score'], float)

def test_search_results_are_capped_at_k():
    store = memory('chat-cap')
    store.record([f'note number {i} about kubernetes' for i in range(5)])
    assert len(store.search('kubernetes', k=3)) == 3

def test_search_on_empty_store_returns_no_matches():
    assert memory('chat-empty').search('anything', k=5) == []

def test_record_ignores_blank_texts():
    store = memory('chat-blank')
    store.record(['   ', ''])
    assert store.search('anything', k=5) == []


# ---- the backend seam: 'memory' vs a real server ---------------------------
# Invariant 3 (everything substitutable): the store selects its client from the
# url, so unit tests stay offline while production talks to Docker Chroma.

def test_memory_url_uses_an_in_process_client_and_never_hits_the_network():
    store = memory('chat-offline')
    store.record(['recorded with no server running'])
    assert store.search('no server', k=1)  # would raise if it dialled :8001


def test_memory_url_is_not_treated_as_a_directory_path(tmp_path, monkeypatch):
    # Regression: a path-based client accepts 'memory' as a *relative directory*
    # and silently persists there, so every "offline" assertion above would pass
    # while writing a chroma.sqlite3 into the repo root. Run from an empty cwd
    # and require that nothing lands on disk.
    monkeypatch.chdir(tmp_path)
    store = ChromaChatMemory('memory', HashEmbedder(), collection='chat-nodisk')
    store.record(['this must live in process only'])
    assert store.search('in process', k=1)
    assert list(tmp_path.iterdir()) == [], \
        f"'memory' was written to disk as {[p.name for p in tmp_path.iterdir()]}"

def test_http_url_is_parsed_into_host_and_port():
    # Construction must not require the server to be up for parsing to work;
    # the resolved target is inspectable so misconfiguration is obvious.
    host, port, ssl = ChromaChatMemory.parse_url('http://localhost:8001')
    assert (host, port, ssl) == ('localhost', 8001, False)
    assert ChromaChatMemory.parse_url('https://chroma.internal') \
        == ('chroma.internal', 443, True)
    assert ChromaChatMemory.parse_url('http://host.docker.internal:8001') \
        == ('host.docker.internal', 8001, False)


# ---- isolation: one collection per board (board.db vs board-3001.db) -------

def test_two_collections_are_fully_isolated():
    main = memory('chat-board-3000')
    test = memory('chat-board-3001')
    main.record(['production fact: rotate the api key'])
    test.record(['test fact: purple elephants'])
    assert all('purple' not in m['text']
               for m in main.search('purple elephants', k=5))
    assert all('api key' not in m['text']
               for m in test.search('rotate the api key', k=5))

def test_collection_name_is_exposed_for_diagnostics():
    assert memory('chat-board-3001').collection_name == 'chat-board-3001'


# ---- one record = vector + chunk body + scalar metadata -------------------
# The chunk (plain text or a JSON string) rides in `documents` alongside its
# own embedding, so a single query returns the match and its payload together.
# There is deliberately no separate "vectors" vs "chunks" collection.

def test_a_json_chunk_body_round_trips_with_its_vector():
    import json
    store = memory('chat-json-body')
    chunk = {'q': 'where is the router', 'tags': ['home', 'wifi'], 'turn': 4}
    store.record([json.dumps(chunk)], metadata={'role': 'user'})
    match = store.search('where is the router', k=1)[0]
    assert json.loads(match['text']) == chunk   # body intact, not truncated
    assert isinstance(match['score'], float)    # ranked by the same record
    assert match['metadata']['role'] == 'user'


# ---- JSON metadata: Chroma rejects nested dicts ---------------------------
# Metadata values must stay scalar/list. Recording must not explode when a
# caller passes something richer than a flat dict.

def test_record_rejects_or_flattens_nested_metadata():
    store = memory('chat-json')
    store.record(['nested metadata attempt'],
                 metadata={'role': 'user', 'ctx': {'board': 3000}})
    matches = store.search('nested metadata', k=1)
    assert matches, 'record() must not drop the text over rich metadata'
    meta = matches[0]['metadata']
    assert meta['role'] == 'user'
    # the nested value survives as a JSON string, never as a dict
    assert not any(isinstance(v, dict) for v in meta.values())


# ---- recall_chat tool: how the agent reaches the memory --------------------

def test_recall_tool_searches_the_store():
    store = memory('chat-tool')
    store.record(['the wifi password is hunter2'])
    tool = make_recall_tool(store)
    assert tool.name == 'recall_chat'
    assert 'text' in tool.parameters['properties']
    assert tool.parameters['required'] == ['text']
    matches = tool.run({'text': 'wifi password'})
    assert matches and 'hunter2' in matches[0]['text']
