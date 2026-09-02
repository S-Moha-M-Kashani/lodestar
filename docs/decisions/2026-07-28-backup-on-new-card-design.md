# Backup on a new card — design

Date: 2026-07-28
Status: approved by user (brainstorming session)

## Goal

Back up `board.db` when a new entry actually appears on the board, instead of only
when a test suite happens to run. Today the backup is a side effect of development
activity; after this change it follows the data.

## Context (audit, 2026-07-28)

`scripts/backup-db.mjs` is invoked from exactly four places, all of them test entry
points: `npm run backup` (manual), `npm run test:server`, `npm run test:all`
(`package.json`), and `tests/e2e_test.py:134` (at import time). There is no cron
entry, no launchd agent, no git hook, and no call from `server.js` or Docker —
verified. Consequences:

- Snapshot cadence tracks the developer's rhythm, not the user's. 7 runs on Jul 24,
  4 on Jul 26, 10 on Jul 27, 9 on Jul 28 — and zero on Jul 25, a day on which the
  board could have changed all day unrecorded.
- `keep` is 30 **files**, not 30 days (`backup-db.mjs:13`). The directory is already
  at the cap and spans only Jul 24–28, so a busy day evicts a week of history.
- `node --test tests/*.test.js` is the one test path that skips the backup entirely.

Relevant existing structure:

- Every board write from every writer funnels through `writeBoard(cards)`
  (`server.js:248`), reached only from `PUT /api/state` (`server.js:336`). There is
  no create endpoint — the frontend debounce-pushes the whole board 150 ms after a
  change (`app.js:281`), and the brain writes through the same route
  (`BoardClient.save_cards`), per invariant 2.
- `journal_mode=delete` with no `-wal`/`-journal` sidecars, SQLite 3.53.3.

## Decisions made with the user

1. **Add the frontend trigger, keep the test-run backups.** The documented backup
   guarantee in CLAUDE.md stands unchanged — tests mutate `board.db`, so a pre-test
   snapshot is the safety net if a suite corrupts it. This change is purely
   additive.
2. **A "new entry" is a new card only.** Edits, column moves, reorders and deletes
   do not trigger a backup.
3. **One backup per PUT that carries new cards; retain the newest 100.** No
   cooldown. A single payload with twelve new cards produces one snapshot, not
   twelve.
4. **Agent-created cards trigger a backup too**, for now. The user additionally
   wants agent-created cards to require confirmation before being backed up; that
   gate does not exist today (the brain's `create_question` commits straight to
   SQLite and the browser merely adopts the result, `app.js:2841`) and is deferred
   to its own spec — see "Out of scope".

## 1. Detecting a new entry

Inside `writeBoard`, before the upsert loop:

```js
const known = new Set(db.prepare('SELECT id FROM cards').all().map((r) => r.id));
const created = clean.filter((c) => !known.has(c.id)).length;
```

The query deliberately covers **all** rows, not just live ones. A card restored from
Trash has an id the table already knows, so restoring an old thought is not counted
as capturing a new one. The soft-delete design gives this semantic for free.

`writeBoard` returns `{ board, created }` rather than the bare board. It has a single
caller (`server.js:346`), which destructures it; nothing else depends on the shape.

## 2. Firing the backup off the request path

`runBackup` shells out to rclone with `spawnSync` (`backup-db.mjs:34,42`) — network
bound and blocking. Called inline from a request handler it would freeze the
single-threaded server for the entire Drive upload, stalling every other request.
So the route responds first, then spawns the existing script detached:

```js
spawn(process.execPath, [BACKUP_SCRIPT], {
  detached: true, stdio: 'ignore',
  env: { ...process.env, BOARD_DB: DB_PATH },
}).unref();
```

- `BOARD_DB` is passed explicitly so the child snapshots the database *this* server
  instance opened, not whatever the script would default to. Without it the test
  server on :3001 would back up the wrong file.
- A module-level in-flight flag skips the spawn while a previous child is still
  running, so a burst of writes cannot pile up processes.
- The snapshot is taken **after** the transaction commits, so the backup contains
  the new card. A pre-write snapshot would omit the very thought it was triggered
  by.
- No feedback loop: the client adopts server state and pushes the whole board back
  (`adoptServerBoard` → `saveState`), but by then every id is known, so
  `created` is 0.

## 3. Changes to `scripts/backup-db.mjs`

1. **`keep` default 30 → 100.** Pruning logic is unchanged; it already keeps the
   newest `keep` files and deletes the rest.
2. **Snapshot via `VACUUM INTO`, falling back to `copyFileSync`.** The current copy
   is safe only because the server is idle when tests run. This change introduces
   copies taken while the server is live and may be mid-transaction, so a plain
   file copy could capture a torn database. `VACUUM INTO` is atomic and consistent,
   and is available in SQLite 3.53.3. The `copyFileSync` fallback keeps the script
   working if the destination or SQLite version ever refuses.

## 4. Keeping tests out of the real backup history

Essential, not optional. `backupsDir` is hardcoded to `ROOT/backups`
(`backup-db.mjs:11`) while the suites run against temp databases. Without a guard,
every card the e2e suite creates would drop a snapshot of a throwaway board into the
user's genuine history and evict a real one under the 100-file cap.

Two environment seams:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LODESTAR_BACKUP_ON_WRITE` | on | Set to `0` to disable write-triggered backups. Test harnesses set it off. |
| `LODESTAR_BACKUP_DIR` | `ROOT/backups` | Lets tests assert against a temp directory. |

## 5. Tests (written first)

| Layer | Assertion |
| --- | --- |
| `tests/server.test.js` | PUT containing an unseen id → exactly one snapshot appears |
| | PUT that only edits / moves / reorders existing cards → no snapshot |
| | PUT that restores a soft-deleted card → no snapshot |
| | One PUT carrying 5 new cards → exactly one snapshot |
| | `LODESTAR_BACKUP_ON_WRITE=0` → no snapshot |
| `tests/backup.test.js` | `keep` defaults to 100; prune retains the newest 100 |
| | `VACUUM INTO` produces a readable DB with the same rows |
| `tests/e2e_test.py` | Adding a card through the UI produces a snapshot |

The spawn is detached and asynchronous, so these poll for the file with a short
timeout rather than asserting immediately. Tests set `LODESTAR_BACKUP_DIR` to a temp
directory and `LODESTAR_BACKUP_ON_WRITE=1` explicitly, so they exercise the feature
without touching `backups/`.

## Out of scope

**The agent-card confirmation gate.** The user wants agent-created cards confirmed
before they are backed up. That requires a pending state on cards (schema change),
agent writes landing as pending rather than live, review UI to approve or reject,
reject semantics that respect invariant 2 (Trash or purge?), and care in
`writeBoard`'s reconciliation — which soft-deletes any live card absent from a
payload, so pending cards the frontend has not seen would be archived on the next
push. It is a product decision about how much the assistant is trusted, comparable
in size to this change. It gets its own brainstorming session and spec; that work
will move the agent-card backup from creation time to confirmation time.

## Non-goals

- No change to the pre-test backup guarantee.
- No scheduled or time-based backups.
- No change to rclone/Drive auth, or to what `DELETE /api/cards/:id` means.
