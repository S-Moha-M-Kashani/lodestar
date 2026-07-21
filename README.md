# Question Board

A kanban-style board to collect and prioritize AI engineering questions. Pure HTML/CSS/JavaScript — no frameworks, no build step, no dependencies.

The design is a "question ledger": quad-ruled engineering paper, questions as ruled index cards with permanent ledger IDs (`Q-001`, `Q-002`, …), and priorities as ink stamps.

## Features

- **Four-column question lifecycle**: Inbox → To Research → In Progress → Answered
- **Board / Backlog views**: switch the whole board to a clean, scannable ledger list of the Inbox for triaging many questions at once
- **Drag & drop** cards within and between columns, with drop-position indicator
- **Priorities**: High / Medium / Low color-coded badges, plus a per-column "sort by priority" button
- **Quick capture**: type a question in the Inbox and press Enter
- **Edit modal**: notes, priority, and tags per question
- **Search & filters**: free-text search, priority filter, tag chips
- **Persistence**: auto-saves to localStorage; Export/Import as JSON for backups (commit `questions.json` to this repo if you like)
- **Light/dark theme**, follows your system preference
- **Keyboard support**: fully usable without a mouse

## Run it

Open `index.html` directly in a browser, or serve it:

```sh
python3 -m http.server
# then open http://localhost:8000
```

## Keyboard shortcuts (with a card focused)

| Key | Action |
| --- | --- |
| `Enter` | Edit the question |
| `[` / `]` | Move to previous / next column |
| `Alt` + `↑` / `↓` | Reorder within the column |
| `Delete` | Delete the question (with confirmation) |

## Tests

End-to-end tests drive the app in headless Chrome (requires [uv](https://docs.astral.sh/uv/) and Google Chrome):

```sh
python3 -m http.server 8741 &
uv run --with playwright python tests/e2e_test.py
```

See `plan.md` for the design/implementation plan.
