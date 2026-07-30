# Tooka Farsi embedder — design

**Date:** 2026-07-30
**Status:** **superseded — never implemented as written.** `PartAI/Tooka-SBERT-V2-Large` was
not added as a `BRAIN_EMBEDDER` backend; nothing in the repo references Tooka. The Farsi
problem this spec identifies is real and was solved differently: the RAG lab reaches Persian
through the `sentence-transformers` backend with `heydariAI/persian-embeddings` as its default
(`brain/tests/raglab/embedding.py`). Keep this document for the diagnosis in "Why this goes
first" — the zero-vector analysis still holds and is asserted by
`test_production_ascii_hash_embedder_is_blind_to_farsi`.
**Cycle 1 of 5.** Roadmap: **this** → LangChain agent rewrite → model registry & pickers →
factor explainers. The fifth cycle (Telegram MCP capture) was **abandoned** and its spec
removed.

## Why this goes first

`HashEmbedder`'s tokeniser is `[a-z0-9]+`. Farsi text contains no matching tokens, so every
Farsi string embeds to the **zero vector**. The RAG Lab already measures this — ~0.01 recall
on `diary_year_fa.json` — and `test_raglab.py` asserts it deliberately, "so the day the
default is fixed the test says so".

The consequence is sharper than a low score: **until a Farsi-capable embedder exists, every
retrieval number on that corpus is measuring noise.** Chunking strategies, rerankers, RRF
weights — none of it can be compared, because the dense side of every hybrid retrieval is
returning arbitrary neighbours. The lab cannot do the job it was built for.

`PartAI/Tooka-SBERT-V2-Large` is a Farsi-specific sentence-transformer. Adding it as a named
backend is the smallest change that makes the leaderboard mean something.

## Scope

- A `tooka` case in the brain's `make_embedder`, behind a new optional `farsi` extra.
- The same backend available to the lab's `EMBEDDERS`.
- A query/passage prompt seam on the `Embedder` protocol, because V2 SBERT models are
  asymmetric.
- **A collection-naming fix**, without which switching embedders corrupts or hides chat memory.
- Tests, including a real-model test that is opt-in so the offline suite stays offline.

## Non-goals

- Not changing the default. `BRAIN_EMBEDDER` stays `hash`; Tooka is opt-in by name, exactly
  like `fastembed`.
- Not installing it in Docker (see "Dependencies").
- No model picker UI — that is the next-but-one cycle, which will list this backend.
- Not re-embedding existing board cards or chat memory automatically.

## The embedder

```python
class TookaEmbedder:
    """Farsi sentence embeddings via sentence-transformers. Asked for by name
    (BRAIN_EMBEDDER=tooka) with the 'farsi' extra installed — a missing wheel
    raises rather than silently degrading to token buckets."""

    def __init__(self, model_name: str = 'PartAI/Tooka-SBERT-V2-Large'):
        self.model_name = model_name
        self._model = None          # loaded lazily; see below

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer   # 'farsi' extra
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:        # passages/documents
    def embed_query(self, texts: list[str]) -> np.ndarray:  # queries
```

