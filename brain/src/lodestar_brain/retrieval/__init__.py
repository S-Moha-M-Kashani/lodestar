"""Retrieval: everything between a question and the contexts that answer it.

This was one 1449-line module doing eight jobs. It is now a package, and the
public surface is unchanged — `from lodestar_brain.retrieval import CardIndex,
ChatStore, make_embeddings, …` resolves exactly as it did, because every name
the module exported is re-exported here.

The shipped half of the chosen retrieval architecture, bottom up:

| Module | Job |
| --- | --- |
| `embeddings.py` | the embedder seam: what text becomes before anything ranks |
| `chunking.py` | how text is cut, and the document shape a card takes |
| `timescope.py` | the time language in a question, as a date range |
| `expand.py` | one question in, several searches out |
| `fusion.py` | BM25, and the RRF that combines the two halves |
| `rerank.py` | the reranker seam: IDF term coverage by default, a model by config |
| `gate.py` | asking a model which contexts actually help |
| `cards.py` | `CardIndex` — the board, embedded in this process |
| `chat.py` | `ChatStore` — chat memory in Chroma |

**The dependency direction is one-way.** The leaves (`embeddings`, `chunking`,
`timescope`, `expand`) import nothing from this package; `fusion` reads
`timescope`, `rerank` reads `expand` and `fusion`, `gate` reads nothing. Only
`cards` and `chat` — the two assemblies — reach across the whole set, and
nothing imports *them* from inside the package. That is what keeps the import
graph acyclic: the two modules that need everything are the two nothing needs.

Everything here implements LangChain's own interfaces (`Embeddings`,
`Document`, `BaseRetriever`), so the pieces compose without adapters.
"""
from .cards import CardIndex
from .chat import (LEGACY_BOARD, MEMORY_URL, ChatStore, ensure_database,
                   parse_chroma_url)
from .chunking import (CARD_META_KEYS, CHUNK_OVERLAP, CHUNK_SIZE, SEPARATORS,
                       card_document, card_text, day_int, flatten_metadata,
                       split_text)
from .embeddings import (BACKEND_DEFAULTS, BACKENDS, DEFAULT_FASTEMBED_MODEL,
                         DEFAULT_LOCAL_MODEL, E5_PREFIXES, EMBED_PREFIXES,
                         LEXICAL_DIM, QWEN3_INSTRUCT, FastEmbedEmbeddings,
                         LexicalHashEmbeddings, SentenceTransformerEmbeddings,
                         make_embeddings, resolve_embed_model)
from .expand import (QUESTION_WORDS, SYNONYMS, expand_queries, keyword_query,
                     multi_query)
from .fusion import (CANDIDATES, RECALL_WEIGHTS, RERANK_DEPTH, RRF_K, TOP_K,
                     RankBM25Retriever, hybrid_retriever, rrf_fuse)
from .gate import (GATE_BUDGET, GATE_MAX_CHARS, GATE_PROMPT,
                   GATE_USER_TEMPLATE, GRADE_THRESHOLD, GRADERS, NO_OPINION,
                   gate_llm, relevance_gate, relevance_scores)
from .rerank import (FAKE_NGRAM, MIN_TERM_WEIGHT, RERANK_BACKENDS,
                     RERANK_BUDGET, RERANK_MODEL_DEFAULTS, FakeReranker,
                     OpenRouterReranker, Reranker, coverage, lexical_rerank,
                     make_reranker, resolve_rerank_model)
from .timescope import (DATE_FIELDS, ENGLISH_SEASONS, JALALI_MONTHS, LAST_YEAR,
                        SEASONS, TimeScope, resolve_time_scope, where_clause)

__all__ = [
    # embeddings
    'BACKENDS', 'BACKEND_DEFAULTS', 'DEFAULT_FASTEMBED_MODEL',
    'DEFAULT_LOCAL_MODEL', 'E5_PREFIXES', 'EMBED_PREFIXES', 'LEXICAL_DIM',
    'QWEN3_INSTRUCT', 'FastEmbedEmbeddings', 'LexicalHashEmbeddings',
    'SentenceTransformerEmbeddings', 'make_embeddings', 'resolve_embed_model',
    # chunking and documents
    'CARD_META_KEYS', 'CHUNK_OVERLAP', 'CHUNK_SIZE', 'SEPARATORS',
    'card_document', 'card_text', 'day_int', 'flatten_metadata', 'split_text',
    # time scopes
    'DATE_FIELDS', 'ENGLISH_SEASONS', 'JALALI_MONTHS', 'LAST_YEAR', 'SEASONS',
    'TimeScope', 'resolve_time_scope', 'where_clause',
    # query expansion
    'QUESTION_WORDS', 'SYNONYMS', 'expand_queries', 'keyword_query',
    'multi_query',
    # retrieval and fusion
    'CANDIDATES', 'RECALL_WEIGHTS', 'RERANK_DEPTH', 'RRF_K', 'TOP_K',
    'RankBM25Retriever', 'hybrid_retriever', 'rrf_fuse',
    # reranking
    'FAKE_NGRAM', 'MIN_TERM_WEIGHT', 'RERANK_BACKENDS', 'RERANK_BUDGET',
    'RERANK_MODEL_DEFAULTS', 'FakeReranker', 'OpenRouterReranker', 'Reranker',
    'coverage', 'lexical_rerank', 'make_reranker', 'resolve_rerank_model',
    # the relevance gate
    'GATE_BUDGET', 'GATE_MAX_CHARS', 'GATE_PROMPT', 'GATE_USER_TEMPLATE',
    'GRADERS', 'GRADE_THRESHOLD', 'NO_OPINION', 'gate_llm', 'relevance_gate',
    'relevance_scores',
    # the two assemblies
    'CardIndex', 'ChatStore', 'LEGACY_BOARD', 'MEMORY_URL', 'ensure_database',
    'parse_chroma_url',
]
