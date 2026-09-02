# CLI assistant backends, on the machine that owns the subscription — design

*2026-08-20*

> **Built on 2026-08-20: the first half only.** Sections 1, 4, 5 and the docs
> shipped — a CLI backend is now a per-board choice made in the picker, offered
> only where its binary exists, priced as nothing rather than as zero, and run in
> an empty scratch directory. **The bridge did not ship**, by decision: the whole
> registry, lease and dial-out apparatus below exists to serve one case — a brain
> in Docker reaching a CLI on a *different* machine — and the owner cut it rather
> than carry that much machinery for it. So today the CLI backends work where the
> brain and the logged-in binary are on the same computer, and `docs/cli-backends.md`
> says so on its first screen. Everything below about `bridge.py`, `cli_bridge.py`,
> `/agent/bridge/*`, `BRAIN_CLI_EXECUTOR` and the `CliCall` seam is a **design
> that has not been implemented**; it is kept because the case is real and the
> argument was made, not because the code exists. What did ship is listed under
> *What actually shipped* at the foot of this file.

## What this adds

`llm_cli.py` already lets the brain answer through `claude` or `codex` on this
machine's own subscriptions, keyless. It is selectable **only by `BRAIN_LLM`, and
only where the brain runs**. Two limits fall out of that, and they are different
problems: one brain serves every board the same backend, and a brain in a
container holds neither binary nor anybody's login.

The first is what shipped: a CLI backend becomes a **per-board choice made in the
browser**, so several boards on one endpoint can each answer through a different
subscription. The second is what the bridge below was for.

```
one computer
┌──────────────────────────────────────────────────────────────┐
│ browser → board A   Assistant: Claude CLI ──┐                │
│ browser → board B   Assistant: Codex CLI  ──┤                │
│                                             ▼                │
│ :3000 Node (proxy + limiter) ──→ :9000 brain                 │
│                                    agent loop, tools, fence  │
│                                    └─ claude -p / codex exec │
│                                       (already logged in)    │
└──────────────────────────────────────────────────────────────┘
```

## What is not being built, and why that matters

Three things already exist and are load-bearing here, so this design adds to
them rather than replacing them:

| Already there | Consequence for this design |
| --- | --- |
| `llm_cli.py` — both CLIs as `BaseChatModel`s: hardened argv, prompt-embedded tool calls, per-CLI usage reassembly, 8 offline tests | The agent loop, the untrusted fence, the middleware stack and the proposal gate stay exactly where they are. Only *where the subprocess runs* changes. |
| Multi-board (`docs/decisions/2026-08-12-multi-board-design.md`) — cards and chats scoped by `board_id` | "Board moha uses claude-cli" is expressible from day one. Board scoping is not a later migration. |
| `served_models` / `/agent/models` and its `verified` flag | The picker's existing doctrine — never offer what the backend cannot serve — is the rule the new options obey. |

