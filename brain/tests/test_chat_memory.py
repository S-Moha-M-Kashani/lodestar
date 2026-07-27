"""Chat memory: chunks from assistant chat are recorded with embeddings in a
per-board Chroma store, so the agent can quickly recall relevant past context.
Offline: HashEmbedder + a tmp persist dir; no model downloads, no network."""
from lodestar_brain.rag.chat_memory import (ChromaChatMemory, chunk_text,
                                            make_recall_tool)
from lodestar_brain.rag.embedder import HashEmbedder


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

def test_record_then_search_returns_most_relevant_first(tmp_path):
    memory = ChromaChatMemory(str(tmp_path), HashEmbedder())
    memory.record(['the wifi password is hunter2'], metadata={'role': 'user'})
    memory.record(['dentist appointment moved to friday'],
                  metadata={'role': 'user'})
    matches = memory.search('what was the wifi password', k=2)
    assert matches
    assert 'wifi password' in matches[0]['text']
    assert matches[0]['metadata']['role'] == 'user'
    assert isinstance(matches[0]['score'], float)

def test_search_results_are_capped_at_k(tmp_path):
    memory = ChromaChatMemory(str(tmp_path), HashEmbedder())
    memory.record([f'note number {i} about kubernetes' for i in range(5)])
    assert len(memory.search('kubernetes', k=3)) == 3

def test_search_on_empty_store_returns_no_matches(tmp_path):
    memory = ChromaChatMemory(str(tmp_path), HashEmbedder())
    assert memory.search('anything', k=5) == []


# ---- persistence: the whole point of Chroma over the in-memory index ------

def test_memory_persists_across_instances(tmp_path):
    ChromaChatMemory(str(tmp_path), HashEmbedder()).record(
        ['the wifi password is hunter2'])
    reopened = ChromaChatMemory(str(tmp_path), HashEmbedder())
    matches = reopened.search('wifi password', k=1)
    assert matches and 'hunter2' in matches[0]['text']


# ---- isolation: one store per board (board.db vs board-3001.db) ------------

def test_two_stores_in_different_dirs_are_fully_isolated(tmp_path):
    main = ChromaChatMemory(str(tmp_path / 'board-3000'), HashEmbedder())
    test = ChromaChatMemory(str(tmp_path / 'board-3001'), HashEmbedder())
    main.record(['production fact: rotate the api key'])
    test.record(['test fact: purple elephants'])
    assert all('purple' not in m['text']
               for m in main.search('purple elephants', k=5))
    assert all('api key' not in m['text']
               for m in test.search('rotate the api key', k=5))


# ---- recall_chat tool: how the agent reaches the memory --------------------

def test_recall_tool_searches_the_store(tmp_path):
    memory = ChromaChatMemory(str(tmp_path), HashEmbedder())
    memory.record(['the wifi password is hunter2'])
    tool = make_recall_tool(memory)
    assert tool.name == 'recall_chat'
    assert 'text' in tool.parameters['properties']
    assert tool.parameters['required'] == ['text']
    matches = tool.run({'text': 'wifi password'})
    assert matches and 'hunter2' in matches[0]['text']
