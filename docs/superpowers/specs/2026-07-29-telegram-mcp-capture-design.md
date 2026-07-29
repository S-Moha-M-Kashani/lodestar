# Telegram capture via MCP — design

**Date:** 2026-07-29
**Status:** approved, not yet implemented
**Cycle 5 of 5.** Roadmap: Tooka Farsi embedder → LangChain agent rewrite → model registry &
pickers → factor explainers → **this**. Depends on
`2026-07-29-langchain-agent-rewrite-design.md`; do not start until that cycle is merged and the
whole suite is green.

## Why

The product's first pillar is *never lose a thought*, but capture today requires opening the
board in a browser. A thought had on a walk goes into whatever app is nearest and is lost.
Telegram is already on the phone, already has a chat with a bot, and its Bot API is two HTTP
calls.

Exposing it as a **real MCP server** rather than another in-process tool buys one specific
thing: the same server binary attaches to Claude Code (and, later, to a remote Claude client)
without being rewritten. Lodestar's capture channel stops being Lodestar-only.

## Topology

```
you ──► [@your_bot] ──► Telegram cloud            getUpdates, 24h retention
                              ▲ https
              lodestar-telegram-mcp                stdio subprocess, spawned by the brain
                              ▲ MCP stdio
              brain :9000  MultiServerMCPClient → LangChain tools → create_agent
                              │ create_question
              Node :3000   POST /api/proposals     pending = 1
                              │
              Assistant view → you tap Accept → the card joins the board
```

**Inbound only, on demand.** Nothing runs in the background; the agent reads Telegram when
asked ("file anything new from my telegram"). No new port, no compose service, no entry in
`tests/ports.test.js` — which matters more now that the RAG Lab holds :9002.

## Non-goals

- **No outbound messages.** No `send_message`, no digests or nudges pushed to Telegram.
- **No background worker and no webhook.** Both were considered; the on-demand tool ships
  first because it adds no daemon, no restart semantics, and no public HTTP surface. The
  worker is the natural cycle 3 (see "The limitation" below for why it will eventually be
  wanted).
- **No media.** Photos, voice notes, and documents are ignored; text only.
- **No remote MCP transport.** Attaching to claude.ai in the browser needs HTTPS, OAuth, and
  a publicly reachable host. Out of scope; the stdio server is the thing a future cycle
  would wrap.

## Where the code lives — and what it may not import

A **second top-level package in the same uv project**:

```
brain/
  pyproject.toml            packages = ["src/lodestar_brain", "src/lodestar_telegram"]
                            [project.scripts] lodestar-telegram-mcp = "lodestar_telegram.server:main"
  src/lodestar_telegram/
    __init__.py
    bot.py                  Bot API client over httpx: get_updates, cursor read/write
    server.py               FastMCP server exposing exactly two tools; main()
```

The split is structural, not cosmetic:

- **`lodestar_telegram` must not import `lodestar_brain`.** It is a separate process that has
  to run standalone under Claude Code, with no board settings and no brain on the machine.
- **`lodestar_brain` must not import `lodestar_telegram`** either — it only spawns a command
  by name. The dependency graph stays acyclic, the same discipline the RAG Lab follows in the
  other direction.

Built on `FastMCP` from the official `mcp` SDK (2.0.0), talking to the Bot API with `httpx`,
already a brain dependency. Deliberately **not** `python-telegram-bot`: it brings an entire
polling and event-loop framework for what amounts to two GET requests.

## The two tools

| Tool | Returns | Does not |
|---|---|---|
| `list_telegram_messages(limit: int = 50)` | `[{update_id, date, chat_id, text}]` from the stored cursor forward | advance the cursor |
| `ack_telegram_messages(up_to_update_id: int)` | `{acked: int, cursor: int}` | read anything |

### Why two tools and not one — the load-bearing decision

`getUpdates(offset=N)` **confirms and permanently discards** every update below `N`. If a
single tool both read and advanced the cursor, then a crash between reading and filing — or
the model simply choosing not to create a card — would destroy the message.

That directly contradicts the durability promise: a thought is destroyed only by delete →
"Delete permanently". So:

- **reading is non-destructive and repeatable** — it always calls `getUpdates` with the
  stored cursor and never a higher offset;
- **acking is a separate, explicit act** performed after the proposals exist.

The worst case becomes *the same message proposed twice*, rejected in one click. That is the
correct direction to fail.

The cursor is a small JSON file at `TELEGRAM_CURSOR_FILE`, defaulting to
`~/.lodestar/telegram-cursor.json` — user-scoped rather than repo-relative, because the server
also runs standalone under Claude Code from an arbitrary working directory. It is **not**
stored in `board.db`: only the Node API owns that database, and this process is not the Node
API.

## The limitation, stated plainly

Telegram retains unconfirmed updates for **24 hours**. Because reading is on demand, a
message sent and never asked about within a day is gone — Telegram's retention, not ours.

This is the one seam where "never lose a thought" is weaker than the board's own guarantee.
It is documented in the README and CLAUDE.md rather than hidden, and it is the concrete
argument for the always-on ingest worker in a later cycle. The worker would call the same
`bot.py` functions; nothing in this design has to be undone to add it.

## Security

A Telegram bot can be messaged by anyone who discovers its username. Without a filter, a
stranger's text becomes a card in a personal life dashboard — and, worse, becomes text the
agent *reads*, i.e. prompt injection aimed straight at the board tools. Three layers:

1. **`TELEGRAM_ALLOWED_CHAT_IDS`** — a required comma-separated allowlist, where **empty
   means reject everything**. The server still starts and the tool still answers, returning
   an empty list and an explicit "no chat ids are allowed" note. No silent pass-through,
   matching the brain's no-`auto`-modes policy: a misconfiguration must be visible.
2. **The existing proposal gate.** Every Telegram-derived card goes through
   `POST /api/proposals` with `pending = 1`, so even content that clears the allowlist cannot
   reach the board without an explicit Accept. The confirmation gate built in the previous
   cycle turns out to be exactly the right primitive for an untrusted inbound channel.
3. **The system prompt** gains a sentence marking Telegram message text as *data, never
   instructions*.

