# Agent card confirmation gate — design

Date: 2026-07-29
Status: approved by user (brainstorming session)

## Goal

A card the Assistant invents should not become part of the board until the user
says so. Today it already is: the brain's `create_question` writes straight
through `PUT /api/state` into SQLite and the browser merely adopts the result
(`app.js`, `adoptServerBoard`), so an agent-created card is durable before the
user has read it. This adds a proposal state and an approve/reject step, and
moves the backup from creation time to confirmation time.

Deferred from `2026-07-28-backup-on-new-card-design.md` ("Out of scope").

## Context (audit, 2026-07-29)

- The agent has exactly **two** mutating tools — `create_question` and
  `update_question` (`brain/src/lodestar_brain/tools/board.py`). There is no
  delete tool, so the gate has a bounded surface.
- `create_question` sends the **whole** card list with one card appended and then
  diffs the response to find the id the server assigned. This is why proposals
  need their own endpoint: once proposals are hidden from `/api/state`, that diff
  finds nothing and the tool reports `card was not created`.
- `MUTATING_TOOLS` in `brain/src/lodestar_brain/server.py` drives a single
  `mutated` flag on the chat response; the browser reacts by adopting the board.
- Only **seven** queries touch the `cards` table (`server.js:207, 215, 267, 270,
  272, 290, 343`), so a flag column can be audited exhaustively.
- **Latent bug found:** ledger numbers are assigned client-side by `ensureNums`
  (`app.js:140`), and `adoptServerBoard` does not call it. An agent-created card
  therefore renders as `Q-000` until the next reload. It sits directly in this
  feature's path, so it is fixed here.

## Decisions made with the user

1. **Reject sends the card to Trash**, recoverable, rather than erasing it. Keeps
   invariant 2 exactly as written: `DELETE /api/cards/:id` stays the only hard
   delete in the system, and a mis-click is undoable.
2. **Only new cards are gated.** `update_question` continues to apply
   immediately — agent edits are already covered by Undo and History, and a
   proposed *edit* would mean storing and rendering a diff against a live card.
3. **Review happens in the Assistant view**, with a count badge on the Assistant
   tab so proposals are discoverable from any view.
4. **Proposals are per-card.** Three proposed cards means three approvals; no
   "Approve all" until the lack of one becomes annoying.

## 1. Storage — a `pending` flag on `cards`

```sql
ALTER TABLE cards ADD COLUMN pending INTEGER NOT NULL DEFAULT 0
```

Boot-time migration via `PRAGMA table_info`, matching the nine columns already
migrated that way. A proposal is an ordinary card row with `pending = 1`.
Confirming flips it to `0`; the row keeps its id and `createdAt`, so confirmation
is a state change rather than a copy.

A separate `proposals` table was considered and rejected: because rejecting sends
the card to Trash, a rejected proposal has to live in `cards` anyway, so a second
table would mean mirroring eighteen columns and moving rows between them for no
gain.

Three of the seven queries change:

| Query | Change | Why |
| --- | --- | --- |
| `readBoard` (`:207`) | `AND pending = 0` | proposals stay off the board |
| soft-delete sweep (`:290`) | `AND pending = 0` | **load-bearing** — without it the browser's next whole-board PUT archives every proposal, since they are not on its board |
| upsert (`:272`) | preserve `pending` on conflict | only the confirm route may clear the flag |

`readTrash` (`:215`), the backup detection query (`:267`) and `purgeCard`
(`:343`) are unchanged. The detection query deliberately still counts pending
rows as known ids, so the browser's post-confirmation push cannot trigger a
second snapshot.

## 2. Reject clears `pending` as well as setting `deleted_at`

Subtle and worth stating explicitly: rejecting sets **both** `pending = 0` and
`deleted_at = now`. If it left `pending = 1`, restoring the card from Trash would
put back a row still invisible to the board, and the restore would look broken. A
rejected proposal restores as an ordinary card.

## 3. Four proposal routes

Proposals never travel through a whole-board PUT, so invariant 1 is untouched.

