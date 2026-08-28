# Lodestar

*Your compass for life!* Lodestar is a kanban-style board that is one dashboard for your whole life — work questions, plans with your partner, sports, music, reading, holidays — everything in one place, so nothing open lives in your head. Local-first and private: vanilla HTML/CSS/JavaScript front end, a tiny zero-dependency Node server persisting to SQLite, and an optional Python "brain" that adds an AI assistant. Out of the box every model runs on your machine and no API key is required.

The design is a "card ledger": quad-ruled engineering paper, cards as ruled index cards with permanent ledger IDs (`C-001`, `C-002`, …), card types as ink stamps, and life-area categories as coloured index tabs.

## Features

- **Three-column lifecycle**: Inbox → In Progress → Done, with drag & drop and full keyboard support
- **Seven views**: Board, Backlog (the Inbox as a scannable list), Overview (a semantic map — cards plotted by meaning, embedded in-browser), Matrix (importance crossed with urgency, effort, control, or staleness), Areas (which part of your life is starved), Review (the GTD weekly sweep), and Assistant (chat with the brain)
- **Card types**: question, problem, task, idea, dream, habit — habits repeat rather than finish, wear a punch strip of per-period tick boxes, and remind you when due
- **Categories**: ten life areas to start, each with its own colour; add, remove, and recolour up to 24. Every board keeps its own set
- **Deadlines and derived priority**: importance × urgency gives P1–P4, never stored, so it can't disagree with the judgements behind it
- **Plan dates**: any card can say when you mean to get to it — a year, a year and month, or a full day. It follows the deadline until you set it by hand, and a rail beside the board groups the planned cards by how near they are
- **Quick capture, search, filters, tags**, and a pro/con balance sheet on any card tagged `decision`
- **Undo & History**: every change is logged and any logged state can be restored, git-style
- **Four themes**, following your system preference until you pick one

## Run it

Needs **Node 23.4+** (for built-in `node:sqlite`) and, for the Assistant, **uv**. The board alone is enough to capture and organise cards; each further service adds a capability and degrades cleanly when absent.

```sh
git clone https://github.com/S-Moha-M-Kashani/lodestar.git
cd lodestar
npm start                                # the board on http://localhost:3000
```

That is the whole setup — there is nothing to install and nothing to build. The server has zero dependencies and the frontend is native ES modules, so there is no `npm install` step and no bundler.

For the **Assistant**, start the brain (port 9000) in a second terminal:

```sh
set -a; . ./.env; set +a                 # if you keep a .env — uvicorn never reads it itself
uv run --project brain --extra local-embeddings uvicorn lodestar_brain.server:app --port 9000
```

The `local-embeddings` extra is ~1 GB of torch for real semantic retrieval; `BRAIN_EMBEDDER=fake uv run --project brain uvicorn …` skips it and makes retrieval lexical. The default chat backend is a local model via [Ollama](https://ollama.com):

```sh
ollama pull 4skl/gemma4-e2b-mtp          # the default BRAIN_MODEL
```

`OPENROUTER_API_KEY` plus `BRAIN_LLM=openrouter` switches chat to a hosted model — the key lives only in the brain's environment; the browser never sees it. Chat memory (the assistant recalling past conversations) needs a [Chroma](https://www.trychroma.com/) server on :8003 (`docker compose up -d chroma` starts one); without it the brain logs a warning, disables recall, and serves everything else.

Or run board and brain together with Docker (Chroma and Ollama stay on the host):

```sh
docker compose up --build                # then open http://localhost:3000
```

Chat still needs a model from the host's side: have Ollama running, or `export OPENROUTER_API_KEY=… BRAIN_LLM=openrouter` before composing. Without either, chat fails while the board and every non-chat feature keep working.

Every backend — chat model, embedder, transcriber, search — is chosen by an env var, and an unknown value raises at boot rather than silently picking something else. Every variable is documented in `.env.example`, and the same settings are commented in `docker-compose.yml`.

## How your data is stored

Local-first: the browser keeps a working copy in `localStorage`, and the Node server persists every change to a single SQLite file (`board.db`, or wherever `BOARD_DB` points). No external database server, no cloud dependency, and boards never sync off the machine.

One deliberate durability guarantee: **a card is destroyed only by a two-step act** — delete it from the board, then "Delete permanently" from the Trash. A save missing some cards archives them to the Trash rather than removing them, so a partial or buggy write can never blank your board. Export/Import as JSON moves a board deliberately.

Every test run backs up `board.db` first (timestamped copies under `backups/`, plus Google Drive if [rclone](https://rclone.org/) is configured — one-time OAuth, no Google password ever stored or read), and the server snapshots the database whenever a save brings a card it has never seen.

## The Assistant

The Assistant view talks to the brain — one function-calling agent that researches questions on the web (with cited urls), operates the board in plain language, finds connections between your cards (hybrid dense + BM25 retrieval with a relevance gate, so it can honestly say *"I have nothing on that"*), and recalls past conversations. Each reply shows the tools it actually called.

It cannot put a card on your board by itself: a card it invents is a **proposal**, held off the board until you approve it, and an edit it suggests opens in the ordinary card dialog for you to change and apply. Deleting is yours alone. A mic beside Send dictates into the composer — locally by default (Parakeet on Apple Silicon), and the transcript is editable text, never auto-sent.

Privacy is a design rule, not a setting: conversations are never sent to any tracing or analytics service, web snippets and recalled text are fenced as data rather than instructions before the model reads them, and every link a web search returns is safety-checked before the model may cite it.

## Tests

Developed test-first; all four layers run fully offline (deterministic fake LLM and embedder — no key, no network):

```sh
npm run test:server                                 # server + backup unit suites
uv run --project brain pytest brain/tests -v        # brain unit tests
uv run --with playwright python tests/e2e_test.py   # full e2e — starts everything itself
npm run test:all                                    # all of the above
```

## License

All rights reserved.