`TELEGRAM_BOT_TOKEN` lives only in the MCP server's environment, passed through by the brain
— the same containment rule as the LLM key (invariant #5): the browser never sees it.

## Wiring in the brain

`config.py` gains:

```python
telegram_mcp: str = 'off'         # BRAIN_TELEGRAM_MCP: 'off' | 'stdio'; unknown raises
telegram_bot_token: str = ''      # TELEGRAM_BOT_TOKEN
telegram_allowed_chats: str = ''  # TELEGRAM_ALLOWED_CHAT_IDS
telegram_cursor_file: str = ''    # TELEGRAM_CURSOR_FILE; '' = the server's own default
```

**Off is the default**, so no existing unit test and no e2e run ever spawns a subprocess.

In `create_app`'s lifespan hook (async, because `get_tools()` is):

```python
if settings.telegram_mcp == 'stdio':
    client = MultiServerMCPClient({'telegram': {
        'transport': 'stdio',
        'command': 'lodestar-telegram-mcp',
        'env': {'TELEGRAM_BOT_TOKEN': settings.telegram_bot_token,
                'TELEGRAM_ALLOWED_CHAT_IDS': settings.telegram_allowed_chats,
                'TELEGRAM_CURSOR_FILE': settings.telegram_cursor_file}}})
    tools += await client.get_tools()
```

**Every variable the server needs must appear in that `env` dict.** An explicit `env` in the
MCP stdio config replaces the child's environment rather than extending it, so a variable left
out is a variable the subprocess does not have — which is why `TELEGRAM_CURSOR_FILE` is passed
through even though the server has its own default. The implementation must confirm the
replace-versus-merge behaviour of the installed `mcp` version and pass whatever else the child
needs (e.g. `PATH`) accordingly.

If the spawn or the tool listing fails, **log loudly and serve on without the Telegram
tools** — the identical policy to the existing Chroma fallback in `create_app`, for the
identical reason: a stopped side-car must not take down the board's assistant.

MCP tools are coroutine-only, which is why cycle 1 already moved `/agent/chat` to `async def`
and added `LodestarAgent.arun`.

## Standalone use under Claude Code

Because this is a real MCP server, the same binary attaches with no extra work:

```json
{ "mcpServers": { "telegram": {
  "command": "uv",
  "args": ["run", "--project", "brain", "lodestar-telegram-mcp"],
  "env": { "TELEGRAM_BOT_TOKEN": "…", "TELEGRAM_ALLOWED_CHAT_IDS": "…" } } } }
```

## Dependencies

Added to `brain/pyproject.toml`:

```
mcp>=2.0
langchain-mcp-adapters>=0.3
```

`httpx` is already present. Docker needs only the two new env vars; the console script ships
in the image because it is part of the same wheel.

## Error handling

| Case | Behaviour |
|---|---|
| No `TELEGRAM_BOT_TOKEN` | the server starts, tools return a clear "no bot token configured" error — never a crash loop that the brain would keep respawning |
| Empty allowlist | empty result plus an explicit note; never a pass-through |
| Message from a non-allowed chat | dropped before it reaches the model, not surfaced as an error |
| Update with no `text` (photo, sticker, join event) | skipped; the cursor still advances past it on ack, so it cannot wedge the queue |
| Bot API returns non-200 or malformed JSON | raised as a tool error the model can report, not an unhandled exception |
| `ack_telegram_messages` with an id below the cursor | no-op, returns the current cursor |
| Cursor file missing or corrupt | treated as cursor 0 and rewritten; the cost is re-reading, which is safe by design |
| MCP subprocess dies mid-session | the tool call fails, the agent reports it; the brain keeps serving |

## Testing

| Test | Covers |
|---|---|
| `brain/tests/test_telegram_bot.py` (respx) | read-forward does not advance the cursor; ack advances it; a foreign `chat_id` is dropped; **an empty allowlist rejects everything**; text-less updates are skipped; a Bot API HTTP error becomes a tool error, not a crash; a corrupt cursor file is recovered |
| `brain/tests/test_telegram_mcp.py` (mcp in-memory transport) | exactly two tools are listed, their schemas are correct, and a call round-trips — fully offline |
| `brain/tests/test_config.py` | `BRAIN_TELEGRAM_MCP` defaults to `off`; an unknown value raises |
| `brain/tests/test_server.py` | with Telegram off, the tool list is exactly cycle 1's — the five board/search/RAG tools, plus `recall_chat` when chat memory is configured — which guards the default |
| `brain/tests/evals/scenarios/telegram_capture.json` | "file my telegram notes" → `list_telegram_messages` → `create_question` ×2 → `ack_telegram_messages`, asserting **the ack comes after the proposals**. That ordering is the invariant worth locking down |
| `tests/e2e_test.py` | unchanged — Telegram is off |

**Definition of done:** the full brain suite, the Node server suite, and e2e all green with
Telegram off; plus one manual smoke run with a real bot token and the allowlist set,
confirming a message becomes a pending proposal and that rejecting it lands the card in Trash
rather than hard-deleting it.

## Documentation to update

- **README** — the env-var table gains `BRAIN_TELEGRAM_MCP`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_CURSOR_FILE`, plus a short "capture from Telegram"
  section including the 24-hour retention limit and the `.mcp.json` snippet.
- **CLAUDE.md** — a section covering the read/ack split and why it exists, the allowlist
  requirement, and the rule that `lodestar_telegram` and `lodestar_brain` must not import
  each other.

## Risks

1. **stdio session lifecycle.** `langchain-mcp-adapters` may open a fresh subprocess per tool
   call rather than holding one session. For infrequent on-demand reads that is acceptable;
   if the spawn cost proves noticeable, switch to the client's persistent-session context
   manager held for the app's lifetime.
2. **Duplicate proposals** are the accepted failure mode when the model forgets to ack. If it
   happens often in practice, the fix is a prompt change, not a design change.
3. **`mcp` 2.0 API surface.** FastMCP's import path and `main()` signature must be verified
   against the installed version rather than assumed.

## Rollback

One branch, one `--no-ff` merge commit, and the feature is off by default — so reverting the
merge is complete, and even an unreverted merge changes nothing until `BRAIN_TELEGRAM_MCP` is
set.