And one thing is deliberately **not** built: **the MCP inversion.** Exposing the
brain's tools to the CLI over `--mcp-config` and letting the coding agent do the
tool calling is higher fidelity, and `llm_cli.py`'s own `Alternatives considered`
note already rejects it in writing — *"it inverts the architecture … the
untrusted fence and the proposal gate all stop being on the path. That is a
different product."* Nothing here overturns that. Improving the fenced
` ```tool_call ` protocol is likewise out of scope; that file names the
measurement that would justify it.

## Decisions taken

1. **The bridge executes, the brain decides.** Prompt assembly, both hardening
   lists, the tool-call regex and the usage reassembly stay in the brain, where
   they are already tested. The bridge receives four strings and returns three.
2. **argv is never sent over the wire.** A board that could hand a laptop an
   argv to run is a remote-code-execution primitive, whatever the board's
   intentions. The bridge builds the hardened argv from **its own copy** of
   `CLAUDE_HARDENING` / `CODEX_HARDENING`, and a test asserts the two copies are
   identical — the idiom `NEVER_CACHED == server.PROPOSING_TOOLS` already uses.
3. **Both executors run the CLI in a scratch working directory.** `llm_cli.py`'s
   security note names the hazard precisely: `claude -p` boots in whatever
   directory the brain is running in — this repository, next to
   `databases/real/`. The note asks for "a scratch working directory for the
   subprocess instead of the repository root". The bridge gets one, and the
   local executor gets the same one, because it is the same code path and the
   hazard is the same.
4. **A CLI option is offered only when it can actually be served.** Locally that
   means the binary is on `PATH`; remotely it means a bridge is registered for
   this board. Anything else and the option is absent from the picker.
5. **The bridge token is not a login, and the docs must not imply one.** The
   board has no authentication; anyone who can reach it can already read every
   card. `LODESTAR_BRIDGE_TOKEN` stops an unrelated process on the network from
   registering itself as somebody's assistant backend. That is its whole job.

## The executor seam

`_CliChatModel._generate` calls `subprocess.run` directly today. That call
becomes the seam, and nothing else in the class moves:

```python
@dataclass(frozen=True)
class CliCall:
    provider: str          # 'claude-cli' | 'codex-cli'
    model: str             # '' where the backend names none, as codex does
    board_id: str          # which board's bridge may serve this
    argv: list[str]        # hardened, built by _command — LOCAL use only
    stdin: str             # what _command decided to feed the binary
    system: str            # the two pieces argv and stdin were built FROM
    prompt: str

class Executor(Protocol):
    def run(self, call: CliCall, timeout: float) -> CliOutput: ...  # stdout, stderr, returncode

class LocalExecutor:      # today's behaviour, plus the scratch cwd
class BridgeExecutor:     # hands the call to a registered bridge
```

One dataclass rather than seven parameters, because the two executors read
disjoint halves of it and a positional signature would hide that.
`LocalExecutor` runs `argv` with `stdin` — today's behaviour exactly.
`BridgeExecutor` **ignores both** and sends `provider`, `model`, `system`,
`prompt`, per decision 2. The redundancy is the design: the pieces travel, the
command does not, and the bridge rebuilds the command from constants of its own.
`system` and `prompt` cannot be recovered from `stdin` alone — Claude carries the
system text in argv while Codex prepends it to stdin — which is why the call
carries both forms rather than one.

Selection is a property of the settings, not of the model class:
`BRAIN_CLI_EXECUTOR` = `local` (default) | `bridge`. No `auto` — probing for a
binary and silently reaching for a stranger's laptop when it is missing is the
embedder footgun this repo removed twice already. Docker pins `bridge`, because
the image has no binary to run.

**The executor mode is also what the picker's "can this be served?" question
means.** Under `local` a CLI option is offered when the binary is on `PATH`;
under `bridge`, when a bridge is registered for this board. `/agent/models`
reports the mode alongside the bridges, so the frontend never has to guess which
question it is asking.

### Waiting from a synchronous method

`_generate` is synchronous and runs in LangChain's thread executor; the bridge
registry lives on the event loop. So a call is a `concurrent.futures.Future`:
the sync side blocks on `future.result(timeout)` — thread-safe and needing no
loop — and the SSE handler resolves it with `loop.call_soon_threadsafe`. Written
down because the obvious alternative, `asyncio.run_coroutine_threadsafe`, needs
the loop captured at app start and one more thing to get wrong at shutdown.

## The registry

`brain/src/lodestar_brain/cli_bridge.py`. Keyed by `(provider, board_id)`:

- **A bridge with no `--board` serves every board on that provider.** One
  command, no board id to copy, which is what keeps activation a single line for
  someone who is not a developer. A board-specific bridge outranks a general one
  for that board.
- **Two bridges on one key: the newest serves**, and `/agent/models` reports the
  count. "The one you just started" is the only predictable answer, and the
  alternative — refusing — would strand somebody who restarted a laptop.
- **A lease not renewed within twice its length is dropped.** The reconnect *is*
  the liveness signal; there is no separate heartbeat to disagree with it.
- **No bridge, or a call that times out, raises a named error** — never a hang,
  never a silent fall back to another backend. `RuntimeError('no claude-cli
  bridge is connected for this board — start it on your machine')` reaches the
  chat as an error bubble through the path a failing local CLI already takes.

## Protocol

Three routes on the brain, reached through Node's existing `/api/agent/*` proxy
because **the brain's `:9000` is not published** (compose publishes `3000:3000`
only). All three require `Authorization: Bearer $LODESTAR_BRIDGE_TOKEN`.

