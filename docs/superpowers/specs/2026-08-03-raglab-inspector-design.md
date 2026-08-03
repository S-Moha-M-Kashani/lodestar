# RAG Lab Inspector — design

Date: 2026-08-03
Status: approved (design), pending implementation plan
Branch: `feat/raglab-inspector`

## Purpose

The RAG lab decides retrieval choices by measurement. But the *why* behind a
number is currently invisible: you cannot see the ground-truth pairs the lab
scores against, the chunks a config produced, or where a given chunk ranked at
each retrieval step. The Inspector makes all three visible, read-only, in a page
you tile next to the board so you can watch a config's behaviour while you drive
runs.

It is **test-only tooling** (part of `brain/tests/raglab/`), nothing in
production imports it, and it ships with tests like every other change here.

## Non-goals

- Not a place to *run* the sweep or the leaderboard — those stay on the :9002
  panel / their CLIs. The Inspector inspects; it does not rank or decide.
- Not a board view. It is a standalone page on its own port, by explicit request
  ("a new port so I could open them side by side").
- No new persistence. It reads fixtures and `.runs/`; it writes nothing.

## Architecture

A new self-contained lab service, **the Inspector**, bound to **:9003** — a free
slot in the 9000 block (9000 brain / 9001 test-brain / 9002 raglab / 9003
inspector). Started with a new script `npm run raglab:inspector`.

- **Self-contained, not a proxy.** It imports the same lab modules (`corpus`,
  `chunking`, `index`, `pipeline`, `metrics`) and builds its **own in-memory
  index** on demand, so it runs without :9002 up. This matches the lab's rule
  that an experiment is not a record and the index lives in process memory
  (`MemoryVectors`). No Chroma, no DB, no socket opened on build.
- **Read-only.** It never writes `.runs/`. It reads the two fixtures
  (`diary_year_fa.json`, `diary_year_fa_groundtruth.json`) and reads saved runs
  from `.runs/`. It has no board write path (the lab has none anyway).
- It serves its own static page (`static/inspector.html`, `inspector.js`,
  `inspector.css`) — a standalone page in the ledger aesthetic, theme-aware.

The service is a second FastAPI app in its own module
(`brain/tests/raglab/inspector.py`), run by its own uvicorn invocation. It
reuses the lab's `IndexRegistry` and pipeline; a separate process means its own
index registry, which is acceptable and consistent at this corpus size
(in-memory, brute-force cosine).

## The three views (tabs)

### A. Ground Truth

The 112 pairs from `diary_year_fa_groundtruth.json`, one row each:

| id | type · difficulty | question (fa / en) | reference answer | evidence quote(s) | answerable |

A collapsible row reveals `key_facts` and, per evidence item, its `session_id`
and `message_indices`. Top-of-view controls: a type/difficulty filter and a text
search over the question text.

The data is the fixture's questions **with** their answers and evidence — the
existing `/api/questions` on :9002 deliberately strips those (it exists for
picking a question to run), so the Inspector needs its own endpoint.

### B. Chunks (after indexing)

Pick a config → the Inspector builds that index → it lists **all chunks grouped
by session**, collapsible:

```
▾ 2025-08-02-a  (4 chunks)
   chunk 1: خلاصه که باز نشستم پای اسپرینت دو…
   chunk 2: راستش گیر فنی نیست، گیرم اینه که…
▸ 2025-08-03-a  (3 chunks)
```

Rendered incrementally (session groups expand on demand) so thousands of rows
stay smooth. Session headers carry the index ("orange") step ink.

### C. Retrieval — one table per question

Two data sources, both feeding the same table:

- **Live**: choose a question + config; the Inspector runs retrieval now and
  builds a trace.
- **Browse saved**: open a `.runs/` file and read its stored per-question rows.

Rows = every candidate chunk considered, **including chunks dropped at rerank or
grade**. Columns:

| chunk | dense rank | bm25 rank | RRF (fused) rank | rerank score | grade score | kept? | gold |

- **Row background = ground-truth relevance:** **white** if the chunk contains
  one of *this question's* gold evidence quotes, **gray** if not. Relevance is
  measured against `diary_year_fa_groundtruth.json`, not the pipeline's own
  keep/drop decision — the keep/drop decision is the separate `kept?` column, so
  disagreements between "was actually relevant" and "the pipeline kept it" are
  visible.
- **Column headers colour-coded by pipeline step** per the lab convention:
  retrieval/ranking green, generation blue, index orange.
- A one-line question header shows question-level grades/metrics (recall,
  quote-recall, abstained-correctly; for saved runs, the RAGAS scores that run
  stored).

Matching a chunk to a gold quote reuses the same notion of "contains the quote"
the ground truth is built on (the evidence quotes are verbatim substrings of the
corpus messages, verified). Rule, fixed in one helper and unit-tested: a chunk
is **gold** for a question when its (normalised) text contains one of that
question's evidence quotes, **or** the quote contains the chunk text — substring
in either direction, since a chunk may be smaller than a quote or span several.
Normalisation reuses the lab's shared tokeniser/normaliser so a whitespace or
zero-width difference cannot make a true match look false.

