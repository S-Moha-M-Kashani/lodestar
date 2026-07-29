# Model registry and factor explainers — design

**Date:** 2026-07-30
**Status:** approved, not yet implemented
**Cycles 3 and 4 of 5.** Roadmap: Tooka Farsi embedder → LangChain agent rewrite → **model
registry & pickers (3)** → **factor explainers (4)** → Telegram MCP capture.

Two features in one spec because they share one mechanism — a registry that lives beside the
definition it describes, is served over the API, and is rendered by one frontend component —
and one surface, the RAG Lab panel and the Assistant view.

## Why

Two standing requirements:

1. **Anywhere a model is used, the user picks it.** A hard-coded model is a decision taken
   away from them, and it matters most for Farsi, where most defaults are quietly bad.
2. **Every configuration factor explains itself.** The RAG Lab exposes 28 knobs. An
   unexplained knob is a knob no real decision can be made about, and the explanation has to
   be re-readable later rather than remembered.

## What already exists — and must not be broken

`app.js:2388–2447` is not a blank slate:

- `MODEL_PICKERS` — three roles today (`text`, `omni`, `embed`), each `{key, id, label,
  options}`, persisted to `localStorage` under `question-board:models`.
- `RETIRED_MODELS` — slugs retired **for cause**, each with a comment recording what it cost.
  The nemotron entry documents a whole release lost because dropping a model from the options
  list does not deselect it, so the one browser that had it selected kept using the broken
  provider.
- A saved off-list pick is deliberately re-added as an option and kept selected.

**All three behaviours survive.** This cycle generalises the registry; it does not restart it.

### Three states that must stay distinct

The new `(NA)` label introduces a state next to two that already exist, and conflating them
would re-create the nemotron bug:

| State | Shown as | Selectable |
|---|---|---|
| Wired and reachable | `name (open source)` / `(closed source)` | yes |
| Recommended, unverified or not installed | `name (NA — not available but recommended to check)` | no — informational |
| **Retired for cause** | not listed at all | no, and a saved pick is swept |

A retired model must never reappear as "NA, worth checking". `RETIRED_MODELS` stays a hard
denylist, and a test asserts no id appears in both it and any role's choices.

## The scope problem — the part I got wrong first

A dropdown implies "change this now". Not every model choice can honour that:

| Scope | Meaning | Roles |
|---|---|---|
| `request` | sent with each API call; takes effect on the next message | `agent.chat`, `agent.omni`, `raglab.llm`, `raglab.judge`, `raglab.ragas` |
| `boot` | chosen when the process starts; a change needs a restart | `embed.board`, `voice.transcriber` |
| `index` | a change builds a **new** collection; previous results stay comparable | `raglab.embedder`, `raglab.cross_encoder` |

Rendering a live-looking dropdown for a `boot` role would be a silent no-op — the same class of
failure as the old `auto` embedder modes. So **scope is part of the registry and part of the
UI**: `request` roles apply immediately; `boot` roles show the current value with the env var
and "takes effect after restart"; `index` roles state that selecting them builds a new
collection. The `!` explainer repeats it in words.

## The registry

One shape, two sources — model roles from the brain, lab factors from the lab, because each
lives beside the definition it describes:

```python
@dataclass(frozen=True)
class ModelChoice:
    id: str                  # 'PartAI/Tooka-SBERT-V2-Large'
    source: str              # 'open' | 'closed' | 'na'
    note: str = ''           # why you might pick it; shown in the option's help

@dataclass(frozen=True)
class ModelRole:
    id: str                  # 'raglab.judge'
    label: str               # 'LLM-as-judge (key facts)'
    scope: str               # 'request' | 'boot' | 'index'
    env: str                 # the variable that sets it, for boot/index roles
    help: str                # the ! explainer
    default: str
    choices: list[ModelChoice]
```

`available` is **computed at request time**, never stored: is the extra importable, is there an
API key, is the checkpoint already downloaded. A choice marked `source='na'` is always
unavailable by definition; a choice marked open/closed can still come back unavailable, and the
UI says which.

### The nine roles