| Route | Shape |
| --- | --- |
| `GET /agent/bridge/stream?provider=&board=&name=` | SSE. `ready {lease_id, ttl_ms}`, then `call {call_id, provider, model, system, prompt}` per CLI invocation, `ping {}` every 15 s. Closes at `BRAIN_BRIDGE_LEASE` (110 s); the bridge reconnects at once. |
| `POST /agent/bridge/result` | `{call_id, stdout, stderr, returncode}` → 204. An unknown or expired `call_id` is a 404: the turn that was waiting has already given up, and pretending otherwise would resolve a future nobody holds. |
| `GET /agent/models` (existing) | Grows `bridges: [{provider, board, name, count}]`, so the picker offers exactly what is connected. |

**110 seconds is not a taste call**: Node's proxy aborts every upstream request
at 120 s (`AbortSignal.timeout`), so a longer lease would be cut mid-stream and
read as a dropped bridge every two minutes.

One CLI invocation is one `call`, and a turn with a tool round is two — the
lease stays open across them, so the second costs no reconnect.

## `bridge.py` — one file, no dependencies

`brain/src/lodestar_brain/bridge_client.py`, **importing nothing outside the
standard library** (a test asserts it), so it runs anywhere Python 3.13 does —
the same stance `server.js` takes on npm. Node serves it from the `STATIC`
whitelist at `/bridge.py`, one entry pointing at that path, so there is one copy
and activation needs no git checkout:

```sh
curl -O http://home.local:3000/bridge.py
python3 bridge.py --provider codex-cli --url http://home.local:3000
```

Its flags, named so that none of them means two things: `--provider`
(`claude-cli` | `codex-cli`), `--url` (the board's address), `--board` (a board
id — omit it to serve every board), `--name` (what the picker calls this machine;
defaults to the hostname), and the token from `--token` or
`$LODESTAR_BRIDGE_TOKEN`.

Per call it builds the hardened argv from its own constants, runs the binary in a
fresh scratch directory with the system text and prompt exactly as received, and
POSTs `stdout`/`stderr`/`returncode` back. It parses nothing. A non-zero exit is
reported, not interpreted — `llm_cli.py` already turns "run /login" into
something the user can read, and a second interpretation site would be a second
thing to keep in step.

## Frontend

`js/assistant/models.js` gains two options — *Claude CLI — your own
subscription* and *Codex CLI — your own subscription* — rendered only when
`/agent/models` reports a bridge for this board (or a local binary). Model lists
follow the existing curated-and-honest pattern: `sonnet` / `opus` / `haiku` for
Claude, and for Codex **no model at all**, because `PROVIDER_MODELS['codex-cli']`
is deliberately `''` and pinning a slug in the picker would undo that decision.

**The text-provider choice becomes per board.** It is one global key today; with
two boards deliberately on two backends that is the bug this feature would ship.
The existing global value migrates once into the current board's key, the way
`migrateStorageKeys` handled the `question-board:` prefix.

The settings drawer gains a short *Connect a CLI* explainer with the copy-paste
command and a `$LODESTAR_BRIDGE_TOKEN` placeholder. **The token is never
rendered into the DOM.**

## Two `server.js` fixes this depends on

1. **Forward the `authorization` header** on `/api/agent/*`. The proxy forwards
   `content-type` and nothing else today, so a bearer token cannot reach the
   brain at all.
2. **Raise the agent upstream timeout from 120 s to 600 s**
   (`LODESTAR_AGENT_PROXY_TIMEOUT`). This is a live bug independent of this
   feature: `CLI_TIMEOUT` is 300 s and `LOCAL_TIMEOUT` is 600 s, and the proxy
   truncates both — so a slow Ollama turn today dies at two minutes with no
   explanation.