## The one real backend change — a retrieval trace

`pipeline.py` today keeps per-stage *scores* only for the chunks it *keeps*
(`Context.stages`). The retrieval table needs ranks at **every step for every
candidate, including dropped ones**. So the pipeline gains an **opt-in trace**:

- A new traced retrieval path (`retrieve_traced()`, or a `trace=True` argument
  that returns an extra structure) records the full ladder: `dense_ranked`,
  `lexical_ranked`, the fused (RRF) order, the rerank order + scores, and the
  grade score + keep decision per candidate.
- The normal `Outcome` that flows to evals/RAGAS is **unchanged** — the trace is
  built only when the Inspector asks, so the production/eval path stays
  byte-for-byte identical and no eval number can move because of this feature.

The trace is the Inspector's contract with the pipeline. It is a plain
dataclass/dict of ordered lists, so the frontend renders it without knowing
pipeline internals.

## Endpoints (on the :9003 app)

- `GET /` → the inspector page
- `GET /api/health` → `{ok: true}`
- `GET /api/groundtruth` → the full pairs (incl. `answer_fa`, `key_facts`,
  `evidence` with quotes, `type`, `difficulty`, `answerable`, `threads`)
- `POST /api/chunks` `{config}` → build the index for that config, return chunks
  grouped by session: `[{session_id, date, chunks: [{id, text}]}]`
- `POST /api/trace` `{question_id, config}` → run retrieval live, return the
  per-step trace plus gold marking for each candidate
- `GET /api/runs` → list saved runs in `.runs/`
- `GET /api/runs/:id` → per-question rows from one saved run (the "browse saved"
  path)

## Colour & visual conventions

Honors the lab's rule that colour means a pipeline step: index orange, retrieval
green, generation blue (reusing the step hue tokens `--step-index-h`, etc.). The
white/gray row shading is a *separate* axis (ground-truth relevance) and does not
collide with the step hues. Standalone page, ledger aesthetic (quad-paper,
typewriter display font), theme-aware (light/dark).

CSS class names are test-stable API; the inspector introduces its own
(`.inspector-*`, and view-specific ones) and does not rename existing ones.

## Testing (ships with the change)

- **`tests/ports.test.js`** (configuration invariant): the Inspector binds
  :9003, lives in the 9000 block, names no database; `npm run raglab:inspector`
  exists and requests the embedder extra its configured default needs (same rule
  already enforced for `raglab`).
- **Python units** (`brain/tests/test_inspector.py`, or added to
  `test_raglab.py`):
  - the groundtruth endpoint returns full pairs including answers and evidence
    quotes (integration: FastAPI TestClient over the app);
  - the chunk listing groups by session and the per-session counts match the
    index (unit over the grouping helper);
  - **the retrieval trace records a rank at each step and includes at least one
    dropped candidate** (unit over `retrieve_traced` on a small in-memory index
    with the fake embedder);
  - the gold-marking helper marks a chunk containing a question's evidence quote
    as gold and a non-matching chunk as not gold (unit).
- **Guardrails extended** (the lab's existing absence tests): no inspector module
  imports `chromadb` or `ChromaChatMemory`; the inspector build opens no socket.

Suite pins `RAGLAB_LLM=fake` / `BRAIN_EMBEDDER=fake` as the rest of the lab
suite does, so the trace test is deterministic and offline.

## Scope / YAGNI

- Live retrieval needs the real embedder (~2.2 GB on first use) and, only when
  the grade step is on, an LLM — the same requirements as the existing query
  panel. For a quick structural look you can run against a fake/ascii-hash
  embedder.
- The live config picker stays to the essentials (retriever, k, rerank depth,
  grader/threshold) and defaults to the **chosen architecture (candidate F)**.
  It can grow to the full knob set later; that is a separate change.
- The three views share the inspector shell but are independent enough to build
  and test one at a time (Ground Truth → Chunks → Retrieval), Retrieval last
  because it depends on the trace.

## Files touched (anticipated)

- `brain/tests/raglab/inspector.py` — new service (FastAPI app + read-only
  endpoints)
- `brain/tests/raglab/static/inspector.html`, `inspector.js`, `inspector.css` —
  new standalone page
- `brain/tests/raglab/pipeline.py` — opt-in `retrieve_traced` / trace structure
- `brain/tests/raglab/corpus.py` — a full-groundtruth reader if the existing one
  is not reused as-is
- `package.json` — `raglab:inspector` script
- `tests/ports.test.js` — :9003 allocation + command invariants
- `brain/tests/test_inspector.py` (or `test_raglab.py`) — units + integration
- `brain/tests/raglab/CLAUDE.md` — document the Inspector, its port, and the
  trace contract
