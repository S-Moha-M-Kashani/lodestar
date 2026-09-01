# Where the data lives, and moving the board to Postgres (2026-08-31)

**Status:** design agreed; Phase 1 not started. Plan:
`docs/superpowers/plans/2026-08-31-postgres-migration-plan.md`.

This document exists because on 2026-08-30 a board disappeared and it took a
full investigation to say why — and the answer was not a bug in any function.
It was that nobody had ever written down every place this project keeps data.
Seven stores had accumulated, each one added for a good reason, and two of them
answered the same URL. That is the kind of failure a map prevents and a code
review does not.

## Every store, as of 2026-08-31

| # | Store | Location | Written by | Verdict |
| --- | --- | --- | --- | --- |
| 1 | `board.db` | `databases/real/board.db` | native `npm start` | the real board |
| 2 | `board.db` **again** | Docker volume `lodestar_board-data`, at `/data/board.db` | the composed container on :3000 | **the defect** |
| 3 | `assistant.db` | `databases/real/assistant.db` | native server; the container opens it **read-only** | one file, one broken writer |
| 4 | `board-3001.db`, `assistant-3001.db` | `databases/test/` | the :3001 sandbox | correct and deliberate |
| 5 | `brain-checkpoints.db` | `databases/real/` | LangGraph's `SqliteSaver` | library-owned |
| 6 | Chroma ×2 | `:8003` (`databases/real/chroma-data`), `:8004` (test) | the brain | different kind of data; derived |
| 7 | Postgres | `:5432`, `~/Projects/postgres` | nothing yet | built 2026-08-31, empty |

Of the seven, exactly one — #2 — is a mistake nobody chose.

## What actually happened on 2026-08-30

Evidence, all of it from the machine rather than from reasoning:

- `docker-compose.yml` sets `BOARD_DB: /data/board.db` on the named volume
  `lodestar_board-data`. `npm start` resolves `databases/real/board.db`
  (`scripts/db-location.mjs`). **Two files, one URL** — whichever stack was
  started last owned :3000.
- The host file held three boards: `main` (created 2026-08-13), `Moha-Pari` and
  `Pari` (2026-08-22 13:28 and 13:29). The volume was created at **13:38 the
  same day** — nine minutes later — and its `board.db` was born
  **2026-08-24 10:35**, empty.
- After that reset the browser pushed its cached copy back, and **62 of the 71
  live cards on the container's `main` board are byte-identical ids** to the
  host's. So the *cards* were restored from `localStorage` and the board looked
  healthy.
- **The set of boards is not in `localStorage`** — `js/core/boards.js` stores
  only *which* board is open (`lodestar:board`), never the list. Nothing
  restored `Moha-Pari` or `Pari`. On 2026-08-30 06:51 the user created
  "Moha-Pari" a second time, in the container's copy.

The failure is therefore precise: **the cache can heal cards and cannot heal
boards.** Any future store must not depend on that asymmetry, and no two stores
may answer one URL.

### The second defect, found in the same investigation

`ASSISTANT_DB` is unset in `docker-compose.yml`, so inside the container it
resolves to `/app/databases/real/assistant.db` — the real file, reached through
the `.:/app:ro` mount. The container therefore opens the true chat record
**read-only** and every write from the composed stack fails. It has not been
noticed because the composed Assistant is rarely the one used.

## Decision

**One Postgres server, outside every application, holding one database per
project. Lodestar's board and chat records become two schemas in one database.**

The server is built and running: `~/Projects/postgres/`, a compose project of
its own, `postgres:18-alpine` pinned by digest, port 5432, database `lodestar`
with a login that cannot connect to any other project's database.

### Why the server belongs to no application

A database inside an application's compose project shares that project's
lifecycle: `docker compose down -v` in that repo destroys it. With several
projects on one server, one careless command in one repo takes out all of them —
2026-08-30 with higher stakes. `tests/postgres.test.js` pins the absence of a
`postgres` service in this repo so the coupling cannot return by accident.

### Why not simply point Docker at `databases/real/board.db`?

It is the right *first* move and Phase 1 does exactly that — one file, both
stacks, no new technology. It does not solve the rest:

- SQLite is one writer at a time and one file per database, so board and chat
  stay two files and the test split stays a second folder of files.
- The whole-board `PUT` sweep, the `rev` check and the "one save in flight"
  rule are all doing, in JavaScript, what a transaction does in a real database.
- Nothing here works from a second machine, which is the reason the sync/merge
  machinery in `js/core/merge.js` exists at all.

Phase 1 is a fix. Postgres is the answer to the question the fix leaves open.

### Trap found while building the server, recorded so it is never rediscovered

**Postgres 18 moved the image's data directory.** `PGDATA` is now
`/var/lib/postgresql/18/docker` and the image declares its volume at
`/var/lib/postgresql` — not the `/var/lib/postgresql/data` that every guide
still shows. Mounting the old path leaves the real data in the container's
writable layer, where the next redeploy deletes it: the 2026-08-30 failure
rebuilt exactly, and invisible until the first `--force-recreate`. Verify after
any version bump with
`docker image inspect <image> --format '{{json .Config.Volumes}}'`.

## The schema

`scripts/postgres/001-schema.sql`, already written and applied. Six tables in
two schemas, mirroring the two SQLite files column for column.
`tests/postgres.test.js` derives the expected tables and columns **from
`server.js` itself**, so the mirror fails on the day a column is added rather
than on the day a migration runs.

