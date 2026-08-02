# Lodestar

*Your compass for life!* Lodestar is a place to get everything open out of your head, see what actually matters, and never lose a thought — so you can follow through, privately and at work. A kanban-style board that is one dashboard for your whole life — work questions, plans with your partner, sports, music, reading, holidays — everything in one place. Vanilla HTML/CSS/JavaScript front end, with an optional tiny server that persists the board to a local **SQLite** database. No build step and no npm dependencies — the server uses Node's built-in `node:sqlite` and `http`.

The design is a "card ledger": quad-ruled engineering paper, cards as ruled index cards with permanent ledger IDs (`C-001`, `C-002`, …), card types as ink stamps, and life-area categories as coloured index tabs — every category gets its own ink, in all four themes.

## Features

- **Three-column lifecycle**: Inbox → In Progress → Done
- **Seven views**: **Board** (the columns), **Backlog** (the Inbox as a scannable ledger list), **Overview** (a semantic map — see below), **Matrix** (four decision lenses on one grid), **Areas** (which life area is starved), **Review** (the weekly sweep), and **Assistant** (chat with the brain — see below)
- **Drag & drop** cards within and between columns, with drop-position indicator
- **Card types**: question, problem, task, idea, plan, habit — stamped on each card in neutral ink, with a per-column sort menu (by deadline, priority, type, newest or oldest)
- **Habits**: a card you repeat rather than finish. Set how many times per day, week, month or year, and optional clock times to be reminded at. Each habit wears a **punch strip** — one box per repetition the period asks for, stamped in the card's own category ink; click the next open box to record one, click the newest stamp to take it back. A rail beside the board lists what is due, a banner and one short beep announce it when the board opens or a reminder time passes (mute it from the Menu), and `↻ history` opens a tape of past periods on the card. A habit moved to Done is retired: it stops reminding and keeps its history
- **Categories**: nine life areas to start (Work, Love, Family, Health, Mind, Music, Travel, Home, Money), each with its own colour — and the set is **yours**: add or remove areas from the ✎ tab on the rail, pick a hue for each, up to 24. Cards carry a coloured spine, and the rail of coloured tabs under the header filters the board to one life area
- **Importance & urgency**: set each to High or Low on a card to place it on the **Matrix**
- **Deadlines and automatic priority**: give a card an ISO date and it carries a deadline chip that turns red once overdue. Priority is **derived**, never stored — P1 urgent & important, P2 urgent, P3 important, P4 neither — so it can never disagree with the two judgements behind it; the toolbar filters by it
- **Matrix — four lenses on the same cards**: importance is always the vertical axis, and the picker swaps what it is crossed with — **Eisenhower** (urgency; Answer now / Schedule / Delegate / Drop, urgent on the left), **Leverage** (effort — where a little work moves a lot), **Serenity** (control — what deserves action, and what you are allowed to put down), and **Follow-through** (time since a card was last touched)
- **Overview map**: every card is plotted by meaning — it is embedded and reduced to two axes, so cards that read alike sit close together. What gets embedded is **the whole card as you filed it**: its tags, its category, its type, its title and its notes. The labels are part of the meaning, not decoration — two cards with the same words under *Health* and under *Work* are not the same thought, and on title and notes alone they landed on the same dot. Labels come first in the sentence, because the model truncates from the tail and a long note must never be able to push a card's category out of its own vector; the category contributes the name you gave it, so renaming an area re-embeds the cards in it. Toggle the projection between **PCA** (fast, stable global axes) and **t-SNE** (local neighbourhoods, so clusters of related thoughts pull together); t-SNE is seeded, so the same cards always land in the same spots. Embeddings come from a HuggingFace model ([Transformers.js](https://huggingface.co/docs/transformers.js), `Xenova/all-MiniLM-L6-v2`) loaded on demand from a CDN — **no login or API key needed**; the ~30 MB model downloads once and is cached by the browser. When it's still loading or the browser is offline, the map falls back to a keyword-overlap layout, so it always renders with no network. Hover a dot for details, click to open the full editor, and the tag/type/category/search filters apply just like the board. Dots are inked in their category's colour, so the map reads by life area.
- **Areas view**: one small-multiples tile per life area plus an attention wheel whose spoke length is open-card mass, answering "which part of my life is starved?" at a glance. Click a tile to focus that area and open a category-aware detail panel — cooling-off (the 30-day rule), learning progress, serenity check, and what has been longest untouched
- **Review view**: GTD's weekly review as a screen — stat tiles (inbox, answered this week, new this week, open in total), week-over-week drift per area, the neglect list (important cards untouched for over a month), and three old thoughts resurfaced on purpose. The picks are seeded on the date and pinned for the day, so acting on one never reshuffles the others
- **Quick capture**: write anything into the Inbox and press Enter
- **Edit modal**: notes, type, category, tags, importance and urgency, effort, control, and a deadline per card. Tag a card `decision` and notes lines starting with `+` or `−` read back as a two-column pro/con balance sheet
- **Search & filters**: free-text search, type filter, priority filter, category tabs, tag chips
- **Persistence**: runs on localStorage on its own; when the server is running it also saves to a SQLite database, so cards survive restarts and reopen on any browser. See **How your data is stored** below for the exact durability guarantee. Export/Import as JSON for backups
- **Database sync rule**: a browser that already has its own board keeps it (and pushes it to the server), so unsynced local edits are never clobbered; a fresh browser loads the board from the database
- **One Menu button**: Undo, History, Export and Import fold into a single expanding menu in the toolbar
- **Export dialog**: download `lodestar.json`, or copy the JSON when the browser blocks downloads
- **Import with a documented schema**: the Import dialog shows the expected JSON format (copyable), so a valid board file can be written by hand — or generated by an AI; imported cards are **added** to the board by default, and substituting the whole board requires an extra are-you-sure confirmation
- **Undo & History**: every change (add, edit, move, sort, delete, import, restore) is logged with a timestamp; Undo steps back one state, and the History dialog can restore *any* logged state, git-style. Deleting a card only moves it to the **Trash** in the History dialog, where it stays recoverable until you choose **Delete permanently** — the only action that truly erases it
- **Four themes**: Morning (ruled paper), Day (plain white, high-contrast for easy reading), Dusk (warm sepia), Night (dark); follows your system preference until you pick one
- **Keyboard support**: fully usable without a mouse