| Role | Scope | Drives |
|---|---|---|
| `agent.chat` | request | the Assistant's LLM (existing `text` picker, renamed) |
| `agent.omni` | request | audio/photo/video → text (existing `omni` picker) |
| `embed.frontend` | request | the browser's semantic map (existing `embed` picker) |
| `embed.board` | boot | `BRAIN_EMBEDDER` — Leiden index + chat memory |
| `voice.transcriber` | boot | `BRAIN_TRANSCRIBER` + `BRAIN_PARAKEET_MODEL` |
| `raglab.embedder` | index | lab indexing |
| `raglab.cross_encoder` | index | cross-encoder reranking |
| `raglab.llm` | request | summariser, reranker, grader, answerer, HyDE |
| **`raglab.judge`** | request | `judge_key_facts` — **split out from `raglab.llm`** |
| `raglab.ragas` | request | RAGAS judged metrics |

`embed.frontend` and `embed.board` are genuinely different models on different sides of the
wire; today both are called "embeddings", which is confusing enough to be worth renaming.

**Splitting `raglab.judge` from `raglab.llm` is a correctness fix, not a feature.** A judge
scoring output from the same model shares its blind spots; the judge should be separately —
usually more strongly — selectable.

### Seed contents

Availability of specific OpenRouter and Hugging Face ids is **not asserted from memory**.
Anything unverified ships as `na`, which is exactly what that label is for; promoting a choice
out of `na` requires someone to have actually run it.

**Farsi / multilingual embedders** — `PartAI/Tooka-SBERT-V2-Large` (open, the new Farsi
default) · `PartAI/Tooka-SBERT-V2-Small` (open, na — benchmark against Large) ·
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (open, current lab default) ·
`intfloat/multilingual-e5-large` (open, na) · `BAAI/bge-m3` (open, na) ·
`Alibaba-NLP/gte-multilingual-base` (open, na) · `HooshvareLab/bert-fa-zwnj-base` (open, na —
Persian BERT, *not* similarity-tuned; expect it to lose) · `openai/text-embedding-3-large`
(closed, na — needs a provider seam) · `cohere/embed-multilingual-v3` (closed, na)

