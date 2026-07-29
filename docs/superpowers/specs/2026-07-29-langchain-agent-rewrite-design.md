# LangChain agent rewrite — design

**Date:** 2026-07-29
**Status:** approved, not yet implemented
**Cycle 1 of 2.** Cycle 2 is `2026-07-29-telegram-mcp-capture-design.md`, which depends on
this one and must not start until this is merged and green.

## Why

The brain hand-rolls its function calling: `agent/loop.py` (85 lines), a frozen `Tool`
dataclass carrying hand-written JSON Schema, and an `LLMProvider` Protocol with two
implementations. It works. Three things argue for replacing it:

1. **Schemas drift silently.** Nothing checks that `create_question(title, notes, type,
   category, column_id, tags)` matches the JSON Schema written 40 lines below it. A new
   parameter can be added and never reach the model.
2. **The loop re-implements a solved problem** — tool dispatch, feeding exceptions back to
   the model, transcript assembly — and we maintain it.
3. **Cycle 2 needs MCP tools**, which arrive as LangChain `BaseTool` objects. Keeping our
   own `Tool` dataclass would mean writing and maintaining a translation layer forever.

The goal: **LangChain owns the loop and the schemas; the brain keeps its own result type and
its env-var seam.** Invariant #3 (everything substitutable, selected by env var) is not
weakened — it is re-expressed as a factory, and it gets *closer* to the stated "OpenRouter
now, Ollama later" aim, since `ChatOllama` becomes one case in that factory.

## Non-goals

- **No LangGraph graph and no checkpointer.** The ecosystem decision table puts a
  single-purpose agent with a fixed six-tool set at the LangChain `create_agent` layer.
  Chat history lives in the browser and is posted whole with every request; a server-side
  thread store would be a second source of truth for something already durable.
- **No LangSmith tracing.** See "A deliberate deviation" below.
- **No Deep Agents.** No planning, file management, or subagents are needed.
- **No change** to the board API, SQLite, the frontend, or the proposal gate.
- **No new tools.** The same six: `list_questions`, `create_question`, `update_question`,
  `web_search`, `find_related`, `recall_chat`.

## Architecture

```
brain/src/lodestar_brain/
  llm/
    factory.py    NEW      make_chat_model(settings, model=None) -> BaseChatModel
    fake.py       REWRITE  FakeChat(BaseChatModel) — same two modes, same strings
    base.py       DELETE   LLMProvider / AssistantTurn / ToolCall
    openrouter.py DELETE   ChatOpenAI(base_url=…) replaces all 29 lines
  tools/
    base.py       DELETE   the Tool dataclass and its hand-written schemas
    board.py      @tool × 3 with explicit Pydantic args models
    websearch.py  @tool × 1; the SearchProvider Protocol stays untouched
  rag/
    index.py      make_retrieve_tool → returns a LangChain tool (find_related)
    chat_memory.py make_recall_tool  → returns a LangChain tool (recall_chat)
  agent/
    loop.py       DELETE   all 85 lines
    runner.py     NEW      LodestarAgent — wraps create_agent, keeps AgentResult
    registry.py   builders return LodestarAgent; the seam's purpose is unchanged
  server.py       /agent/chat becomes async def; response shape identical
```

### What deliberately does not change

This list is what makes the rewrite safe to merge:

- **`AgentResult(reply, steps)` and `AgentStep(tool, arguments, result)`** remain the
  brain's own dataclasses. `MUTATING_TOOLS`/`PROPOSING_TOOLS` detection, the `steps` array
  the Assistant view renders, and every eval assertion read `.tool` — none of them should
  learn a framework type.
- **`build_agent(name, ...)`** remains the registry seam: new agents add a builder, never
  edit call sites.
- **Tool names, enum members, and field descriptions** stay byte-identical.
- **`BoardClient`** is untouched, so invariants #1 (never PUT a partial card list) and #2
  (the brain never touches SQLite) sit *below* the rewritten layer and cannot be affected.
- **`BRAIN_LLM`, `BRAIN_MODEL`, `BRAIN_MAX_STEPS`** keep their names and meanings.
- **`SYSTEM_PROMPT`** text is unchanged in this cycle.

### llm/factory.py — the seam

```python
def make_chat_model(settings: Settings, model: str | None = None) -> BaseChatModel:
    if settings.llm_provider == 'fake':
        return FakeChat()
    if settings.llm_provider == 'openrouter':
        return ChatOpenAI(model=model or settings.model,
                          base_url=settings.openrouter_base_url,
                          api_key=settings.openrouter_api_key,
                          timeout=90)
    raise ValueError(f'unknown BRAIN_LLM {settings.llm_provider!r}')
```

- The unknown value **raises**, matching the repo's no-`auto`-modes rule. This is a small
  behaviour improvement: today `BRAIN_LLM=typo` silently becomes openrouter.
- The 90-second timeout is carried over from `OpenRouterProvider`.
- `'ollama'` is the documented next case (`ChatOllama` + the `langchain-ollama` dependency)
  and is deliberately **not** added now.