## Run it

Needs **Node 23.4+** (for built-in `node:sqlite`) and **uv** (for the brain). Out of the
box every model runs on your machine and no API key is required — the only thing that
leaves it is web search, when the agent chooses to use it.

The board alone is enough to capture and organise cards. The brain adds the Assistant;
Chroma adds its memory of past chats; the lab is developer tooling. Start as little or
as much as you need — a missing piece degrades, it does not break the board.

### The ports

| Port | Service | Started by | You need it for |
| --- | --- | --- | --- |
| **3000** | board — Node, SQLite, static files, proxy | `npm start` | everything |
| **9000** | brain — agent, retrieval, voice | `uvicorn`, see below | the Assistant view |
| **3001** | test board — its own `board-3001.db` | `npm run test-board` | trying things without touching real cards |
| **9001** | test brain — writes only to :3001 | `npm run test-brain` | the Assistant on the test board |
| **9002** | RAG lab — retrieval workbench (test-only) | `npm run lab` | retrieval experiments |
| **8001** | Chroma — chat memory | separate stack, see below | "what did we say about X last month" |
| **11434** | Ollama — local models | `ollama serve` | local chat with no API key |

Boards pair with brains by last digit, and the test brain writes only to the test board.
`tests/ports.test.js` fails if that ever drifts.

### The runners

| Command | Starts | Notes |
| --- | --- | --- |
| `npm start` | board on :3000 | `PORT` and `BOARD_DB` override the port and database path |
| `set -a; . ./.env; set +a` | — | **run this before either brain.** `.env` is read by Docker Compose only, never by uvicorn |
| `uv run --project brain --extra local-embeddings uvicorn lodestar_brain.server:app --reload --port 9000` | brain on :9000 | the extra is ~1 GB of torch; `BRAIN_EMBEDDER=fake uv run --project brain uvicorn …` skips it and makes retrieval lexical |
| `npm run test-board` | board on :3001 | separate database, separate Chroma database |
| `npm run test-brain` | brain on :9001 | carries the extra already; still needs `.env` exported |
| `npm run lab` | the lab's suite, then :9002 | won't open the panel on a red suite. `-- --no-test`, `-- --all`, `-- --test-only` |
| `docker compose up --build` | board **and** brain | not Chroma, not Ollama — see below |

Open the board, then **Assistant** for chat and **Assistant → RAG lab** for the workbench.

### The databases

| Store | Holds | Starts itself? |
| --- | --- | --- |
| SQLite `board.db` | your cards — the record | **yes, always.** Created and migrated on boot; no daemon, no setup |
| Chroma on :8001 | chat memory | **no.** External, optional, degrades cleanly |
| the lab's index | experiment chunks and vectors | it isn't a database — process memory, discarded on exit |

Chroma is deliberately **not** in this repo's compose file: one store, so the composed
brain and a native one share the same memory. Start it yourself first —

```sh
cd ~/vectordb-lab && docker compose up -d      # Chroma on :8001
```

— because the brain probes it **once, at boot**. Start Chroma afterwards and the brain
never notices until you restart it. Without it the brain logs `chat memory disabled`,
drops the `recall_chat` tool, and serves everything else normally: board tools, web
search, card retrieval and the agent all work. Chroma auto-creates its database and
collections, so there is no schema step; the board on :3000 gets the `lodestar`
database and every other board gets `lodestar-test`, which is what keeps the test brain
out of your real chat history. `BRAIN_CHROMA_URL=memory` runs it in-process for one boot.

### Local models — the default, and what stays on the machine

