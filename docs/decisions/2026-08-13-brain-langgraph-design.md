# The brain on LangGraph — durable state, middleware, tracing

**Date:** 2026-08-13
**Status:** approved, implementing
**Branch:** `feat/brain-langgraph-autopilot`, cut from `development`

## Why

Three complaints, each with an address in the current code.

**It burns tokens.** Nothing summarises and nothing trims. The browser reassembles
the whole transcript into `ChatBody.messages` every turn, the brain has no memory of
the previous one, and at 80 messages or 120 000 characters it simply refuses
(`server.py:94`). Tool output is fenced verbatim, and `find_related` returns each
card's full `notes`.

**It does redundant work.** `find_related` fetches the entire board over HTTP on
every invocation (`tools/retrieve.py:39`); three tools in one turn means three
fetches. `remember()` writes to Node *and* indexes into Chroma before the turn
returns (`server.py:226`), so the user waits for bookkeeping.

**It is hard to change.** `retrieval.py` is 1449 lines doing eight jobs.

## What this is not

The brain is *already* a LangGraph service: `agent.py:230` calls LangChain 1.x
`create_agent`, which compiles to a `CompiledStateGraph`, and `langgraph 1.2.10`
is already resolved in `uv.lock`. This work does not port anything to LangGraph.
It stops treating the graph as opaque — attaching durable state, expressing
cross-cutting behaviour as middleware, and making the whole turn observable.

Rejected: hand-writing a `StateGraph` ReAct loop. It re-derives a loop LangChain
already ships and tests, and buys no capability the middleware route does not.

Rejected: an outer lifecycle graph (`guard → prefetch → agent → record`) with its
own checkpoints. It buys crash-resume mid-turn, which a single-user local board
will never notice, at the cost of a second graph to reason about.

## The constraint that shapes everything

**Drop-in.** The wire contract does not move. Nine endpoints keep their paths,
request models, status codes and response shapes; `server.js` and `js/` are not
touched. The contract tests are the specification and must pass **unchanged**:

`test_server.py` (32), `test_guardrails.py`, `test_chat_record.py`,
`test_recall_hybrid.py`, `test_tool_schemas.py`, `test_config.py`, `test_voice.py`
(42), `test_pricing.py`, `test_topics.py`, `test_edit_suggestions.py`,
`test_board_tools.py`, `test_tools_retrieve.py`, `test_url_safety.py`,
`test_websearch.py` — plus every Node and e2e test that crosses the proxy.

Specifically preserved: the `_turn_json` envelope, `usage` key names, `cost` that
is `null` and never `0`, the five SSE event names, `calling`/`step` positional
pairing, the `provider` literal set, 413 on both caps, the 400/502 split on
transcribe, the six tool-result row shapes the transcript's `SOURCE_READERS`
parse, `recall`'s chat-then-cards grouping, and the five outbound Node calls —
including the deliberate absence of a whole-board write.

Tests that pin *internals* — `test_agent.py`, `test_retrieval.py`, `test_llm.py`,
`test_recap.py` — are rewritten alongside the code they cover.

## Module layout

```
lodestar_brain/
  agent/      graph.py · state.py · prompt.py · result.py
  middleware/ untrusted.py · errors.py · summarize.py · cache.py · usage.py · memory.py · tracing.py
  retrieval/  embeddings.py · chunking.py · timescope.py · expand.py · fusion.py
              rerank.py · gate.py · cards.py · chat.py
  board/      client.py · snapshot.py
  routes/     chat.py · rag.py · voice.py · models.py
  server.py   create_app() — composition root
```

Every `Alternatives considered` docstring moves with the code it justifies. The
split is a pure move: no behaviour changes in the same commit as a file move.

## Durable state

| | Backend | Scope | Holds |
|---|---|---|---|
| Short-term | `AsyncSqliteSaver` | `thread_id` = chat `session_id` | messages, tool results, step counter |
| Long-term | `SqliteStore` | namespace `("facts", board_port)` | facts across sessions |

Both live in `databases/real/brain-checkpoints.db`, opened in the FastAPI lifespan
and closed with it. `assistant.db` is untouched and Node still owns it: the chats,
trash and history UI read exactly what they read today. The checkpoint database is
the agent's working memory — losing it costs resume, never a record.

Two rules carried over from the board's existing principles:

- **Every long-term memory write appears as a step** in the turn's `steps` array,
  so it renders as a chip like any other tool call. A memory the model can write
  into invisibly is the same mistake as a habit history it can tick.