### llm/fake.py — FakeChat

A `BaseChatModel` subclass implementing `_generate` and `_llm_type`. Both existing modes are
preserved exactly:

- **Scripted:** `FakeChat(script=[AIMessage(...), ...])` pops turns in order.
- **Heuristic (no script):** the last human message starting with `add:` yields one
  `create_question` tool call with `{'title': <rest>}`; once a `ToolMessage` is present in
  the transcript it yields `AIMessage('FAKE: created "<title>"')`; anything else yields
  `AIMessage('FAKE: <text>')`.

**These strings are load-bearing.** `tests/e2e_test.py:955` asserts `FAKE: hello brain`, and
lines 961 and 1001 drive the `add:` path. They must not change.

### tools — `@tool` with explicit Pydantic args models

The factory functions keep their shape — `make_board_tools(client)`,
`make_search_tool(provider)`, `make_retrieve_tool(index, board)`, `make_recall_tool(memory)`
— so dependency injection is unchanged. They now return `list[BaseTool]`.

Args models live beside their tool:

```python
class CreateQuestionArgs(BaseModel):
    title: str = Field(description="the card's text")
    notes: str = ''
    type: Literal['question', 'problem', 'task', 'idea', 'plan'] = 'question'
    category: str = Field('', description="a category id from the user's own registry "
                                          "(e.g. work, love, health — list_questions "
                                          "shows what's in use), or '' for uncategorized")
    column_id: Literal['inbox', 'in-progress', 'answered'] = 'inbox'
    tags: list[str] = []
```

`type` shadows the builtin, exactly as the current signature does; the wire name stays
`type` because both the model and the card schema use it.

`update_question` keeps its semantics: every field except `id` is `X | None = None`, and
only non-`None` values are applied to the target card.

**Schema stability is now testable**, which it was not before: a new
`brain/tests/test_tool_schemas.py` asserts the six tool names, the column enum, the card-type
enum, and that every tool has a non-empty description. This is the tool-schema equivalent of
the "CSS class names are test-stable API" rule.

### agent/runner.py — LodestarAgent

```python
@dataclass
class AgentStep:  tool: str; arguments: dict; result: object      # unchanged
@dataclass
class AgentResult: reply: str; steps: list[AgentStep]             # unchanged

class LodestarAgent:
    def __init__(self, *, settings, tools, system_prompt, max_steps): ...
    def _graph(self, model: str | None):        # compiled agents cached per model name
    def run(self, messages, model=None)  -> AgentResult          # sync
    async def arun(self, messages, model=None) -> AgentResult    # async; used by the route
```

Four mechanics:

1. **Per-request model override.** `create_agent` binds its model at build time, but the
   Assistant view sends `model` per request. `_graph` builds
   `create_agent(model=make_chat_model(settings, model), tools=..., system_prompt=...)` and
   caches by the model string (key `''` for the default). One compile per model per process;
   the picker keeps working.
2. **`max_steps` → `recursion_limit`.** The invoke config carries
   `{'recursion_limit': 2 * max_steps + 1}`.
3. **Step-limit reply.** `GraphRecursionError` is caught and returns the existing
   `'I hit my step limit before finishing — try a smaller request.'` with the steps taken so
   far. If the exception does not carry the partial message list, accumulate via `.stream`
   instead of `.invoke`. The contract is the reply text and the fact that steps are still
   reported — not the retrieval mechanism.
4. **Step extraction.** `_steps_from(messages)` pairs each `AIMessage.tool_calls` entry with
   the `ToolMessage` whose `tool_call_id` matches; `result` is the tool message content,
   JSON-decoded when it parses and the raw string otherwise. Unit-tested directly.

Both `run` and `arun` exist because the eval harness and unit tests are synchronous and
never load MCP tools, while cycle 2's MCP tools are coroutine-only.

### agent/registry.py

`build_agent(name, *, settings, tools, max_steps) -> LodestarAgent`. The keyword changes from
`llm=` to `settings=`, because the factory now needs the whole `Settings` to build a model
per request. `brain/tests/evals/test_registry.py` updates to match.

### server.py

- `/agent/chat` becomes `async def` and awaits `agent.arun(...)`. Doing it in this cycle
  avoids touching the route twice; cycle 2 requires it.
- Chat-memory recording, `MUTATING_TOOLS`, `PROPOSING_TOOLS`, and the JSON response shape
  (`reply`, `mutated`, `proposed`, `steps`) are unchanged.
- The `FakeProvider` / `OpenRouterProvider` imports and the `if settings.llm_provider ==
  'fake'` branch disappear; `build_agent(..., settings=settings)` handles it.

## A deliberate deviation from the LangChain guidance

The ecosystem primer instructs setting `LANGSMITH_TRACING=true` for observability on any
project. **We do not, and we do not default it.** This board holds the user's marriage,
health, and money; tracing would ship those conversations to a third-party cloud service. It
is documented in the README env table as explicitly opt-in, and no code path sets it.

