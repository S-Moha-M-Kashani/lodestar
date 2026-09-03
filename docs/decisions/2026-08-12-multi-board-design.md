# Several boards, one database — design

*2026-08-12*

> **Amended 2026-08-20: categories are per board after all.** The shared
> registry decided below did not survive contact with use. Every browser caches
> the registry inside each board's localStorage snapshot and pushes it back on
> load ("the local board wins"), so a single shared registry was rewritten by
> whichever board's stale copy pushed last — categories leaked onto new boards,
> and a category deleted on one board resurrected the next time another board
> was opened. The fix scopes the `categories` table by `board_id` (composite
> primary key, table rebuilt on migration with every board — deleted ones
> included — keeping a copy of the registry it showed the day before). A new
> board is seeded with the default life areas at creation, and only then: an
> emptied registry is a real state, never re-seeded at boot. Tests:
> `tests/boards.test.js`. Where the text below says categories are shared, this
> note wins.

## What this adds

Lodestar has always held exactly one board. This adds as many as you want: a
picker in the app header switches between them, and **New board** / **Rename** /
**Delete** manage the set. Cards belong to a board; chats belong to a board; the
category registry does not.

## What a board scopes

| Data | Scoped | Why |
| --- | --- | --- |
| Cards (live, trashed, proposed, suggested edits) | **per board** | This is the whole point. A card is on a board or it is nowhere. |
| Chat sessions and their messages | **per board** | The Assistant on the Work board must not answer out of the Home board's conversations, and it must not propose a card onto a board you are not looking at. |
| Categories (life areas and their hues) | **shared** | Colour means category everywhere in this app. Per-board hues would make the same colour mean two things on two screens, and the picker already tells you which board you are on. |
| Theme, view, habit mute | **shared** | Preferences, not data. |
| Undo timeline | **per board** | It restores card snapshots. A cross-board undo would put board A's cards on board B. |

Ledger numbers are **per board**: each board counts its own cards from `C-001`,
which is what falls out of `nextNum()` reading the active board's cards. Two
cards on two boards can both be `C-007`; the pair `(board, number)` is what
identifies a card to a human, the `id` is what identifies it to the machine.

## Schema

A new table in `board.db`, and one column on `cards`:

```sql
CREATE TABLE boards (
  id         TEXT PRIMARY KEY,
  name       TEXT    NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER
);

ALTER TABLE cards ADD COLUMN board_id TEXT NOT NULL DEFAULT 'main' REFERENCES boards(id);
```

`boards` is created and seeded with the default board **before** the `cards`
migration runs, or the first card would reference a board that does not exist.
The default board keeps the id `main` and is named *Lodestar*; every card that
predates this feature lands on it by the column default, so an existing
`board.db` opens on a board that looks exactly like the one it had.

`card_edits` needs no column — an edit points at a `card_id`, and that card
already knows its board.

**`sessions.board_id` in `assistant.db` carries no foreign key**, because
`boards` lives in a different file. That is a real seam, not an oversight: the
chat record is deliberately in its own database (a bad whole-board `PUT` must
never sit next to two kinds of data), and SQLite cannot reference across files.
The board id is validated by the server on the way in instead. `messages` needs
no column for the same reason `card_edits` does not: a message belongs to a
session, and the session knows its board.

### Foreign keys and `ALTER TABLE`

`node:sqlite` enables foreign key constraints by default, and SQLite refuses
`ADD COLUMN` with a `REFERENCES` clause and a non-NULL default while they are
on. The migration therefore turns them off for the length of the statement and
back on afterwards, then runs `PRAGMA foreign_key_check` to assert what the
pragma was turned off to assume — so a database created before this feature and
one created after it end up with the *same* schema rather than diverging by a
constraint only new files have.

Worth knowing, because it made this look optional at first: SQLite accepts the
statement when the table is **empty**. A test that migrates a database with no
cards in it passes against a migration that would fail on every real one.

## Server

New routes, all under `/api/boards`:

| Route | Does |
| --- | --- |
| `GET /api/boards` | Live boards, ordered; plus the default board's id. |
| `POST /api/boards` | Create. Returns the board. |
| `PATCH /api/boards/:id` | Rename. |
| `DELETE /api/boards/:id` | Soft-delete: stamp `deleted_at`. Cards and chats are untouched. |
| `GET /api/boards/trash` | Deleted boards, newest first, with their card counts. |
| `POST /api/boards/trash/:id/restore` | Un-stamp. The board comes back whole. |
| `DELETE /api/boards/trash/:id` | The one hard delete: erases the board, its cards, its edits, its chats. Requires `deleted_at IS NOT NULL`, so no single call both hides a board and destroys it. |

Every existing board-data route takes `?board=<id>` and defaults to the default
board when it is absent — so every curl, eval and test written before this
feature still addresses a real board instead of erroring.

**The load-bearing line is in `writeBoard`.** Its soft-delete sweep archives
every live card it was not sent; scoped to nothing, saving board A would archive
the whole of board B on the first keystroke. It takes `AND board_id = ?`, and
the test that proves it is the first one in `tests/boards.test.js`.

The last live board cannot be deleted. A board picker with nothing in it is a
dead end, and "delete the only board" is never what anyone meant.

## Frontend

`js/core/boards.js` owns the active board id — a live binding with a setter,
like every other piece of shared state. It imports nothing from `ui/`, so the
id is readable during module evaluation without waking the cycle rules.

Storage keys become board-scoped: `lodestar:v1:<boardId>` and
`lodestar:history:<boardId>`. The default board keeps the unsuffixed
`lodestar:v1` and `lodestar:history`, so an existing browser's board and undo
history are exactly where they were.

**Switching boards reloads the page.** The alternative is re-initialising the
board, the timeline, the filters, the Review state, the proposal list, the chat
sheet and its panels in the right order and never missing one; the reload is a
hundred milliseconds on a local app and it makes cross-board leakage
structurally impossible rather than a thing to keep getting right. The choice is
written here so the next person knows it was a choice.

The picker is header furniture — static markup, wired once at boot, for the
same reason the Assistant's tools are. It sits **with the brand, under the
tagline**, not in the toolbar: a board is the workspace everything else
filters, searches and edits, not another filter. (It was in the toolbar first,
where it crowded the row enough to overlap the view switch — which is how the
question "is this a filter?" got answered.)

It shows names and no card counts. A count there is painted once and goes stale
on the next card added, and the board you are on is the one whose cards are in
front of you. The Deleted boards dialog does show counts, freshly fetched,
because there "how much is in here" is exactly the question.

**A new board opens empty.** The seed cards are what an empty *app* opens with,
an explanation of the board written as cards; someone who just asked for a new
board would get six cards to delete.

## The brain

`board_id` rides the run config next to `session_id`, and for the same reason:
a tool argument is something the model can name, spoof, or get wrong, and which
board it is working on is not the model's decision. `BoardClient`'s methods take
`board_id` and pass it as a query parameter; the board tools read it from
`config['configurable']`. `ChatBody` gains `board_id`, defaulting to `''` and
omitted rather than sent empty — the same rule `session_id` follows, so the
server can tell "no board named" from "a board named the empty string".

Chat recall is scoped by the same id, so `recall_chat` cannot surface a
conversation from another board.

## Tests

- `tests/boards.test.js` — board CRUD; the `writeBoard` sweep does not cross
  boards; a legacy database migrates onto the default board; the last board
  refuses deletion; purge takes cards and chats with it.
- `tests/e2e_test.py` — create a board, switch to it, prove the first board's
  cards are not there, delete it, restore it.
- `brain/tests/` — the board id reaches `BoardClient` from the run config.
- `tests/frontend.test.js` — the new modules are reachable from `main.js`.

## What this deliberately does not do

- **No moving a card between boards.** It is a real want and a separate feature:
  it needs a picker in the card dialog, a rule for what happens to the ledger
  number, and a decision about whether the move is undoable. Guessing at it here
  would ship a half of it.
- **No per-board categories**, per the table above.
- **No cross-board search or overview.** Every view stays a view of one board.