Install [Ollama](https://ollama.com), then pull what the project names:

```sh
ollama pull 4skl/gemma4-e2b-mtp   # fast local chat and RAG answerer — BRAIN_MODEL default
ollama pull gemma4:e2b            # stronger local RAG judge
ollama pull deepseek-r1:8b        # optional, slower, better at reasoning
```

That is all. `BRAIN_LLM` already defaults to `ollama` and `BRAIN_OLLAMA_BASE_URL` to
`http://localhost:11434/v1` — the `/v1` is part of the setting, so the same variable
reaches llama.cpp or vLLM without a code change. With the defaults:

| Stage | Default backend | Where it runs |
| --- | --- | --- |
| chat and tool calls | `ollama` | your machine |
| relevance gate | follows `BRAIN_MODEL` | your machine |
| embeddings | `sentence-transformers`, `heydariAI/persian-embeddings` | your machine (~2.2 GB, fetched on the **first retrieval**, not at boot, so `/health` answers while it is still cold) |
| voice → text | `parakeet` (MLX) | your machine, Apple Silicon only (2.5 GB on first use) |
| chat memory | Chroma | your machine |
| web search | DuckDuckGo | **leaves the machine** — that is what a web search is |

So an unmodified checkout needs no API key. A key is needed only if you switch a stage
to a hosted model: `OPENROUTER_API_KEY` for `BRAIN_LLM=openrouter` or
`BRAIN_TRANSCRIBER=openrouter`, and `OPENAI_API_KEY` — deliberately a *different* key,
since the OpenRouter one buys chat completions only — for the lab's `openai` embedder.
It lives in the brain's environment and the browser never sees it, which is why the
board proxies rather than the page calling out.

**If you keep a `.env`, it wins.** These are code defaults; anything set in `.env` or
your shell overrides them, and it is worth reading yours before concluding a stage is
local. The full variable table is in [The brain](#the-brain-assistant-service).

If `BRAIN_LLM=ollama` and the daemon is down, every chat replies with a connection
error and the Assistant renders it as *"check that the brain service is running"* —
pointing at the one service that is fine. Check `ollama serve` first.

The RAG lab is local on the same terms: `RAGLAB_LLM=ollama` with the default embedder
and the default lexical reranker means a full experiment costs nothing and needs no key.
Fully offline mode, for tests and CI, is `BRAIN_LLM=fake BRAIN_EMBEDDER=fake
BRAIN_CHROMA_URL=memory`.

### With Docker

```sh
docker compose up --build
# then open http://localhost:3000
```

Two services, `lodestar` and `brain`. **Chroma and Ollama are not among them** — the
brain reaches both on your host through `host.docker.internal`, so start Chroma as
above and leave Ollama running, or accept a board with no chat memory and no local
chat. Two settings also differ from a native run, and both are forced by the image
rather than chosen: `BRAIN_TRANSCRIBER=openrouter`, because parakeet-mlx is
Apple-Silicon only, and the embedding weights are re-downloaded whenever the container
is recreated, because they are not on a volume yet.

Cards live on the named volume `board-data`, **not** in the container, so they survive
restarts, upgrades and `docker compose down`. The volume is local to the machine, so a
new laptop starts with an empty board, and `docker volume rm <project>_board-data` is
the only thing that erases it.

### Two smaller ways to run it

**Deploy online (free tier):** any Node host works — start command `node server.js`,
expose the platform's `$PORT`, and point `BOARD_DB` at a persistent disk (Render,
Railway, Fly with an attached volume) so the SQLite file survives redeploys.

**Front end only:** open `index.html` directly, or `python3 -m http.server`. Runs
entirely on localStorage — no Assistant, and no database shared across browsers.

## How your data is stored

The application follows a **local-first** persistence model. The browser keeps a working copy of the board in `localStorage`, so the interface loads instantly and stays fully usable offline. A lightweight Node.js service, bound to a configurable port, persists every change to an **embedded SQLite database** — a single self-contained file on the host (`board.db` by default, or wherever `BOARD_DB` points; a Docker volume when run with Compose). There is no external database server and no cloud dependency.

The database is the durable source of truth, with one deliberate guarantee: **a card is destroyed only by a two-step act — delete it from the board, then Delete permanently from the Trash in the History dialog.** Nothing else loses it:

- Deleting a card from the board **soft-deletes** it: the row stays in the database, hidden from the board but listed in the Trash, and can be restored.
- The server **never hard-deletes on a save**. If a save arrives missing some cards, those rows are archived to the Trash, not removed — so a partial or buggy write can never blank your board.
- Because deleted cards live in SQLite (not just the browser), they remain recoverable **even if you clear the browser's localStorage**.
- The data lives only in the SQLite file. Clearing browser storage doesn't touch it; the single way to wipe it is deleting that file (or the Docker volume) yourself.

Each machine keeps its own database — the board does not sync between laptops. To move a board deliberately, copy the SQLite file (or the volume), or use Export → Import.

## The brain (assistant service)

The **Assistant** view talks to a separate Python service — the *brain* — that runs one function-calling agent with four jobs: research a question (web search, cited urls), operate the board in plain language ("triage my inbox"), break fuzzy questions into concrete sub-questions, and surface connections between cards by **searching your board** — dense vectors and BM25 together, fused, reranked, and passed through a relevance gate that lets it answer *"I have nothing on that"* instead of inventing something. Each reply shows the tools the agent actually called, so its work is visible rather than magic, and the view has model pickers for text generation (live), plus media-to-text and embeddings (saved for the features they belong to).

```
browser ── :3000 Node (board + SQLite + static) ──proxy /api/agent/*──▶ :9000 brain (FastAPI)
                      ▲                                                     │
                      └────────── board reads/writes via /api/state ────────┘
```

The brain **never touches SQLite directly** — every board change goes through the Node API, so the Trash/purge durability guarantee above applies to agent edits too. The browser never sees the LLM key; it stays in the brain's environment.

**The assistant cannot put a card on your board by itself.** A card it invents is a **proposal**: it is saved immediately (a suggestion shouldn't be lost to a crash) but stays off the board until you accept it. Proposals appear in a *Proposed* section at the top of the Assistant view, with a count badge on the Assistant tab so you notice them from any view. **Approve** makes the card real — that is also the moment it earns its permanent `C-0NN` ledger number and triggers a database snapshot. **Reject** sends it to the Trash, where it stays recoverable, because "Delete permanently" remains the only thing that truly erases a card. Agent *edits* to cards you already own are not gated — those apply straight away and are covered by Undo and History.

Every capability sits behind a small interface chosen by env vars, so each piece can be swapped without touching the rest:

| Env var | Default | Meaning |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | *(empty)* | Optional remote-LLM access. The local defaults do not use it. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint works |
| `BRAIN_MODEL` | `4skl/gemma4-e2b-mtp` | Text-generation default for the local Ollama backend. |
| `BRAIN_LLM` | `ollama` | Text-generation route. The Assistant UI can explicitly switch to OpenRouter and GPT-5 Nano; use `fake` for deterministic tests. |
| `BRAIN_OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Where the local model server is. The `/v1` is part of the URL, so the same setting reaches llama.cpp or vLLM without a code change |
| `BRAIN_EMBEDDER` | `sentence-transformers` | The measured winner, and the largest single decision in the whole pipeline (~60× on recall — see the RAG section). Needs the `local-embeddings` extra and a ~2.2 GB download, paid on the first retrieval rather than at boot. Alternatives: `fastembed` (ONNX, the `semantic` extra) and `fake` (offline tests, lexical and never semantic). No fallback mode — a missing wheel is an error, not a silent downgrade, and `hash` is retired *by name* so an old config raises instead of quietly selecting its replacement |
| `BRAIN_EMBED_MODEL` | *(empty)* | Empty means that backend's own default (`heydariAI/persian-embeddings` for sentence-transformers). A name you set explicitly is never overridden, so the configuration and the model that actually embedded can't disagree |
| `BRAIN_GRADER` | `llm` | The relevance gate between retrieval and generation. `none` disables it. It follows `BRAIN_MODEL`, so it needs no model of its own, and it is one batched call per query rather than one per result |
| `BRAIN_GRADE_THRESHOLD` | `0.4` | Below this a retrieved card is dropped before the model sees it. A reply the gate cannot parse means *no opinion* (0.5), never *irrelevant* — otherwise one change of output format silently empties every answer's evidence |
| `BRAIN_MAX_STEPS` | `8` | Tool-call budget per chat turn |
| `BRAIN_TRANSCRIBER` | `parakeet` | Voice-to-text backend. `parakeet` is local MLX (Apple Silicon, nothing leaves the machine); `openrouter` is the hosted alternative; `fake` is deterministic for tests. No `auto` mode — an unknown value raises at boot rather than silently picking the one that sends your audio away |
| `BRAIN_OMNI_MODEL` | `google/gemini-2.5-flash-lite` | Audio → text for the *remote* transcriber only. Parakeet owns its own checkpoint and ignores this |
| `BRAIN_PARAKEET_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | Local checkpoint for the Parakeet backend (2.5 GB, fetched on first use) |
| `BOARD_API_URL` | `http://127.0.0.1:3000` | Where the brain finds the board API |
| `AGENT_URL` | `http://127.0.0.1:9000` | Where the Node proxy finds the brain |
| `BRAIN_CHROMA_URL` | `http://localhost:8001` | Chroma server holding chat memory. `memory` = in-process, nothing persisted (tests/CI); empty = chat memory off |
| `BRAIN_CHROMA_DATABASE` | paired with the board | `lodestar` for the board on `:3000`, `lodestar-test` for every other board |
| `BRAIN_CHAT_COLLECTION` | `chat-board-<port>` | One collection per board, so recall never leaks between boards |
| `LANGSMITH_TRACING` | *(never set)* | Deliberately not enabled and not defaulted anywhere in this repo. LangChain's tracing would upload whole conversations — marriage, health, money — to a third-party cloud. Set it yourself only if you accept that |
| `LODESTAR_AGENT_BURST` | `60` | How many assistant requests may land at once before the board starts refusing with `429` plus a `Retry-After`. Set on the **Node** server, not the brain |
| `LODESTAR_AGENT_PER_MIN` | `240` | How fast that budget is earned back. Separate from the burst because they answer different questions — how big a spike is tolerated, and what the sustained rate is |

The rate limit covers `/api/agent/*` **and** `/api/rag/*` — both are the brain, and an unbounded
recall loop costs embeddings just as an unbounded chat loop costs tokens. It is one bucket for
the whole assistant surface rather than one per client address: this is a single-user local
board, so keying a map on a value that never varies would be bookkeeping, not protection.
Requests are metered *before* the body is read (a flood should cost nothing to refuse), and
**the board API is never metered** — being over the assistant's limit must not make your own
cards unreachable. The developer-only `/api/raglab/*` proxy is deliberately unmetered too.

### Voice input

The mic beside Send dictates into the composer: the browser records, decodes to 16 kHz mono
WAV and posts it to the brain, and the transcript lands in the textarea as **editable text
that is never auto-sent** — a misheard word must be fixable before it reaches the agent.

Two backends sit behind the same seam, and you name the one you want — **there is no `auto`
mode**. It used to prefer local Parakeet when `mlx` was importable and fall back to OpenRouter
when it was not, which meant an audio file could leave the machine because a wheel failed to
install. Naming the backend outright makes that a boot error instead of a silent privacy
change, so an unknown value raises rather than choosing for you.

**Local Parakeet** (`nvidia/parakeet-tdt-0.6b-v3` via MLX) is the default — free, offline and
private, with no key and no audio leaving the machine:

```sh
uv sync --project brain --extra voice     # Apple Silicon only
```

The checkpoint is a 2.5 GB download on the first dictation (cached in
`~/.cache/huggingface` afterwards, and held in memory for the life of the brain process).

With the OpenRouter backend (`BRAIN_TRANSCRIBER=openrouter`) the default is
**`google/gemini-2.5-flash-lite`**. The audio rides in as an `input_audio` content part on an
ordinary chat completion, so any audio-capable chat model works without a second code path.
The brain detects providers that silently drop the audio part and reports the offending model
rather than filing its "I can't hear audio" apology as your words.

The Assistant's model picker chooses the *omni model*, not the backend: with the default
`BRAIN_TRANSCRIBER=parakeet` the brain dictates locally and ignores that pick entirely.

### Chat memory

The assistant remembers your conversations. Each exchange is chunked, embedded and stored
in **Chroma**, and the agent reaches it through a `recall_chat` tool — ask "what did we
decide about the mortgage?" and it searches past chat rather than guessing.

Chroma runs as a separate service, so it is a **prerequisite** for chat memory (everything
else — the board, the agent, web search, retrieval over your own cards — works without it; if Chroma is
unreachable the brain logs a warning, disables recall, and serves on). One record holds the
chunk and its vector together; real and non-real data are separated by database:

```
database "lodestar"        └── chat-board-3000     ← the real board
database "lodestar-test"   ├── chat-board-3001     ← the paired test board
                           └── chat-test-<uuid>    ← pytest, dropped after each run
```

Tests never touch the server: they run with `BRAIN_CHROMA_URL=memory`, an in-process client
that writes nothing to disk.

The commands to start it, and which stages run locally, are in [Run it](#run-it) —
kept in one place so the two cannot drift. Two details belong here rather than there:

Torch is deliberately kept out of `brain/.venv`, which is what lets the brain test suite
run fully offline with no extras. So `BRAIN_EMBEDDER=fake uv run --project brain
uvicorn …` is the lighter of the two launch lines unless you specifically need real
semantic retrieval — and plain `uv run --project brain uvicorn …` with neither the extra
nor the variable **fails at boot**, because `make_embeddings` raises on a missing wheel
rather than downgrading silently.

`--reload` is worth adding while working on the brain (`uvicorn … --reload --port 9000`).
The composed brain has no reload: a change to `brain/src` needs `docker compose restart
brain`, not a rebuild, because the source is bind-mounted and installed editable.

Swap points, each one file: the chat model (`brain/src/lodestar_brain/llm.py`'s `make_chat_model` — add a branch, never edit a call site), the search provider (`tools/websearch.py`), the embedder (`retrieval.py`'s `make_embeddings`, which returns a LangChain `Embeddings`, so a new backend is a class rather than a protocol of ours), the relevance gate (`retrieval.py`'s `gate_llm`), and the transcriber (`voice/`). The agent itself is deliberately *not* a swap point: a second agent is a second `LodestarAgent` (`agent.py`) constructed with its own tools and prompt at the call site. The one-entry builder registry that used to sit there was removed on 2026-08-01 — one name behind one constructor established no extension direction, so it was ceremony rather than a seam.

## Keyboard shortcuts (with a card focused)

| Key | Action |
| --- | --- |
| `Enter` | Edit the card |
| `[` / `]` | Move to previous / next column |
| `Alt` + `↑` / `↓` | Reorder within the column |
| `Delete` | Delete the card (with confirmation) |

## Tests

Lodestar is developed **test-first**: every feature or fix ships with tests in the same change, and the relevant suite passes before commit. There are four layers, and **all of them run fully offline** — the brain uses a deterministic fake LLM and the `fake` embedder — lexical hashing, never semantic, so no model is downloaded — and the frontend's semantic map is forced to its keyword fallback, so there is no API key, no network, and no flakiness.

| Layer | Where | What it covers |
| --- | --- | --- |
| Server unit | `tests/server.test.js`, `tests/backup.test.js` (`node:test`, zero deps) | Every API branch: soft-delete and restore, 400/404/405, the payload cap, the brain proxy's 503, static serving, legacy-schema migration |
| Brain unit | `brain/tests/` (pytest) | Agent loop, tool errors and step limits, the board tools' full-list contract, provider parsing, RAG |
| Brain evals | `brain/tests/evals/` | Agent *behaviour* against JSON scenario files, plus RAG retrieval-quality thresholds |
| Frontend e2e | `tests/e2e_test.py` (Playwright) | 343 checks — one per user-facing action — in headless Chrome |

The e2e suite **starts both services itself** on temporary ports and a temporary database, so nothing needs to be running first (requires [uv](https://docs.astral.sh/uv/) and Node 23.4+):

```sh
uv run --with playwright python tests/e2e_test.py   # full e2e
npm run test:server                                 # server + backup unit suites
npm run test:all                                    # server + brain unit + e2e
uv run --project brain pytest brain/tests -v        # brain unit
uv run --project brain pytest brain/tests/evals -v  # brain evals
```

**Backup guarantee:** every test entry point backs up `board.db` first — a timestamped copy under `backups/` (git-ignored, newest 100 kept) and, if [rclone](https://rclone.org/) is configured, a copy to `gdrive:lodestar-backups/`. Auth is one-time (`rclone config` and sign in to Google in the browser; rclone stores an OAuth token on the machine). **No Google password is ever stored in or read by this repo.** A missing or unconfigured rclone prints a warning and never blocks the tests.

**Backups also follow the data, not just the test runs.** When a `PUT /api/state` brings a card the database has never seen, the server takes a snapshot — one per save, however many new cards it carried. Edits, column moves and deletes do not trigger one, and neither does restoring a card from the Trash (its id is already known, so it is not a new entry). The snapshot runs in a detached child process after the response is sent, so a save is never blocked by a Drive upload, and it is taken after the commit so it contains the card that triggered it. `VACUUM INTO` is used rather than a file copy, so a snapshot taken while the server is mid-write is still a consistent database.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LODESTAR_BACKUP_ON_WRITE` | on | Set to `0` to turn off write-triggered backups. The test suites set this so they never add throwaway boards to your real history. |
| `LODESTAR_BACKUP_DIR` | `backups/` | Where snapshots are written. |
| `LODESTAR_BACKUP_KEEP` | `100` | How many snapshots to retain; older ones are deleted. |
| `LODESTAR_RCLONE_REMOTE` | `gdrive` | The rclone remote to push to. |
| `LODESTAR_RCLONE_BIN` | `rclone` | Path to the rclone binary. |

A CI workflow (`.github/workflows/ci.yml`) runs the brain unit tests and the full e2e suite on every push and pull request. Note that the repository currently has **no git remote**, so CI has not actually run yet — the suites are run locally before each commit.

### RAG test lab — measuring diary retrieval

Diary mode has to find one sentence out of a year of rambling, multi-topic Farsi
monologue. Which chunking and retrieval strategy does that best is an empirical
question, so it is answered empirically: the **RAG test lab** indexes a synthetic year
of diary chat, runs a 100-question ground-truth set through whatever pipeline you
configure, and grades the result.

It is a page inside the board — **Assistant → “RAG test lab”** — backed by a test-only
service on :9002 that the board proxies to. Start the service, open the board, and the
page finds it:

```sh
npm run lab         # the lab's suite, then the lab service on :9002 (test-only)
npm run test-board  # the board on :3001 → Assistant → RAG test lab
```

`npm run lab` will not open the panel on a red suite — the lab's whole claim is that
retrieval choices here were decided by measurement, and that is worth what the tests
behind it are worth. `-- --no-test` skips the suite, `-- --all` runs the whole brain
suite instead of the lab's, `-- --test-only` stops after it. `npm run raglab` still
starts the service on its own.

The page says how to start the service if it is not running, so a board with no lab
behind it is a normal state rather than a broken screen. The service also serves a
standalone panel at `http://localhost:9002/` if you would rather run it without a board.

Two fixtures are the whole basis of it: `brain/tests/fixtures/diary_year_fa.json`
(167 sessions, 998 messages, Aug 2025 → Jul 2026, with a mood and storyline tags per
session, plus a `habits` block declaring five tracked practices) and `diary_year_fa_groundtruth.json` (112 questions across
eleven types — single-hop, temporal, multi-hop, aggregation, knowledge-update,
commitment, entity, pattern, abstention, adversarial, habit — each with a reference
answer and verbatim evidence quotes). Both are synthetic; every person and event in
them is fictional.

A candidate sweep is measured on **49 of those questions, balanced across the
difficulty bands** (17 easy / 16 medium / 16 hard). The bands are naturally 29/57/26,
so sampling the set as it is hands medium about half of every run — and since the
four deciding metrics are means over questions, that measures one band and reports it
as the pipeline. Which questions a run actually scored is saved on the run itself,
because two rows are comparable only if they scored the same ones.

Pick a strategy per stage and the panel grades it:

| Stage | Choices |
| --- | --- |
| Chunking | fixed 500 (what the brain ships), fixed+overlap, per-message, turn-pair, whole-session, semantic-drift (topic segmentation) — each optionally with Anthropic-style contextual headers |
| Hierarchy | raw chunks plus, additively, session summaries, month digests, per-storyline digests, and a promise/deadline ledger; summaries extractive (offline) or LLM |
| Embedding | **Persian-tuned `heydariAI/persian-embeddings`** (sentence-transformers, loaded locally) is the lab default — the corpus is a Farsi diary, and this was the one choice worth ~60×. Alternatives: fastembed's ONNX list, any HuggingFace checkpoint, OpenAI embeddings (needs `OPENAI_API_KEY`), and the hash embedders that exist to be measured against |
| Retrieval | dense, BM25, or hybrid with Reciprocal Rank Fusion (the default); Farsi time expressions («آذر», «پارسال پاییز») resolved into a metadata date range; multi-query expansion; HyDE |
| Reranking | none, **lexical (the default)**, recency, "agentic" (relevance + recency + emotional importance), multilingual cross-encoder, or LLM grading |
| Gating | a relevance threshold — what makes an honest *"I have nothing on that"* possible — plus parent/session expansion and MMR diversification |
| Scoring | recall/precision/MRR/nDCG@k over evidence sessions, verbatim **quote recall**, latest-state recall for facts that changed, abstention accuracy, and **RAGAS** — its non-LLM context metrics offline, its judged metrics (faithfulness, relevancy, factual correctness) with an API key |

Everything is reported per question *type*, because a change that lifts single-hop
recall while destroying temporal recall is not an improvement.

`npm run raglab` starts the lab at `http://localhost:9002/`. Its defaults embed with
`heydariAI/persian-embeddings` and rerank lexically, both on this machine; only the
LLM stages (answerer, summaries, relevance gate, RAGAS judge) reach out, to
OpenRouter by default (`OPENROUTER_API_KEY`) or to a local model with
`RAGLAB_LLM=ollama`. The command installs what the local embedder needs (the
`local-embeddings` extra: sentence-transformers and torch, ~1 GB), and downloads the
Persian model once (~2.2 GB) on the first index build. `tests/ports.test.js` checks
that launcher against the configured default, so switching the default without
switching the extra fails a test rather than a run.

Four of the metrics that choose the architecture are LLM-judged, so a lab with no
model can rank nothing. `RAGLAB_LLM=ollama` points every LLM stage — answerer,
relevance gate, reranker **and the RAGAS judge** — at a model on this machine, which
is what makes the expensive candidates measurable without buying credit (a
per-chunk relevance gate is *k* calls per question). Two rules it keeps: a model the
daemon does not serve stops the run rather than silently becoming another one, and
the judge is **screened before it is allowed to grade** —
`npm run raglab:judgescreen -- --models qwen3.5:2b gemma4:e2b` scores it on claims
whose answers are already known, and the results are committed under `.screens/`
because they are the evidence for which model was permitted to decide. Two of the
local models screened so far answered identically to every claim, which scores 50%
on a balanced set and separates no candidate from any other. `RAGLAB_LLM=ollama` is
enough on its own — the model defaults follow the backend, since a slug only means
something to whatever serves it — and every phase reports where it is, per question
while answering and per judge call while grading, because a judged local run spends
hours inside one stage.

`npm run raglab:leaderboard` builds the leaderboard from `.runs/`, and its main job
is refusing to rank rows that are not comparable: a decision score is a mean over
questions judged by a model, so it groups by (question set, judge) and never ranks
across groups. A lead inside the combined error of the top two rows is reported as
a tie rather than a win, and runs that recorded only *how many* questions they
scored get no rank numbers at all — two runs of 24 questions may be two different
24.

The lab is strictly test-side, and its experiments are **ephemeral by
construction**: the index lives in process memory and is discarded when the lab
stops, so there is no database to start first and no state a later run can inherit
from an earlier one. The only thing it writes is **one JSON file per run** in a
git-ignored `.runs/` folder — the config, the metrics, and the per-question detail
needed to reopen the result. It used to keep its vectors in a Chroma database of its
own, guarded by a check that refused the production one; having no such setting is
the stronger version of that guard. No production module imports the lab — the board
knows nothing about it beyond a proxy prefix. Its own tests are part of the brain
suite (`npm run test:raglab`), and the page is covered by the e2e suite.

## What the RAG lab measured

The lab exists so retrieval choices are settled by measurement rather than taste. It
has now been used in anger — eight candidate architectures, one changed knob each —
and this is what came back. The short version is that **the sweep could not separate
the candidates, and the reason it could not is more useful than a winner would have
been.**

### The whole path a question travels

```
 ┌───────────────────────────────────────────────────────────────────┐
 │ browser — board UI, seven views, Assistant chat                   │
 └────────────────────────────────┬──────────────────────────────────┘
                                  │  the browser talks only to Node
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ Node  server.js  :3000       board API · static files · zero deps │
 │                                                                   │
 │   ┌──────────────────────────────────────────────────────────┐    │
 │   │ SQLite  board.db          built-in node:sqlite, no ORM   │    │
 │   │   one `cards` table · boot-time column migrations        │    │
 │   │   soft delete only — a card is destroyed only by         │    │
 │   │   Trash → "Delete permanently"                           │    │
 │   └──────────────────────────────────────────────────────────┘    │
 │                                                                   │
 │   proxy  /api/agent/*   /api/rag/*   /api/raglab/*                │
 └────────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ brain  :9000 — the chatbot            FastAPI + LangChain agent   │
 │   tools:  board CRUD · web_search · find_related · recall_chat    │
 │   every write goes back out through Node's API, never SQLite      │
 │   a card it invents is a *proposal* until you accept it           │
 └────────────────────────────────┬──────────────────────────────────┘
                                  │  recall_chat — diary memory
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ INDEX                                         once per corpus     │
 │   semantic-drift chunker · 500 chars / 100 overlap                │
 │   contextual headers                                              │
 │   embedder · 1024-d          ◄── the one choice that mattered     │
 │                                                                   │
 │   6 additive layers ── 732 chunks                                 │
 │     chunk  515 │ session 167 │ month   12  ···· never retrieved   │
 │     thread  32 │ commit    1 │ habit    5  ···· 1 question in 24  │
 └────────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ RETRIEVE   multi-query expansion                                  │
 │            hybrid BM25 + dense ─► RRF (k0 = 60)                   │
 │            40 candidates across all 6 layers                      │
 │            Farsi time expressions → metadata date ranges          │
 └────────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ RANK       lexical rerank, depth 20                               │
 │            rollup boost 1.0        (candidate G tried 1.4: worse) │
 └────────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ CUT        top k = 8               (k=5 and k=12 measured: tie)   │
 └────────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ╔═══════════════════════════════════════════════════════════════════╗
 ║ GATE       an LLM scores each context, drop below 0.40            ║
 ║            8 ─► 6.47 contexts · the run costs LESS                ║
 ║            ◄── candidate F's only change, and the chosen one      ║
 ╚════════════════════════════════╤══════════════════════════════════╝
                                  ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ GENERATE   Farsi answer with [session-id] citations, or refuse    │
 │                                                                   │
 │      context precision  0.9338  ◄─┐                               │
 │      answer relevancy   0.4886  ◄─┴─ the gap, and the bottleneck  │
 └───────────────────────────────────────────────────────────────────┘

     Chunks and vectors live in the lab process — one in-memory index
     per configuration, named by its fingerprint, discarded on exit.

     Nothing in the eight-candidate sweep ever changed the bottom box.
```

### Which approach won

Nothing, on score. Every comparison that carries an error bar came back a **tie**:
F against A is 0.7375 ± 0.0333 versus 0.7222 ± 0.0341 — a 0.0153 lead inside a
combined error of 0.0477. The earlier hosted sweep put five candidates inside 0.0116
with no error bar at all, which is a ranking rather than a result.

There is still a defensible answer, ordered by how well established it is:

| Choice | Verdict | Evidence |
| --- | --- | --- |
| **The embedder** | **the only decision that was ever large** | hash → a real Persian encoder moved recall ~0.01 → 0.617, roughly **60×**. Every knob in the sweep is worth under 2%. |
| **F — the relevance gate** | **keep, on cost rather than quality** | ties A on all four deciding metrics, cuts context 7.90 → 6.47 chunks, and the run costs *less* — 6814 s against 7296 s, because a shorter context means fewer judge calls. Refused 1 of 30 questions against A's 2. |
| k = 5 / k = 12 | settled — stop tuning it | opposite changes to one knob finishing 0.0014 apart, precision and recall simply trading places |
| Dropping the rollup layers | **genuinely open** | the simplest configuration had the best deterministic headline; the `month` layer was retrieved **zero times** in every candidate, and the habit ledger once in 24 |
| G — boost the rollups ×1.4 | **no** | worse in every difficulty band, and it promoted month digests rather than the habit ledger it was meant to rescue |
| H — whole-session chunks | **no** | the weakest retrieval in the sweep, hit rate 0.7727 against 0.8636 |

### What the measurements taught

- **The decision rule picks the winner before any data arrives.** Rank on the four
  judged metrics and one candidate wins; rank on the deterministic composite and the
  candidate that deletes the entire summary hierarchy wins instead. Same runs, same
  answers, opposite architectural conclusions. Fixing the rule *before* seeing the
  rows is the whole reason the result is trustworthy.
- **A score without an error is not a result.** The hosted sweep produced a confident
  ranking from five numbers with no spread. As soon as error bars existed, the first
  comparison they touched returned *tie* rather than a winner — same shape of data,
  opposite conclusion, purely from recording the uncertainty. This is why
  `npm run raglab:leaderboard` refuses to rank rows that cannot be compared.
- **Retrieval is close to saturated here; the answerer is the constraint.** Context
  precision 0.9338 against answer relevancy 0.4886. Across the sweep, retrieval recall
  varies by 0.1098 while the score meant to measure the whole pipeline varies by
  0.0116 — retrieval differences arrive at the answer **9.5× attenuated**. All eight
  candidates varied retrieval; none varied generation.
- **The largest lever was pulled before the sweep began.** The embedder was ~60×;
  everything swept afterwards was worth under 2%. You can only learn that in
  retrospect, which is the argument for measuring early and cheaply rather than
  tuning carefully.
- **A metric can punish the right answer.** On an adversarial question with a false
  premise — *"when we bought the apartment, what was the payment?"*, where no apartment
  was ever bought — one candidate correctly replied that the purchase never happened
  and scored 0.0, because `abstained_correctly` checks whether the model emitted a
  refusal, not whether it was right.
- **Some question types are not retrieval problems.** The `pattern` question failed
  identically in every candidate, and the habit questions scored perfect recall even
  in the configuration that indexes no habit ledger. Counting, streak and date-range
  questions are lookups against structured card fields; routing them is the change
  most likely to move a number next.

Full write-ups, with run ids, real Farsi model outputs and every metric table:

- `docs/report/rag-sweep-essence.html` — the brief: abstract, findings, what to do next.
- `docs/report/rag-candidates-abcd.html` — the evidence, candidate by candidate.
- `docs/rag-architecture.md` and `docs/rag-chosen-architecture.md` — the measured
  argument and the recorded decision.

## Known limitations and next steps

Written from the measurements rather than around them. Each entry says what is known, how
well it is known, and what would move it.

**The answerer is the bottleneck, and nothing built so far touches it.** Context precision is
**0.9338** while answer relevancy is **0.4886** — retrieval hands over almost entirely
relevant context and the generation step fails to use it. Across the eight-candidate sweep,
retrieval recall varies by 0.1098 while the composite meant to score the whole pipeline varies
by 0.0116: retrieval differences arrive at the answer roughly **9.5× attenuated**. All eight
candidates varied retrieval and **not one varied generation**. So the next candidate worth
building is a *different answerer* — and note that answer relevancy is partly a formatting
artifact, because a bulleted `date: fact [session-id]` reply reverse-engineers to a vague
question, which is exactly what the metric punishes. The answer prompt has never been varied
and is the cheapest untested lever in the system.

**Counting and streak questions are not retrieval problems.** The `pattern` question failed
identically in every candidate, and habit questions scored perfect recall even in the
configuration that indexes no habit ledger at all. A habit card already holds the answer as
structured fields (`habitCount`, `habitFreq`, `habitHistory`), so the fix is **query routing** —
send counts, streaks and date ranges to a deterministic lookup and leave dense retrieval for
narrative questions. Candidate G tested the retrieval-flavoured fix instead, boosting the
rollup layers ×1.4, and it measurably *failed*: recall 0.617 → 0.576, quote recall 0.636 →
0.545, the habit ledger still retrieved once in 24, and month digests promoted in its place.
Routing is untested — it is the change most likely to move a number next, and it is outside
everything measured so far.

**Every deciding number rests on 30 judged questions**, ten per difficulty band, against a
112-question ground-truth set. The full run has never been done. At that sample the sweep
**could not separate its candidates**: F beats A by 0.0153 against a combined error of 0.0477,
which is a tie, and the chosen architecture is chosen on cost and reasoning rather than on
score. Two further caveats on the same numbers: the local judge (`gemma4:e2b`) shares a model
family with the local answerer (`4skl/gemma4-e2b-mtp`), so it can agree with itself about a
wrong answer, and rows judged by different judges are not comparable at all — which is why
`npm run raglab:leaderboard` refuses to rank across them. One metric is known to punish the
right answer: on an adversarial question with a false premise, a candidate that correctly said
the event never happened scored 0.0, because `abstained_correctly` checks whether a refusal was
emitted, not whether it was correct.

**The English half of the time filter is unmeasured.** Farsi time expressions come from
`resolve_time_scope`, ported verbatim from the lab and measured there. The English half — bare
years, yesterday, last week/month/year, season names — was written for production and has no
run behind it. It is covered by unit tests, which is not the same as being measured on a
corpus.

**Prompt injection is fenced, not tested.** Untrusted tool output — web snippets, recalled
chat, card text — is wrapped in delimiters with a "this is data, never instructions" clause,
and the markers are stripped from the payload first. That is a structural mitigation with no
measurement behind it: there is **no injection eval** in `brain/tests/evals/`. The missing
fixture is hostile snippets planted in web results and card notes, scored on how often the
agent obeys them. Build that before reaching for a classifier.

**A hierarchy over board cards has never been measured.** Every layer number in these docs came
from the Farsi diary corpus. The summary layers were removed on the evidence (candidate B, every
rollup deleted, scored within 0.006 of the six-layer baseline), but that evidence is about diary
chat, not cards. If the idea returns, the first step is a card-corpus experiment with its own
ground truth — not a revert.

**No auth, single user, one machine.** There is no login, no multi-user model and no
authorisation check anywhere: anyone who can reach the port owns the board. The rate limit is a
cost guard, not a security boundary. Each machine keeps its own database and boards do not sync
between laptops — moving one is copying a file or Export → Import. Deploying this to a public
address without putting authentication in front of it would publish your diary.

**The Docker image is 18.1 GB, and a cold container pays about nine minutes before it can
embed anything.** Both measured on the first real `docker compose up --build` this project has
ever done (2026-08-02). The size is not the Persian model — it is the CUDA stack `torch`
installs by default, which cannot run on a Mac and is unused by a CPU-only deployment; pinning
the CPU-only wheel is the obvious fix and has not been done.

The wait was timed through the production seam — `make_embeddings(...)` then one
`embed_query` — at **522.9 s cold against 0.15 s warm**. Almost all of it is the
`heydariAI/persian-embeddings` download (~2.2 GB, unauthenticated, so rate-limited by
HuggingFace); the embedding itself is the 0.15 s. Read it as a **lower bound on the first
real retrieval** rather than a measurement of one: no `find_related` or `recall_chat` call was
timed end to end, but every retrieval path blocks on exactly this, because `retrieval.py`'s
lazy `model` property is what defers it. One sample, on one network.

That deferral is the design working — `/health` answers throughout and readiness never blocks
— but two consequences are not written down anywhere else. A fresh container's first
retrieval-touching question sits for minutes with **no progress indication**, and the weights
cache inside the container rather than on a volume, so the cost returns on **every container
recreate**, not once ever. Mounting `~/.cache/huggingface`, pre-warming on boot, or simply
telling the user what the wait is — all unbuilt.

**CI is written but has never run.** `.github/workflows/ci.yml` runs the brain units and the
full e2e suite on every push — and the repository has no git remote, so it has never executed
once. The suites are run locally before each commit instead.

## More

- `docs/details.md` — the full architecture deep dive: every module, the data flows, the invariants, and the design trade-offs.
- `docs/plan.md` — the requirements ledger: every Sprint 2 task requirement and optional task, what
  is shipped, what is planned, and where each one is implemented. Includes a known-limitations
  section.
- `docs/` — a design spec per major feature (what was built, why, and what was rejected), plus the
  one implementation plan currently being executed. Start at `docs/README.md`, which carries a
  status per document.