- **The session still never reaches the model.** `context_schema` +
  `ToolRuntime.context` replaces the `config['configurable']['session_id']` read;
  it is typed, absent from the tool schema the model sees, and — unlike
  `configurable` — not smuggled through the checkpoint.

## Middleware

Authored here, because they encode this board's rules:

| Middleware | Job |
|---|---|
| `untrusted.py` | existing fence; unchanged behaviour |
| `errors.py` | existing `ToolErrorMiddleware` wiring |
| `cache.py` | `wrap_tool_call` result cache, keyed `(tool, arguments, board fingerprint)` |
| `usage.py` | token/cost accounting feeding `_turn_json` |
| `memory.py` | injects relevant stored facts; records writes as steps |

Taken from LangChain 1.3.14 rather than written:

| Built-in | Job |
|---|---|
| `SummarizationMiddleware` | collapse old turns past a token budget |
| `ContextEditingMiddleware` + `ClearToolUsesEdit` | bound tool-output text in context |
| `ToolCallLimitMiddleware` | runaway-loop guard |

`create_agent`'s `cache=` parameter is **inert** — it is forwarded to
`.compile(cache=)`, but `create_agent` sets `cache_policy` on no node, and
LangGraph has no tool-level caching. The cache must therefore be middleware. This
is a framework trap worth writing down.

Middleware order is load-bearing: the fence stays outside the error handler, so a
tool's error string is fenced too.

## Cutting redundant work

- **`BoardSnapshot`** fetches `/api/state` once per turn, fingerprints it, and
  serves `list_cards`, `find_related` and `daily_recap` from the same copy.
  `CardIndex` already computes a blake2b fingerprint and skips re-embedding an
  unchanged board — it simply never got to use it across calls.
- **Recording leaves the response path.** `record_chat` + Chroma indexing become a
  background task. Best-effort semantics are unchanged: a failure is logged, never
  raised, and never turns a delivered reply into a 500.
- **Async throughout.** `BoardClient` moves to async `httpx`; the relevance gate's
  daemon-thread wall-clock budget becomes `asyncio.timeout`.

## Tracing

`BRAIN_TRACING` is a seam like every other backend in this brain: the value names
a backend, and an unknown value raises at boot. No `auto`.

| Value | Behaviour |
|---|---|
| `langsmith` | tracing on; requires `LANGSMITH_API_KEY` or it raises at boot |
| `off` | hard off |

Env default `langsmith`, dataclass default `off` — mirroring `BRAIN_URL_SAFETY`,
so the offline suite and a keyless `docker compose up` stay silent while a real
run traces.

**`off` means off, and that takes more than leaving env unset.** Three verified
behaviours of langsmith 0.10.13:

- `LANGCHAIN_TRACING_V2=true` silently overrides `LANGSMITH_TRACING=false` — the
  `_V2` suffix resolves first across both namespaces.
- Removing the API key does **not** stop egress. It warns, then builds a client
  that calls out anyway.
- The comparison is a strict `== "true"`, so `LANGSMITH_TRACING=1` does nothing.

So `off` calls `langsmith.run_trees.configure(enabled=False)` at boot, which
outranks every environment variable and cannot be defeated by a stale shell
export. A test pins it.

`LANGSMITH_HIDE_INPUTS` / `LANGSMITH_HIDE_OUTPUTS` are honoured by langsmith
itself and need no code. They redact payloads while keeping graph shape, latency
and per-call token counts — which is exactly what the token and redundancy
complaints look like in a trace.

This reverses the standing instruction in `CLAUDE.md` that tracing is never
enabled. That line is rewritten rather than left to contradict the code.

## Testing

Per project policy, one test per way the change can break something; edge cases
are extra asserts inside the test they belong to. Each states its type on the line
above it.

New coverage, one each: checkpoint resume across two turns on one `thread_id`;
summarisation firing past its trigger; a tool-cache hit avoiding a second call;
`BRAIN_TRACING` seam dispatch including the hard-off; a long-term memory write
appearing in `steps`; `BoardSnapshot` collapsing three tool calls to one fetch.

Pre-existing breakage fixed in passing: `test_config.py:229` scans `root/'app.js'`
with no existence guard, and `app.js` has not existed since the ES-module split.

## Risks

**Summarisation changes answer quality.** It is the largest token win and the
change most likely to alter what the model says. Accepted knowingly; the trigger
starts conservative.

**One new dependency.** `langgraph-checkpoint-sqlite>=3.1.1` pulls `aiosqlite` and
`sqlite-vec`. It is the single package behind the checkpointer, the store and the
node cache; nothing else is added.