## The pricing lie

`model_prices` returns `Prices(0.0, 0.0)` for every provider that is not
OpenRouter — "local and fake backends have no per-token bill. A known zero." A
CLI backend is neither local nor free: it spends subscription quota, and the
Assistant currently renders that turn as **$0.000**. `llm_cli.py`'s own comment
asserts the opposite ("`model_prices` yields None and the Assistant shows no
figure — which is the honest output"), so the code and its documentation already
disagree and the documentation is the honest one.

Fix: an explicit zero-bill set, `{'ollama', 'fake'}`. Everything else the
catalogue cannot price returns `None`, and `turn_cost` then reports nothing —
which is what `pricing.py`'s docstring says it exists to do.

## Tests

| Type | Test |
| --- | --- |
| unit | `test_llm_cli.py` (extend) — the bridge executor sends `{provider, model, system, prompt}` and **never** argv; the bridge's hardening copy equals the brain's; both executors run in a scratch cwd, not the repo root |
| unit | `test_cli_bridge.py` — newest bridge wins; a board-specific bridge outranks a general one; a stale lease is dropped; no bridge and a timed-out call each raise a *named* error rather than hanging |
| integration | `test_cli_bridge_server.py` — a fake bridge registers over SSE, a `provider=claude-cli` chat reaches it, the reply returns and is recorded in `assistant.db`; a missing or wrong token is 401; `/agent/models` lists the bridge |
| unit | `test_pricing.py` (extend) — a CLI provider prices as `None`; `ollama` and `fake` stay a known zero |
| integration | `tests/server.test.js` — the proxy forwards `authorization`, uses the long agent timeout, and serves `/bridge.py` |
| end-to-end | `tests/e2e_test.py` — a stub `claude` binary behind a real bridge process: pick Claude CLI on a board, get a reply in the transcript; and the CLI options are absent when no bridge is connected |
| live, once | the real `claude` and `codex`, from a laptop, against a brain in Docker. Not in CI. What it settles: that the hardened argv still authenticates through a bridge, and that a tool round survives two calls on one lease. |

## Docs — `docs/cli-backends.md`

Written for someone who is not a developer, because the person activating this
may not be one:

1. Install the CLI (`npm i -g @anthropic-ai/claude-code`, or Codex's own
   instructions) **on your own computer**.
2. Log in with your own account — `claude` then `/login`, or `codex login`.
3. `curl -O http://home.local:3000/bridge.py`
4. `python3 bridge.py --provider codex-cli --url http://home.local:3000`
5. Open your board → Assistant → ⚙ → Text provider → *Codex CLI*.

With the caveats stated plainly rather than in a footnote: the terminal must stay
open, your own subscription is what pays, closing the lid ends the backend and
the picker will say so, and the assistant can still only touch the board through
proposals and suggested edits.

## What this deliberately does not do

- **The MCP inversion**, and no improvement to the fenced-`tool_call` protocol.
  Both are argued in `llm_cli.py`; neither is this branch's job.
- **Load-balancing across several bridges.** Newest wins. A single-user board
  makes a scheduler bookkeeping, not capability.
- **Authenticating people.** The token identifies a *backend*, never a user. If
  the board ever grows logins, this is not the mechanism.
- **Windows.** macOS and Linux are documented; nothing forbids Windows, and
  nothing tests it.
- **Recording which backend answered, per turn.** `assistant.db` already stores
  usage and cost; a `provider` column would let the transcript say *"answered by
  Codex on Pari's laptop"*, which is a good idea and a different change.

## Alternatives considered

### Why not have the brain call a bridge on the LAN?

Simplest possible protocol — the bridge is an HTTP server, the brain holds an
allowlist of names to URLs, no registry, no lease, no SSE. It was rejected on the
activation story, which is the requirement this feature exists to serve: it needs
a stable LAN address (DHCP breaks it), a firewall approval on macOS, a container
that can resolve a `.local` name, and it stops working the moment either laptop
leaves the house. Worst of all, activating it means editing the **server's** env
and restarting it — so "she activates it on her own computer" becomes "she asks
me to reconfigure the server", which is the thing being fixed.

### Why not let the browser talk to the bridge directly?

No connectivity problem at all: the bridge is on `localhost` for the person using
it. But sessions, recording, the drift nudge, pricing, url-safety and the
untrusted fence all live behind the brain's chat routes, and this would move them
outside — duplicated per bridge, and forked from the invariants the moment either
copy changed. Rejected on principle, not on effort.

### Why not `claude-agent-sdk`, or the Anthropic/OpenAI SDKs?

Same answer `llm_cli.py` already gives, unchanged by this design: the constraint
is keyless operation on subscriptions this machine already has, and an SDK
authenticates with a key that would have to be issued, paid for, and stored in a
repo whose fifth invariant is that no key reaches the browser. This design does
not touch that decision — it only moves *where* the subprocess runs.

### Why is the executor a seam rather than a second chat model?

`BridgeCliChatModel` subclasses would duplicate `_command`, `_parse` and the
usage reassembly per CLI — four classes for two binaries, with the hardening
lists reachable from four places. The thing that varies is the transport, so the
transport is the seam. It also keeps the eight existing `test_llm_cli.py` cases
covering the remote path for free: they assert on argv and parsing, and both are
still built and done in the brain.

## What actually shipped (2026-08-20)

| Change | Where |
| --- | --- |
| `UI_PROVIDERS` grew both CLI backends; a browser-named CLI is built by `_cli_model`, behind the `fake` guard, and `PROVIDER_MODELS` still moves the model with the provider | `llm.py` |
| `BRAIN_MODEL` now reaches a CLI backend, but only when the brain is *configured* for it — a slug meant for Ollama must not be handed to Claude. `BRAIN_CLAUDE_CLI_MODEL` is gone: two knobs for one model is two things to disagree | `llm.py`, `.env.example` |
| `served_models` grew `cli: {backend: bool}` from `shutil.which`, so the picker offers only an installed subscription | `llm.py` |
| `ChatBody.provider` accepts both CLI values — they were a 422 from the request model before the seam was ever reached | `server.py` |
| `ZERO_BILL = {'ollama', 'fake'}`. A subscription turn prices as `None`, not `$0.000`, which is what `llm_cli.py`'s comment already claimed | `pricing.py` |
| Every invocation runs in a fresh empty temp directory — the fix this file's own security note asked for, and a prompt-integrity fix as much as a filesystem one | `llm_cli.py` |
| Two picker options offered per board, labelled *your own subscription*; `sonnet`/`opus`/`haiku` for Claude and no model for Codex; a saved backend the brain can no longer serve is switched off rather than left to fail | `js/assistant/models.js` |
| The models key is board-scoped (`boardSuffix`), so boards differ and the default board's existing pick does not move | `js/assistant/models.js` |
| Activation guide, written for someone who is not a developer, opening with the one thing that decides whether any of it works | `docs/cli-backends.md` |

Tests: `test_llm.py` (UI selection, `served_models`' `cli`), `test_llm_cli.py`
(the scratch cwd, measured from inside the subprocess — the only place it can be,
since a `TemporaryDirectory` is gone by the time a test could look),
`test_pricing.py` (folded into `test_a_price_is_never_invented`, where the rule
lives), `test_server.py` (the wire contract and `cost is None`), and five e2e
checks including per-board persistence across a board switch.

**Not shipped, and not only the bridge:** the two `server.js` fixes this file
lists — the proxy forwarding no `authorization` header, and its 120 s abort
truncating both `CLI_TIMEOUT` (300 s) and `LOCAL_TIMEOUT` (600 s). The second is
a live bug for Ollama today and wants its own `fix/` branch; neither is reachable
from the shipped feature, because a CLI backend selected in the picker is served
by the brain the browser is already talking to.