**Loading is lazy, on first `embed()`, not in `__init__`.** Three reasons: `create_app` builds
the embedder at boot and a multi-hundred-megabyte model load would block readiness for a brain
that may never embed anything; a unit test can then construct the embedder without downloading
anything; and it matches how `FastEmbedEmbedder`'s cost is documented rather than hidden.
(`FastEmbedEmbedder` loads eagerly today — worth making lazy in the same change for symmetry,
which also shortens Docker's ~10–60 s readiness delay.)

The `farsi` extra:

```toml
farsi = ["sentence-transformers==3.4.1"]
```

Pinned exactly as given, since that is the version the model card was exercised against.
Noted for later: the current release is 5.6.1, so this pin will age; it pulls `torch`,
`transformers`, `scikit-learn`, `scipy`, and `Pillow`.

A missing extra raises with the fix in the message — the same contract `fastembed` has:

```
BRAIN_EMBEDDER=tooka needs the 'farsi' extra:
    uv sync --project brain --extra farsi
```

## The asymmetry problem — a protocol change

Most V2 SBERT models are **asymmetric**: a query and a passage must be encoded with different
prompts (typically `prompt_name="query"` / `"passage"`). Encoding both sides identically does
not error — it silently costs retrieval quality, which is exactly the class of failure this
repo has a standing rule against.

So `Embedder` gains an optional second method:

```python
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...

def embed_query(embedder: Embedder, texts: list[str]) -> np.ndarray:
    """Query-side embedding. Asymmetric models implement embed_query; symmetric
    ones (hash, fastembed) do not, and fall back to embed."""
    fn = getattr(embedder, 'embed_query', None)
    return fn(texts) if fn else embedder.embed(texts)
```

A module-level helper rather than a protocol method with a default, so `HashEmbedder` and
`FastEmbedEmbedder` need no change at all. Call sites that must switch to the helper — these
are the *query* side, and missing one is the silent-degradation bug:

| Call site | Side |
|---|---|
| `rag/chat_memory.py` — `ChromaChatMemory.search` | query |
| `rag/chat_memory.py` — `record` | passage (`embed`) |
| `rag/index.py` — `LeidenIndex.build` | passage (`embed`) |
| `rag/index.py` — the `find_related` query text | query |
| `raglab/retrieval.py` — dense retrieval of the question | query |
| `raglab/index.py` — chunk indexing | passage (`embed`) |

**The exact prompt names come from the model card, not from memory.** The implementation reads
them, and a test asserts `embed` and `embed_query` produce *different* vectors for the same
input — so a wiring regression is caught rather than absorbed.

## The collection-naming fix — this is the data-safety part

`_chat_collection_for()` returns `f'chat-board-{port}'`. It carries no embedder identity, so
the production collection currently holds 128-dimensional hash vectors under a name that says
nothing about it. Point a 1024-dimensional Tooka embedder at it and Chroma either rejects the
insert on dimension or, worse, serves neighbours computed across incompatible vector spaces.

The RAG Lab already solved this: `IndexConfig.fingerprint()` includes `embedder`, so changing
embedders builds a new collection and leaves the old one intact. Production gets the same
discipline, with one deliberate exception:

```python
def _chat_collection_for(board_api_url: str, embedder: str) -> str:
    base = f'chat-board-{port}' if port else 'chat-board-default'
    return base if embedder == 'hash' else f'{base}-{embedder}'
```

**`hash` keeps the unsuffixed legacy name.** Suffixing everything would rename the existing
production collection out from under the running board — the brain would look for
`chat-board-3000-hash`, find nothing, and report an empty memory. That is not data loss, but
it looks exactly like it, and a user cannot tell the difference. Keeping `hash` on the legacy
name means **zero migration for every existing install**, while still making mixed vector
spaces impossible.

`BRAIN_CHAT_COLLECTION` continues to override the whole thing, so anyone who wants the old
behaviour has an escape hatch.

## Configuration

| Env var | Meaning |
|---|---|
| `BRAIN_EMBEDDER` | now `hash` \| `fastembed` \| `tooka`; unknown still raises at boot |
| `BRAIN_TOOKA_MODEL` | override the checkpoint (default `PartAI/Tooka-SBERT-V2-Large`) |
| `RAGLAB_TOOKA_MODEL` | the lab's equivalent override |
| `BRAIN_FARSI_TESTS` | `1` opts into the tests that download the real model |

The lab's `EMBEDDERS` tuple becomes `('ascii-hash', 'token-hash', 'char-hash', 'fastembed',
'tooka')`. Because `IndexConfig.fingerprint()` covers `embedder`, selecting it in the panel
builds a fresh collection and every previous run's numbers stay on the leaderboard for
comparison — which is the whole point of finally having a working embedder.

## Dependencies

`sentence-transformers==3.4.1` → `torch`, `transformers`, `scikit-learn`, `scipy`, `Pillow`,
`huggingface-hub`, `tqdm`. Verified: Python ≥3.9 (the brain is 3.13.14), and torch ships cp313
wheels from 2.5.0 onward. The brain's active numpy is 2.4.6, held below 2.5 by numba from the
`voice` extra — torch 2.10+ supports numpy 2.x, so this should resolve, and `uv sync --extra
farsi` is step one of the plan to prove it.

**Docker does not install this extra.** A CPU torch adds on the order of a gigabyte to the
image, and `docker-compose.yml` continues to pin `BRAIN_EMBEDDER=fastembed`. This is the same
policy that keeps mlx out of the image, and `tests/compose.test.js` already enforces the pin —
it needs no change, but a test asserting the compose file does *not* select `tooka` is worth
adding so the image can't quietly grow.

## Error handling

| Case | Behaviour |
|---|---|
| `BRAIN_EMBEDDER=tooka`, extra missing | raises at boot with the `uv sync --extra farsi` command |
| Model download fails (offline, HF down) | raises on first embed, not at boot; the brain still serves board tools and web search |
| Unknown `BRAIN_EMBEDDER` | unchanged — raises, listing the three valid names |
| Existing 128-dim collection, `tooka` selected | impossible by construction; the suffix puts Tooka in its own collection |

## Testing

| Test | Covers |
|---|---|
| `brain/tests/test_embedder.py` | `make_embedder('tooka')` returns a `TookaEmbedder` **without downloading** (proves laziness); the missing-extra error names the extra; unknown kinds still raise |
| same, with a stubbed `SentenceTransformer` | `embed` returns L2-normalised `float32` of shape `(n, dim)`; `embed_query` takes the query path and yields **different** vectors from `embed` for identical input |
| `brain/tests/test_embedder.py::test_embed_query_helper` | the helper falls back to `embed` for `HashEmbedder` and `FastEmbedEmbedder` |
| `brain/tests/test_config.py` | `hash` keeps `chat-board-{port}`; `tooka`/`fastembed` get the suffix; `BRAIN_CHAT_COLLECTION` still overrides |
| `brain/tests/test_raglab.py` | the existing "hash gets ~0.01 recall on Farsi" assertion **stays**; a new sibling asserts Tooka clears a real recall floor — **skipped unless `BRAIN_FARSI_TESTS=1`**, because it downloads the model |
| `tests/compose.test.js` | the image does not select `tooka` |

**The default offline suite must remain fully offline and download nothing.** Every test that
touches the real checkpoint is gated behind `BRAIN_FARSI_TESTS=1`; the rest use a stub double.

**Definition of done:** `uv run --project brain pytest brain/tests -v` green with no network;
the same suite with `BRAIN_FARSI_TESTS=1 uv sync --extra farsi` green; `npm run test:server`
and e2e unaffected; and one manual lab run on `diary_year_fa.json` recording Tooka's recall
next to hash's on the leaderboard.

## Risks

1. **Prompt names.** The single highest-value unknown. Read from the model card; asserted by
   the differing-vectors test.
2. **Install weight and resolution.** `uv sync --extra farsi` is step one, before any code.
3. **Version pin age.** 3.4.1 against a current 5.6.1. If 3.4.1 cannot load the checkpoint,
   the fallback is to raise the pin and record why in the spec rather than work around it.
4. **Embedding dimension is not asserted anywhere.** `EMBED_DIM = 128` describes only
   `HashEmbedder`; nothing else should assume it. Worth a quick audit during implementation.

## Rollback

Reverting the merge removes the backend and the extra. The collection-naming change is
backward compatible by design — `hash` installs never moved — so a revert needs no data
migration either.