## Collateral: the RAG Lab migrates in the same change

CLAUDE.md's rule is that the lab tracks production seams. It uses the `LLMProvider.chat`
interface in five places, so deleting `llm/base.py` without migrating it would break
`npm run raglab`:

| Site | Change |
|---|---|
| `raglab/index.py::_lab_llm` | returns `make_chat_model(...)` instead of `FakeProvider`/`OpenRouterProvider` |
| `raglab/evaluate.py:38` (`judge_key_facts`) | `llm.chat(msgs)` → `llm.invoke(msgs)` |
| `raglab/retrieval.py:183` (`llm_scores`) | same |
| `raglab/summarize.py` (`LLMSummarizer`) | same |
| `raglab/query.py` (`hyde`) | same |

`.content` access is identical on both sides, so the diff is mechanical. `_lab_llm` builds a
`Settings` from `LabSettings` (which already carries `openrouter_api_key`,
`openrouter_base_url`, `llm_model`), choosing `llm_provider='fake'` when there is no key —
preserving the lab's "runnable with no network at all" property. The result: exactly one LLM
path in the repository instead of two.

## Dependencies

Added to `brain/pyproject.toml` `[project] dependencies`:

```
langchain>=1.0,<2.0
langchain-core>=1.0,<2.0
langchain-openai>=1.0,<2.0
```

(Versions on PyPI at time of writing: 1.3.14 / 1.5.2 / 1.4.1; all require Python ≥3.10 and
the brain is 3.13.) The Node server remains zero-dependency and untouched. The
`docker-compose.yml` pins (`BRAIN_EMBEDDER=fastembed`, `BRAIN_TRANSCRIBER=openrouter`) are
unaffected, so `tests/compose.test.js` needs no change.

## Error handling

| Case | Before | After |
|---|---|---|
| Model names an unknown tool | loop returned `{'error': "unknown tool 'x'"}` as tool content | `create_agent`'s tool node returns an error tool message; the test asserts the reply still recovers, not the exact string |
| A tool raises | caught → `{'error': str(exc)}` fed back to the model | same behaviour, now owned by the tool node |
| Step limit reached | custom reply | `GraphRecursionError` → the same custom reply |
| Provider HTTP error | `httpx` raised out of the route → 500 | `ChatOpenAI` raises → 500. Not in scope to improve. |
| Unknown `BRAIN_LLM` | silently fell through to openrouter | **raises at boot** |

## Testing

Test-first, per the repo policy.

| Test | Change |
|---|---|
| `brain/tests/test_agent.py` | rewritten against `FakeChat` scripted with `AIMessage`s. Same three behaviours: tool-then-reply, unknown-tool + tool-error recovery, step limit |
| `brain/tests/test_llm.py` | now covers `make_chat_model` dispatch, that an unknown `BRAIN_LLM` raises, and `FakeChat`'s two modes |
| `brain/tests/test_tool_schemas.py` | **new** — the six tool names, both enums, non-empty descriptions. It builds tools straight from the four factories with fakes, so all six are present regardless of whether chat memory is configured (in `create_app`, `recall_chat` is still conditional on Chroma) |
| `brain/tests/evals/harness.py` | the script builder emits `AIMessage`s; `scenarios/*.json` unchanged, which is the payoff for keeping `AgentStep` |
| `brain/tests/evals/test_registry.py` | `build_agent(settings=...)` |
| `brain/tests/test_board_tools.py`, `test_server.py` | assertions are on tool names/args/results — **should pass unmodified**. That is the primary regression signal |
| `brain/tests/test_raglab.py` | assertions unchanged; only the lab's internals move to `.invoke` |
| `tests/e2e_test.py` | **unchanged** — `FakeChat` preserves `FAKE: <text>` and the `add:` heuristic verbatim |

**Definition of done:** `uv run --project brain pytest brain/tests -v`,
`npm run test:server`, and `uv run --with playwright python tests/e2e_test.py` all green, and
`git status` clean.

## Risks

1. **Dependency resolution.** The brain pins `numpy<2.5` transitively via numba (the voice
   extra) and gets pydantic v2 from chromadb. `uv sync --project brain` is step one of the
   implementation plan so any conflict surfaces before code is written, not during tests.
2. **`create_agent`'s unknown-tool behaviour** may differ from the hand-rolled
   `{'error': ...}`. The test asserts recovery, not the message text.
3. **Partial steps at the recursion limit** may require `.stream` rather than `.invoke`; see
   mechanic 3 above.
4. **Blocking the event loop.** The route becomes `async`, but `BoardClient`, `DdgsSearch`,
   and the Chroma calls inside tools are synchronous. LangChain's async tool node runs sync
   tool functions in a thread executor, so this is handled by default — but the
   implementation must verify it rather than assume, since a sync `httpx.put` executed
   directly on the loop would stall every other request to the brain.

## Rollback

One branch, one `--no-ff` merge commit. There is no data migration and no schema change, so
reverting the merge is a complete rollback.