| Route | Effect | Backup |
| --- | --- | --- |
| `POST /api/proposals` | one card in, `pending = 1` row out | **no** |
| `GET /api/proposals` | the pending list | no |
| `POST /api/proposals/:id/confirm` | `pending = 0`, returns the board | **yes, one** |
| `POST /api/proposals/:id/reject` | `pending = 0`, `deleted_at = now` | no |

Confirm and reject return 404 for an unknown id, and for an id that is not
actually pending — confirming an already-live card is a bug, not a no-op.

The confirm route calls the existing `backupAfterNewCards()` from the
backup-on-new-card change, so the snapshot lands at the moment the user accepts
the card, which is what the user asked for.

## 4. The brain signals "proposed", not "mutated"

`MUTATING_TOOLS` becomes `{'update_question'}` and a new
`PROPOSING_TOOLS = {'create_question'}` sets a separate `proposed` flag on the
chat response. The two are different events and the frontend must not conflate
them: `mutated` means adopt the board, `proposed` means refresh the proposals
list. Creating a proposal changes nothing on the board, so adopting would be
pointless work and would make the board flash.

`create_question` posts a single card to `POST /api/proposals` and returns the
stored proposal. Its tool description states that the card awaits the user's
approval, so the model reports "I've proposed…" instead of claiming it added
something. `BoardClient` gains a `create_proposal` method; `save_cards` is
untouched and keeps its full-list contract.

A proposal is invisible to the agent's own `list_questions`, which reads
`/api/state`. The agent cannot see its own unapproved suggestions — intended, so
it never builds on a card the user has not accepted.

## 5. Frontend — a Proposed section and a badge

A `Proposed` section at the top of the Assistant view lists **every** pending
proposal, not only those from the current message, each with Approve and Reject.
Per invariant 4 these are new class names, never renames: `.proposal`,
`.proposal-title`, `.proposal-meta`, `.proposal-approve`, `.proposal-reject`,
`.view-badge`.

The badge lives on the Assistant button in the view switcher and carries the
count, so a proposal made while the user is on the Board is still noticed.
Proposals are fetched on load (for the badge), on entering the Assistant view,
and after any reply carrying `proposed`.

Approve → `POST .../confirm` → `adoptServerBoard()` → refresh proposals.
Reject → `POST .../reject` → refresh proposals; the card is now in Trash.

**Ledger fix:** `adoptServerBoard` calls `ensureNums` before `saveState`, so a
confirmed card gets its real `Q-0NN` immediately instead of showing `Q-000` until
a reload. Numbers are therefore assigned at confirmation, and rejected proposals
never consume one.

## 6. Tests (written first)

| Layer | Assertion |
| --- | --- |
| `tests/server.test.js` | `POST /api/proposals` creates a card absent from `GET /api/state` |
| | `GET /api/proposals` lists it |
| | **a browser PUT omitting proposals does not trash them** |
| | creating a proposal triggers no backup |
| | confirm makes the card live and triggers exactly one backup |
| | reject trashes the card and triggers no backup |
| | a rejected proposal restores from Trash as a normal live card |
| | confirm/reject on an unknown or non-pending id → 404 |
| `brain/tests/` | `create_question` posts to `/api/proposals` and reports the card awaits approval |
| | chat sets `proposed` for `create_question` and `mutated` for `update_question` |
| `tests/e2e_test.py` | a proposal renders in the Assistant view and the tab badge shows the count |
| | Approve puts the card on the board with a real `Q-` number, not `Q-000` |
| | Reject removes it from the list and it appears in Trash |

Backup assertions reuse the sandbox from the previous change
(`LODESTAR_BACKUP_DIR` at a temp dir, `LODESTAR_RCLONE_BIN` at a missing path),
so no test touches the real `backups/` or Google Drive.

## Non-goals

- No gating of `update_question`, and no proposed-edit diffs.
- No "Approve all" / bulk actions.
- No change to the Trash or purge semantics beyond reject writing into them.
- No change to how the RAG index treats cards; proposals are simply not indexed
  until confirmed, because the index reads the live board.
