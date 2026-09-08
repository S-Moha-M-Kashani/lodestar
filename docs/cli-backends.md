# Using your Claude or Codex subscription as Lodestar's Assistant

*Last updated 2026-09-08*

Lodestar's Assistant can answer through the `claude` or `codex` command-line tool
that is **already installed and already logged in on your computer**. No API key,
no billing setup, no extra account: the subscription you already pay for
monthly is the credential, and Lodestar never sees it.

Each board picks its own backend, so two people sharing one Lodestar can each
answer through their own subscription.

## Before you start: where does Lodestar run?

This is the one thing worth checking first, because it decides whether the option
appears at all.

Lodestar has two parts: the **board** (what you see) and the **brain** (what
answers). The brain is what runs the CLI, so **the CLI has to be installed and
logged in on the machine the brain runs on.**

| How you run Lodestar | Does this work? |
| --- | --- |
| `npm start` + the brain, both on your own laptop | **Yes.** This is the case this feature is for. |
| Docker on this same laptop | **No** — the brain is inside a container that has no `claude`/`codex` and no login of yours. |
| Docker on a home server, opened from another laptop | **No** — same reason, and the subscription would be the server's, not yours. |

If you are on Docker, your options today are Ollama (local, free, private) or
OpenRouter (an API key).

### Under Docker, neither subscription works — and here is why, one at a time

Checked on 2026-09-08 by asking the running container itself: it reports both
subscriptions unavailable, and neither `claude` nor `codex` is inside it — nor
Node, which both of them need to run.

- **Claude: not possible, not merely not done.** Your Claude login is not a
  file. Claude Code keeps it in the **macOS Keychain** — your operating system's
  own password store — and a Linux container cannot read a Keychain at all.
  There is no path to share and nothing to mount, so no amount of Docker
  configuration reaches it.
- **Codex: possible, deliberately not built.** Your Codex login *is* a file
  (`~/.codex/auth.json`), so a container could in principle be given it. Three
  costs stopped it, and none of them buys you anything you cannot already have:
  the brain's image would need a Node runtime and the `codex` program added on
  top of the 18 GB it already weighs; a live paid-subscription token would be
  handed to a container that runs whatever the model writes; and sharing it
  read-only would likely break Codex's own renewal of that token. If you want
  this anyway, say so — it is a decision, not an oversight.

**So the way to use either subscription is to run the brain on your own
machine** — Step 3 below. Everything else can still be in Docker: `docker
compose up chroma` gives you chat memory, and the board and the brain run from
your own user account, which is where your logins already live.

The older sketch of a container reaching the CLI on the host is in
`docs/decisions/2026-08-20-cli-assistant-backends-design.md`. It was weighed
again on 2026-09-08 and turned down: it would hand a container the ability to
run a coding assistant on your laptop, which is a much bigger permission than
answering a chat message.

## Step 1 — install the CLI on your own computer

Claude Code:

```sh
npm install -g @anthropic-ai/claude-code
```

Codex:

```sh
npm install -g @openai/codex        # or: brew install codex
```

## Step 2 — log in, once

```sh
claude          # then type /login and follow the browser prompt
codex login
```

That login lives in your own user account on your own machine. Lodestar reads
neither it nor your password — it only runs the command, the way you would.

Check it worked:

```sh
claude -p 'reply with the word ready'
codex exec 'reply with the word ready'
```

If those answer, Lodestar will too.

## Step 3 — start Lodestar the ordinary way

```sh
npm start                                                   # the board, on :3000
BRAIN_EMBEDDER=fake uv run --project brain \
  uvicorn lodestar_brain.server:app --port 9000              # the brain
```

Nothing about the brain's configuration needs to change. `BRAIN_LLM` can stay
whatever it is — the picker chooses per board, per request.

## Step 4 — choose it on your board

Open the board → **Assistant** → the **⚙** beside the theme picker → unfold
**Models** → **Text provider**:

- *Claude CLI — your own subscription*
- *Codex CLI — your own subscription*

**An option you don't see is an option that can't work.** Lodestar asks the
brain which CLIs are actually installed where it runs, and offers only those —
so a missing entry means the brain cannot find that binary, not that you picked
wrong.

For Claude you also get a model choice: **sonnet**, **opus** or **haiku**. Codex
has none, deliberately — it runs on whatever model Codex itself defaults to.

## Step 5 — per board

The choice is remembered **per board**. Your board can answer through Claude
while another board on the same Lodestar answers through Codex; neither knows
about the other, and switching boards switches backends.

## What this costs, and what it shows

A turn spends your subscription's quota. It is **not** free and it is **not**
billed per token, so the Assistant shows the tokens a turn used and **no dollar
figure at all** — rather than a `$0.000` that would read as "that was free".
(With OpenRouter you get a real price, because there the per-token rate is
published.)

## What the Assistant can and cannot do on this backend

Everything it does on any other backend: search the web, search your cards and
past chats, propose cards, suggest edits, write the daily recap. Card writes stay
proposals you approve — the CLI cannot write to your board any more than the
other backends can.

One honest limitation: on a CLI backend the Assistant asks for one tool at a
time, and it asks for it by writing a small block of text that Lodestar reads
back. It works, but it is less precise than the API backends, so complicated
multi-step requests may take more turns.

## Safety — what Lodestar does to the command it runs

`claude` and `codex` are not plain question-answering endpoints; they are coding
assistants with a shell, file access and their own instructions. Lodestar takes
all of that away before every single call:

- every built-in tool switched off, no MCP servers, no execpolicy rules;
- a read-only sandbox (Codex) and no user or project settings (both);
- no session file left behind;
- and it runs in an **empty scratch folder**, so it can't reach your files by a
  relative path and can't pick up instructions from a `CLAUDE.md` or `AGENTS.md`
  lying around.

This matters because your card text goes into the prompt, and a card is
something anyone could have written into. Details, and what is still unproven,
are in `brain/src/lodestar_brain/llm_cli.py`.

## If something goes wrong

| What you see | What it means |
| --- | --- |
| The CLI option is missing from the picker | The brain can't find the binary. Check `which claude` / `which codex` **in the same shell you start the brain from**, and restart the brain. |
| The option was there, then vanished and switched back to Ollama | The brain looked again and the binary was gone (uninstalled, or a `PATH` that differs between shells). |
| A reply that mentions logging in | The subscription's login expired. Run `claude` → `/login`, or `codex login`, then try again. |
| The turn takes a long time | Expected. These are agents that boot a session before answering; Lodestar waits up to 5 minutes. |
| An error naming an exit status only | Run the same command by hand (Step 2) — the CLI's own message is the useful one. |

If the binary lives somewhere unusual, name it directly in the brain's
environment: `BRAIN_CLAUDE_CLI_BIN=/opt/homebrew/bin/claude`,
`BRAIN_CODEX_CLI_BIN=…`.