Three places the mapping is not one-to-one, each argued in the file's header:
`INTEGER → BIGINT` (Postgres's `INTEGER` is 4 bytes and epoch-millisecond
stamps overflow it; SQLite's widens on its own), `REAL → DOUBLE PRECISION`, and
`INTEGER PRIMARY KEY AUTOINCREMENT → BIGINT GENERATED BY DEFAULT AS IDENTITY`
(**BY DEFAULT**, not `ALWAYS`, because the migration has to write the ids the
SQLite rows already carry).

**Two schemas, not one flat namespace.** `sessions.board_id` carries no foreign
key because `boards` lives in another file and SQLite cannot reference across
one. Postgres *could* express that constraint now and deliberately does not:
the separation is a decision about what the whole-board `PUT` may sit beside,
not a limitation being worked around. A schema boundary keeps the decision
visible.

**LangGraph's tables are not mirrored.** `brain-checkpoints.db` is created and
migrated by the library's own saver; hand-writing those tables would pin a
private schema this project does not control. `PostgresSaver.setup()` creates
them when that phase arrives.

## The dependency decision

`server.js` has **zero npm dependencies** — raw `node:http`, `node:sqlite` —
and that has been deliberate. `node:sqlite` is built into Node; there is no
built-in Postgres client. So this migration spends that property, and it should
be spent knowingly:

- **`pg`** (node-postgres). One direct dependency, no native build required,
  the de facto standard. **Chosen.**
- **`postgres`** (porsager). Smaller and faster, but a tagged-template API that
  reads nothing like the prepared statements already in `server.js`, making
  every one of the 122 call sites a rewrite rather than a translation.
- **Hand-rolled wire protocol.** Genuinely possible and genuinely foolish:
  authentication, type decoding and the extended query protocol are weeks of
  work to reimplement badly.
- **Keep SQLite, share the file over a network mount.** SQLite's own
  documentation warns against locking over network filesystems. Rejected.

`npm ci` gains a lockfile and CI gains an install step. That is the price.

## What changes, what does not

| Area | Before | After |
| --- | --- | --- |
| Board + chat | 2 files × 2 stacks = **4 copies** | one database, two schemas, **one copy** |
| Test data | `databases/test/*.db` | a second database, `lodestar_test` |
| Conversation state | `brain-checkpoints.db` | same Postgres, via `PostgresSaver` |
| Vectors | Chroma `:8003` / `:8004` | **unchanged** — different job, and derived |
| Backups | `VACUUM INTO` + JSON exports | `pg_dump` + the same JSON exports |
| The brain | never touches SQLite; writes via the Node API | **unchanged** |

The brain needing no changes is not luck: invariant 2 — *the brain never
touches SQLite, all writes via the Node API* — is what makes the storage layer
replaceable at all. `test_guardrails.py` pins that surface.

## The backend seam

`LODESTAR_DB_BACKEND` = `sqlite` | `postgres`. Unknown value **raises at boot**;
there is no `auto` mode. This is the project's existing convention (invariant 3,
`make_chat_model`, `make_embeddings`, `BRAIN_URL_SAFETY`, `BRAIN_TRACING`)
applied to storage, and it is what lets the cut-over be reversible for as long
as both paths exist: one environment variable returns you to the file that
worked yesterday.

The default stays `sqlite` until the Postgres path has passed the full suite
and carried the real board for a week. Flipping the default is its own commit,
with its own decision.

## What Postgres makes better, honestly

- **`writeBoard` becomes a transaction.** The sweep, the upsert and the `rev`
  check currently rely on SQLite's one-writer-at-a-time. `SERIALIZABLE` gives
  the same guarantee where two machines are writing, which is the case the
  current design cannot cover.
- **`js/core/merge.js` stops being load-bearing.** It exists because two
  browsers hold two caches and the server could not arbitrate. It stays as a
  cache-reconciliation path, but the "stale write archived 24 cards" class of
  bug is closed by the database rather than by JavaScript.
- **"Where is my board?" gets one answer.**

## Accepted costs

- **One npm dependency**, and the zero-dependency property of `server.js` ends.
- **Every database call becomes asynchronous.** `DatabaseSync` returns rows;
  `pg` returns promises. 122 call sites. Mitigated by the request handler
  already being `async (req, res)` (`server.js:1478`) — the conversion is wide
  and mechanical rather than structural.
- **A running server is now required** to use the board. SQLite needed nothing.
  Mitigated by `restart: unless-stopped` and a health check.
- **The `.db` files stop being the record**, so "just open it in a viewer" is
  replaced by `psql` or a client. `pg_dump` replaces file copying.
- **`databases/real/` does not disappear** — Chroma still lives there.

## Open questions, to settle before the phase that needs them

1. **Does the test stack get its own database or its own server?** A second
   database (`lodestar_test`) on the same server is simpler; a second server is
   a stronger guarantee that a test can never reach real rows. The current file
   split chose the stronger guarantee. Decide at Phase 6.
2. **Does `rev` stay a hash of the bytes sent?** It has no blind spot today. A
   transaction id or `xmin` would be cheaper but changes what the value means.
   Decide at Phase 3, and write the reasoning where the constant lives.
3. **Does `position` stay re-derived from array order?** The reorder-loss cost
   was accepted because reorders do not bump `updatedAt`. A real transaction
   may make a better answer affordable.
