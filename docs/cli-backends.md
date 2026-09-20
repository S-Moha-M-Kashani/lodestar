# Using your Claude or Codex subscription as Lodestar's Assistant

*Last updated 2026-09-17*

Lodestar's Assistant can answer through the `claude` or `codex` command-line
tool, signed in **on your own computer** or **in the container** the brain runs
in. No API key, no billing setup, no extra account: the subscription you already
pay for monthly is the credential, and Lodestar never sees it.

Each board picks its own backend, so two people sharing one Lodestar can each
answer through their own subscription.

## Before you start: where does Lodestar run?

This is the one thing worth checking first, because it decides *how* you sign in,
not whether you can.

Lodestar has two parts: the **board** (what you see) and the **brain** (what
answers). The brain is what runs the CLI, so **the subscription has to be signed
in on the machine the brain runs on.**

| How you run Lodestar | What to do |
| --- | --- |
| `npm start` + the brain, both on your own laptop | Nothing. You are already logged in — skip to Step 4. |
| Docker on this same laptop | **Codex:** press *Sign in* in the Assistant's settings and approve the code. **Claude:** paste a token (Step 3). |
| Docker on a home server, opened from another laptop | The same, and the subscription is the one you sign the *container* in with. |

Since 2026-09-17 the brain's image carries both programs, and the composed brain
answers through **Codex by default**. What changed and what did not:

- **Codex signs itself in, from the board.** `codex login --device-auth` prints a
  link and a one-time code and waits. So the container needs no browser of its
  own and no port opened to it: you read the code on the board, approve it in a
  browser you already have open, and the container ends up logged in. The login
  lives in a Docker volume of its own — never in `databases/`, which is what the
  backup copies to Google Drive, and never in the working tree, which is what git
  publishes.
- **Claude cannot do that, and it is the platform's doing.** Claude Code's login
  is a terminal program, and on a Mac what it writes is a **Keychain** entry —
  your operating system's own password store, which no Linux container can read
  and no mount can share. The one form of that credential a container can be
  *given* is a long-lived token from `claude setup-token`, which is Step 3.
- **The container gets its own login, not yours.** Nothing mounts `~/.codex` or
  `~/.claude` from your machine. That is deliberate: sharing the host login would
  hand a container that runs whatever a model writes the same token your own
  terminal uses, and it would be revocable only by revoking yours.

A first turn on a freshly built stack will fail until that login is done once.
What you see is Codex's own message with the cure added to it: *401
Unauthorized: Missing bearer … — the Codex subscription is not signed in where
the brain runs.*

## Step 1 — install the CLI on your own computer

Claude Code:

```sh
npm install -g @anthropic-ai/claude-code
```

Codex:

```sh
npm install -g @openai/codex        # or: brew install codex
```

## Step 2 — log in, once, on your own computer

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

**Skip this step if you run Lodestar in Docker** — the container is a different
machine and your own login is not in it. Step 3 is the Docker way in.

## Step 3 — in Docker: sign the container in

Open the board → **Assistant** → the **⚙** beside the theme picker → unfold
**Models** → **Your subscriptions**. One row per subscription, each saying what
the brain found.

**Codex — press Sign in.** The row turns into a link and a one-time code:

1. open the link (any device, any browser — it does not have to be this one);
2. type the code;
3. the row says so by itself. Nothing to press afterwards, and nothing to copy
   back.

The code expires in about fifteen minutes. If you miss it, press *Sign in* again
for a fresh one.

**Claude — paste a token.** On the machine where you are already logged in:

```sh
claude setup-token        # prints sk-ant-oat01-…
```

Paste it into the **Claude** row and press *Save*. It is stored the way the
OpenRouter key is — the brain keeps it, the field empties itself, and no page
ever reads it back. To make it survive a restart, put it in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN=…` instead; `docker compose` passes it through.

A row that says *cannot be checked from here* is not a problem: on a Mac the
Claude login is in the keychain, so there is nothing for the brain to look at.
It is saying it does not know, rather than guessing.

## Step 4 — start Lodestar the ordinary way

```sh
npm start                                                   # the board, on :3000
BRAIN_EMBEDDER=fake uv run --project brain \
  uvicorn lodestar_brain.server:app --port 9000              # the brain
```

Nothing about the brain's configuration needs to change. `BRAIN_LLM` can stay
whatever it is — the picker chooses per board, per request.

## Step 5 — choose it on your board

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

## Step 6 — per board

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
| The CLI option is missing from the picker | The brain can't find the binary. Check `which claude` / `which codex` **in the same shell you start the brain from**, and restart the brain. In Docker it is in the image, so a missing option there means the image predates 2026-09-17 — rebuild it. |
| The option is there, and every turn fails saying you are not logged in | The binary is installed and the subscription is not signed in. The **Your subscriptions** rows in the ⚙ drawer say which, and hold the way to fix it. The picker offers a backend the brain *can run*; being signed in is a separate question, asked where it can be answered. |
| *Sign in* says codex is not installed where the brain runs | The brain is running somewhere without it — a native brain on a machine with no `codex`, most likely. Docker images built since 2026-09-17 carry both. |
| The code expired | Press *Sign in* again. Nothing is kept from the old one. |
| The option was there, then vanished and switched back to Ollama | The brain looked again and the binary was gone (uninstalled, or a `PATH` that differs between shells). |
| A reply that mentions logging in | The subscription's login expired. Run `claude` → `/login`, or `codex login`, then try again. |
| The turn takes a long time | Expected. These are agents that boot a session before answering; Lodestar waits up to 5 minutes. |
| An error naming an exit status only | Run the same command by hand (Step 2) — the CLI's own message is the useful one. |

If the binary lives somewhere unusual, name it directly in the brain's
environment: `BRAIN_CLAUDE_CLI_BIN=/opt/homebrew/bin/claude`,
`BRAIN_CODEX_CLI_BIN=…`.
