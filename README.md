# Lodestar

*Your compass for life!* Lodestar is a place to get every open question out of your head, see what actually matters, and never lose a thought — so you can follow through, privately and at work. A kanban-style board that is one dashboard for your whole life — work questions, plans with your partner, sports, music, reading, holidays — everything in one place. Vanilla HTML/CSS/JavaScript front end, with an optional tiny server that persists the board to a local **SQLite** database. No build step and no npm dependencies — the server uses Node's built-in `node:sqlite` and `http`.

The design is a "question ledger": quad-ruled engineering paper, cards as ruled index cards with permanent ledger IDs (`Q-001`, `Q-002`, …), card types as ink stamps, and life-area categories as coloured index tabs — every category gets its own ink, in all four themes.

## Features

- **Three-column lifecycle**: Inbox → In Progress → Answered
- **Seven views**: **Board** (the columns), **Backlog** (the Inbox as a scannable ledger list), **Overview** (a semantic map — see below), **Matrix** (four decision lenses on one grid), **Areas** (which life area is starved), **Review** (the weekly sweep), and **Assistant** (chat with the brain — see below)
- **Drag & drop** cards within and between columns, with drop-position indicator
- **Card types**: question, problem, task, idea, plan — stamped on each card in neutral ink, with a per-column sort menu (by deadline, priority, type, newest or oldest)
- **Categories**: nine life areas to start (Work, Love, Family, Health, Mind, Music, Travel, Home, Money), each with its own colour — and the set is **yours**: add or remove areas from the ✎ tab on the rail, pick a hue for each, up to 24. Cards carry a coloured spine, and the rail of coloured tabs under the header filters the board to one life area
- **Importance & urgency**: set each to High or Low on a card to place it on the **Matrix**
- **Deadlines and automatic priority**: give a card an ISO date and it carries a deadline chip that turns red once overdue. Priority is **derived**, never stored — P1 urgent & important, P2 urgent, P3 important, P4 neither — so it can never disagree with the two judgements behind it; the toolbar filters by it
- **Matrix — four lenses on the same cards**: importance is always the vertical axis, and the picker swaps what it is crossed with — **Eisenhower** (urgency; Answer now / Schedule / Delegate / Drop, urgent on the left), **Leverage** (effort — where a little work moves a lot), **Serenity** (control — what deserves action, and what you are allowed to put down), and **Follow-through** (time since a card was last touched)
- **Overview map**: every question is plotted by meaning — its text is embedded and reduced to two axes, so questions that read alike sit close together. Toggle the projection between **PCA** (fast, stable global axes) and **t-SNE** (local neighbourhoods, so clusters of related thoughts pull together); t-SNE is seeded, so the same cards always land in the same spots. Embeddings come from a HuggingFace model ([Transformers.js](https://huggingface.co/docs/transformers.js), `Xenova/all-MiniLM-L6-v2`) loaded on demand from a CDN — **no login or API key needed**; the ~30 MB model downloads once and is cached by the browser. When it's still loading or the browser is offline, the map falls back to a keyword-overlap layout, so it always renders with no network. Hover a dot for details, click to open the full editor, and the tag/type/category/search filters apply just like the board. Dots are inked in their category's colour, so the map reads by life area.
- **Areas view**: one small-multiples tile per life area plus an attention wheel whose spoke length is open-question mass, answering "which part of my life is starved?" at a glance. Click a tile to focus that area and open a category-aware detail panel — cooling-off (the 30-day rule), learning progress, serenity check, and what has been longest untouched
- **Review view**: GTD's weekly review as a screen — stat tiles (inbox, answered this week, new this week, open in total), week-over-week drift per area, the neglect list (important cards untouched for over a month), and three old thoughts resurfaced on purpose. The picks are seeded on the date and pinned for the day, so acting on one never reshuffles the others
- **Quick capture**: write anything into the Inbox and press Enter
- **Edit modal**: notes, type, category, tags, importance and urgency, effort, control, and a deadline per card. Tag a card `decision` and notes lines starting with `+` or `−` read back as a two-column pro/con balance sheet
- **Search & filters**: free-text search, type filter, priority filter, category tabs, tag chips
- **Persistence**: runs on localStorage on its own; when the server is running it also saves to a SQLite database, so questions survive restarts and reopen on any browser. See **How your data is stored** below for the exact durability guarantee. Export/Import as JSON for backups
- **Database sync rule**: a browser that already has its own board keeps it (and pushes it to the server), so unsynced local edits are never clobbered; a fresh browser loads the board from the database
- **One Menu button**: Undo, History, Export and Import fold into a single expanding menu in the toolbar
- **Export dialog**: download `lodestar.json`, or copy the JSON when the browser blocks downloads
- **Import with a documented schema**: the Import dialog shows the expected JSON format (copyable), so a valid board file can be written by hand — or generated by an AI; imported questions are **added** to the board by default, and substituting the whole board requires an extra are-you-sure confirmation
- **Undo & History**: every change (add, edit, move, sort, delete, import, restore) is logged with a timestamp; Undo steps back one state, and the History dialog can restore *any* logged state, git-style. Deleting a question only moves it to the **Trash** in the History dialog, where it stays recoverable until you choose **Delete permanently** — the only action that truly erases it
- **Four themes**: Morning (ruled paper), Day (plain white, high-contrast for easy reading), Dusk (warm sepia), Night (dark); follows your system preference until you pick one
- **Keyboard support**: fully usable without a mouse

## Run it

**With the database (recommended)** — needs Node 23.4+ (for built-in `node:sqlite`):

```sh
npm start
# then open http://localhost:3000
```

The board is stored in `board.db` next to `server.js`. Change the port or database path with `PORT` and `BOARD_DB`:

```sh
PORT=4000 BOARD_DB=/path/to/board.db npm start
```

**With Docker (recommended for a new laptop)** — no Node install needed, just Docker:

```sh
docker compose up
# then open http://localhost:3000
```

The database is stored on a named Docker volume (`board-data`), **not** inside the container, so your questions survive restarts, upgrades, and `docker compose down`. Because the volume is local to each machine, a new laptop starts with a fresh, empty board. Removing the volume (`docker volume rm <project>_board-data`) is the only thing that erases the data.

**Deploy it online (free cloud):** any host that runs Node works — set the start command to `node server.js`, expose the platform's `$PORT`, and point `BOARD_DB` at a persistent disk (e.g. Render/Railway/Fly with an attached volume) so the SQLite file survives redeploys.

**Front end only (no persistence server):** open `index.html` directly, or `python3 -m http.server`. It runs entirely on localStorage; it just won't share a database across browsers.

## How your data is stored

The application follows a **local-first** persistence model. The browser keeps a working copy of the board in `localStorage`, so the interface loads instantly and stays fully usable offline. A lightweight Node.js service, bound to a configurable port, persists every change to an **embedded SQLite database** — a single self-contained file on the host (`board.db` by default, or wherever `BOARD_DB` points; a Docker volume when run with Compose). There is no external database server and no cloud dependency.

The database is the durable source of truth, with one deliberate guarantee: **a question is destroyed only by a two-step act — delete it from the board, then Delete permanently from the Trash in the History dialog.** Nothing else loses it:

- Deleting a question from the board **soft-deletes** it: the row stays in the database, hidden from the board but listed in the Trash, and can be restored.
- The server **never hard-deletes on a save**. If a save arrives missing some questions, those rows are archived to the Trash, not removed — so a partial or buggy write can never blank your board.
- Because deleted questions live in SQLite (not just the browser), they remain recoverable **even if you clear the browser's localStorage**.
- The data lives only in the SQLite file. Clearing browser storage doesn't touch it; the single way to wipe it is deleting that file (or the Docker volume) yourself.

Each machine keeps its own database — the board does not sync between laptops. To move a board deliberately, copy the SQLite file (or the volume), or use Export → Import.

## The brain (assistant service)

The **Assistant** view talks to a separate Python service — the *brain* — that runs one function-calling agent with four jobs: research a question (web search, cited urls), operate the board in plain language ("triage my inbox"), break fuzzy questions into concrete sub-questions, and surface connections between questions using **Leiden community detection** over a similarity graph of your board. Each reply shows the tools the agent actually called, so its work is visible rather than magic, and the view has model pickers for text generation (live), plus media-to-text and embeddings (saved for the features they belong to).

```
browser ── :3000 Node (board + SQLite + static) ──proxy /api/agent/*──▶ :9000 brain (FastAPI)
                      ▲                                                     │
                      └────────── board reads/writes via /api/state ────────┘
```

The brain **never touches SQLite directly** — every board change goes through the Node API, so the Trash/purge durability guarantee above applies to agent edits too. The browser never sees the LLM key; it stays in the brain's environment.

**The assistant cannot put a card on your board by itself.** A card it invents is a **proposal**: it is saved immediately (a suggestion shouldn't be lost to a crash) but stays off the board until you accept it. Proposals appear in a *Proposed* section at the top of the Assistant view, with a count badge on the Assistant tab so you notice them from any view. **Approve** makes the card real — that is also the moment it earns its permanent `Q-0NN` ledger number and triggers a database snapshot. **Reject** sends it to the Trash, where it stays recoverable, because "Delete permanently" remains the only thing that truly erases a card. Agent *edits* to cards you already own are not gated — those apply straight away and are covered by Undo and History.

Every capability sits behind a small interface chosen by env vars, so each piece can be swapped without touching the rest:

| Env var | Default | Meaning |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | *(empty)* | LLM access. Without it the assistant errors politely; the board is unaffected |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint works (e.g. a local Ollama later) |
| `BRAIN_MODEL` | `openai/gpt-5-nano` | Fallback when the browser sends no pick; any OpenRouter model id |
| `BRAIN_LLM` | `openrouter` | `fake` = deterministic offline provider (tests/CI) |
| `BRAIN_EMBEDDER` | `hash` | `fastembed` (semantic, needs the `semantic` extra) or `hash` (offline token buckets). No fallback mode — a missing wheel is an error, not a silent downgrade |
| `BRAIN_MAX_STEPS` | `8` | Tool-call budget per chat turn |
| `BRAIN_TRANSCRIBER` | `parakeet` | Voice-to-text backend. `parakeet` = local MLX model (free, offline, Apple Silicon only); `openrouter` = the omni model; `fake` = deterministic offline transcript (tests/CI). Compose pins `openrouter`, since the brain image cannot install mlx |
| `BRAIN_OMNI_MODEL` | `google/gemini-2.5-flash-lite` | Audio → text model for the OpenRouter backend; the Assistant view's omni picker overrides it per request |
| `BRAIN_PARAKEET_MODEL` | `mlx-community/parakeet-tdt-0.6b-v3` | Local checkpoint for the Parakeet backend (2.5 GB, fetched on first use) |
| `BOARD_API_URL` | `http://127.0.0.1:3000` | Where the brain finds the board API |
| `AGENT_URL` | `http://127.0.0.1:9000` | Where the Node proxy finds the brain |
| `BRAIN_CHROMA_URL` | `http://localhost:8001` | Chroma server holding chat memory. `memory` = in-process, nothing persisted (tests/CI); empty = chat memory off |
| `BRAIN_CHROMA_DATABASE` | paired with the board | `lodestar` for the board on `:3000`, `lodestar-test` for every other board |
| `BRAIN_CHAT_COLLECTION` | `chat-board-<port>` | One collection per board, so recall never leaks between boards |

### Voice input

The mic beside Send dictates into the composer: the browser records, decodes to 16 kHz mono
WAV and posts it to the brain, and the transcript lands in the textarea as **editable text
that is never auto-sent** — a misheard word must be fixable before it reaches the agent.

Two backends sit behind the same seam. **Local Parakeet** (`nvidia/parakeet-tdt-0.6b-v3` via
MLX) is free, offline and private — no key and no audio leave the machine — and is what
`auto` picks whenever it is installed:

```sh
uv sync --project brain --extra voice     # Apple Silicon only
```

The checkpoint is a 2.5 GB download on the first dictation (cached in
`~/.cache/huggingface` afterwards, and held in memory for the life of the brain process).

Everywhere else (Linux, Docker) `auto` falls back to **OpenRouter**, sending the audio as an
`input_audio` part to `BRAIN_OMNI_MODEL`. Pick that model with care: several models advertise
audio input but the provider serving them silently discards it and answers an invented
apology instead of a transcript. The brain detects that shape and reports which model is at
fault rather than filing its apology as your words. Verified working: `google/gemini-2.5-flash-lite`
(the default), `openai/gpt-audio-mini`, `mistralai/voxtral-small-24b-2507`.

### Chat memory

The assistant remembers your conversations. Each exchange is chunked, embedded and stored
in **Chroma**, and the agent reaches it through a `recall_chat` tool — ask "what did we
decide about the mortgage?" and it searches past chat rather than guessing.

Chroma runs as a separate service, so it is a **prerequisite** for chat memory (everything
else — the board, the agent, web search, Leiden RAG — works without it; if Chroma is
unreachable the brain logs a warning, disables recall, and serves on). One record holds the
chunk and its vector together; real and non-real data are separated by database:

```
database "lodestar"        └── chat-board-3000     ← the real board
database "lodestar-test"   ├── chat-board-3001     ← the paired test board
                           └── chat-test-<uuid>    ← pytest, dropped after each run
```

Tests never touch the server: they run with `BRAIN_CHROMA_URL=memory`, an in-process client
that writes nothing to disk.

Run it locally (requires uv; deps install on first run):

```sh
export OPENROUTER_API_KEY=sk-or-...
uv run --project brain uvicorn lodestar_brain.server:app --reload --port 9000
# in another terminal: npm start, then open http://localhost:3000 → Assistant
```

With Compose, both services start together (`docker compose up`); put `OPENROUTER_API_KEY` in your environment or a `.env` file first. Fully offline mode — used by the tests — is `BRAIN_LLM=fake BRAIN_EMBEDDER=hash`.

Swap points, each one file: the LLM provider (`brain/src/lodestar_brain/llm/`), the search provider (`tools/websearch.py`), the embedder (`rag/embedder.py`), and the agent loop itself (`agent/loop.py`).

## Keyboard shortcuts (with a card focused)

| Key | Action |
| --- | --- |
| `Enter` | Edit the question |
| `[` / `]` | Move to previous / next column |
| `Alt` + `↑` / `↓` | Reorder within the column |
| `Delete` | Delete the question (with confirmation) |

## Tests

Lodestar is developed **test-first**: every feature or fix ships with tests in the same change, and the relevant suite passes before commit. There are four layers, and **all of them run fully offline** — the brain uses a deterministic fake LLM and a hash embedder, and the frontend's semantic map is forced to its keyword fallback, so there is no API key, no network, and no flakiness.

| Layer | Where | What it covers |
| --- | --- | --- |
| Server unit | `tests/server.test.js`, `tests/backup.test.js` (`node:test`, zero deps) | Every API branch: soft-delete and restore, 400/404/405, the payload cap, the brain proxy's 503, static serving, legacy-schema migration |
| Brain unit | `brain/tests/` (pytest) | Agent loop, tool errors and step limits, the board tools' full-list contract, provider parsing, RAG |
| Brain evals | `brain/tests/evals/` | Agent *behaviour* against JSON scenario files, plus RAG retrieval-quality thresholds |
| Frontend e2e | `tests/e2e_test.py` (Playwright) | 160 checks — one per user-facing action — in headless Chrome |

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
npm run raglab      # the lab service on :9002 (test-only)
npm run test-board  # the board on :3001 → Assistant → RAG test lab
```

The page says how to start the service if it is not running, so a board with no lab
behind it is a normal state rather than a broken screen. The service also serves a
standalone panel at `http://localhost:9002/` if you would rather run it without a board.

Two fixtures are the whole basis of it: `brain/tests/fixtures/diary_year_fa.json`
(157 sessions, 954 messages, Aug 2025 → Jul 2026, with a mood and storyline tags per
session) and `diary_year_fa_groundtruth.json` (100 questions across ten types —
single-hop, temporal, multi-hop, aggregation, knowledge-update, commitment, entity,
pattern, abstention, adversarial — each with a reference answer and verbatim evidence
quotes). Both are synthetic; every person and event in them is fictional.

Pick a strategy per stage and the panel grades it:

| Stage | Choices |
| --- | --- |
| Chunking | fixed 500 (what the brain ships), fixed+overlap, per-message, turn-pair, whole-session, semantic-drift (topic segmentation) — each optionally with Anthropic-style contextual headers |
| Hierarchy | raw chunks plus, additively, session summaries, month digests, per-storyline digests, and a promise/deadline ledger; summaries extractive (offline) or LLM |
| Embedding | `ascii-hash` (the brain's current default), `token-hash`, `char-hash` (character n-grams), or a multilingual transformer via fastembed |
| Retrieval | dense, BM25, or hybrid with Reciprocal Rank Fusion; Farsi time expressions («آذر», «پارسال پاییز») resolved into a Chroma date range; multi-query expansion; HyDE |
| Reranking | none, lexical, recency, "agentic" (relevance + recency + emotional importance), multilingual cross-encoder, or LLM grading |
| Gating | a relevance threshold — what makes an honest *"I have nothing on that"* possible — plus parent/session expansion and MMR diversification |
| Scoring | recall/precision/MRR/nDCG@k over evidence sessions, verbatim **quote recall**, latest-state recall for facts that changed, abstention accuracy, and **RAGAS** — its non-LLM context metrics offline, its judged metrics (faithfulness, relevancy, factual correctness) with an API key |

Everything is reported per question *type*, because a change that lifts single-hop
recall while destroying temporal recall is not an improvement.

The lab is strictly test-side: it writes only to its own Chroma database
(`lodestar-raglab`, and it refuses to start against the production one) and to a
git-ignored `.runs/` folder, and no production module imports it — the board knows
nothing about it beyond a proxy prefix. Its own tests are part of the brain suite
(`npm run test:raglab`), and the page is covered by the e2e suite.

## More

- `details.md` — the full architecture deep dive: every module, the data flows, the invariants, and the design trade-offs.
- `plan.md` — the original design/implementation plan.
- `docs/superpowers/` — a design spec and an implementation plan per major feature.