**LLM / judge** — `openai/gpt-5-nano` (closed, current default) · `openai/gpt-5-mini` (closed,
already in the picker) · `openai/gpt-5` (closed, na — judge-grade) ·
`google/gemini-2.5-flash-lite` (closed, current omni) · `anthropic/claude-*` (closed, na) ·
`qwen/qwen3-*` (open weights, na — notably decent Farsi) · `deepseek/deepseek-v3` (open
weights, na) · `google/gemma-3-27b-it` (open weights, na) ·
`meta-llama/llama-3.3-70b-instruct` (open weights, na — weaker Farsi) · Ollama local (open,
na — unlocked by the LangChain cycle's factory)

**Cross-encoder rerankers** — `jinaai/jina-reranker-v2-base-multilingual` (open, current) ·
`BAAI/bge-reranker-v2-m3` (open, na) · `Alibaba-NLP/gte-multilingual-reranker-base` (open, na)
· `cohere/rerank-multilingual-v3.0` (closed, na)

**Transcribers** — `mlx-community/parakeet-tdt-0.6b-v3` (open, current — **European
languages; expect poor Farsi**) · `openai/whisper-large-v3` (open, na — good Farsi) ·
`mistralai/voxtral-small-24b-2507` (open weights, already in the omni picker) ·
`openai/gpt-4o-transcribe` (closed, na)

The Parakeet note matters: the user is building a Farsi diary corpus and Farsi dictation today
probably does not work well. Saying so in the explainer is the point of the feature.

## The `!` explainers

**28 factors**, from `raglab/config.py`:

- `IndexConfig` (7) — chunker, chunk_chars, overlap, contextual, embedder, summarizer, layers
- `RetrievalConfig` (18) — retriever, k, candidates, rrf_k, search_layers, rollup_boost,
  time_filter, multi_query, hyde, mmr_lambda, reranker, rerank_depth,
  recency_half_life_days, agentic_weights, grader, grade_threshold, parent_expansion,
  max_context_chars
- `GenerationConfig` (3) — answerer, model, key_facts_judge

Each `!` opens a popover with three parts:

1. **What it is**, in plain language, no jargon that isn't immediately unpacked.
2. **One line per option** — this is where "what *is* hybrid-RRF?", "what does MMR trade
   away?", "what does a grader actually gate?" get answered. Covering the 36 enum members
   inside their parent factor's popover avoids 36 separate buttons.
3. **What to watch when tuning** — including the measured notes already in the code, e.g.
   `multi_query` is on because it moved quote recall 0.489 → 0.512 and precision 0.243 → 0.300
   for free.

Help text lives in the Python dataclass definitions' registry, not in `app.js`, so it cannot
drift from the field it describes, and **a test asserts every field of all three dataclasses
has an entry** — a new factor cannot ship without an explainer.

### UI

Served as `GET /api/raglab/factors` (already-proxied prefix) and `GET /api/models/roles`
(brain, proxied like the rest of `/api/agent/*`).

- `.factor-help` — the `!` trigger: a real `<button>` with `aria-expanded`, adjacent to the
  field's label.
- `.factor-help-panel` — the popover, `role="note"`, Escape closes, clicking another `!`
  closes the previous one.
- Not a native `alert()` and not a modal `<dialog>` — a modal would hide the very control being
  explained. Inline expansion beside the field is right here, and the frontend rule the
  project actually has is "no native `confirm()`/`alert()`".
- Existing class names are untouched; these are additions, per the test-stable-API rule.
- Styling uses existing tokens (`--paper`, `--ink`, `--rule-red`) — the `!` reads as a
  margin annotation on quad paper, which is what the visual identity already is.

## Error handling

| Case | Behaviour |
|---|---|
| Registry endpoint unreachable (lab not running) | the panel already says `npm run raglab`; pickers fall back to the built-in `MODEL_PICKERS` list so the Assistant view never loses its picker |
| A saved pick is now `na` or unavailable | keep it selected, mark it unavailable in the option label — never silently switch the user's model |
| A saved pick is in `RETIRED_MODELS` | swept back to default, as today |
| A `boot`-scoped role is changed | the UI states the env var and that a restart is needed; nothing pretends to have applied |
| A factor has no help entry | caught by the coverage test before it ships |

## Testing

| Test | Covers |
|---|---|
| `brain/tests/test_model_registry.py` | every role has a non-empty label, help, default, and ≥1 choice; the default is among the choices and is not `na`; **no id appears in both `RETIRED_MODELS` and a role's choices**; every `source` is one of the three values |
| `brain/tests/test_raglab.py` | **coverage:** every field of `IndexConfig`, `RetrievalConfig`, `GenerationConfig` has a help entry — all 28 — and every enum member is mentioned in its parent's option lines |
| `tests/server.test.js` | `/api/models/roles` proxies and returns JSON; `/api/raglab/factors` reports the lab as unavailable rather than erroring when it is down |
| `tests/e2e_test.py` | clicking `.factor-help` reveals `.factor-help-panel` with text; Escape closes it; a second `!` closes the first; each model dropdown renders with source labels; an `na` option is present and **not** selectable; changing a `boot`-scoped role shows the restart note |
| `tests/e2e_test.py` (regression) | the existing retired-pick sweep still works |

**Definition of done:** all four suites green, and a manual pass reading every one of the 28
explainers to confirm they are actually comprehensible — the feature's whole purpose is that
they can be understood later, and only a human read proves that.

## Risks

1. **Content quality is the real work.** 28 explainers plus ~36 option lines is mostly writing,
   and a wrong explanation is worse than none. Drafting them against the code that implements
   each factor — not from general knowledge — is part of the task.
2. **`na` lists rot.** Every `na` entry is a claim that something is worth checking; a stale
   list becomes noise. The `note` field should say what to compare it against, so an entry can
   be resolved rather than merely accumulated.
3. **Two embedding roles named "embeddings"** will confuse until renamed; the rename touches
   saved `localStorage` keys, so the load path must migrate the old `embed` key to
   `embed.frontend` rather than dropping the user's pick.

## Rollback

Additive: new endpoints, new registry modules, new CSS classes, and a generalised picker that
falls back to today's hard-coded list. Reverting the merge restores the current three pickers
with their saved values intact, provided the `localStorage` key migration is one-way-safe —
which the risk above requires anyway.
