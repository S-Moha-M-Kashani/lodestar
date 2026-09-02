# One database for local and Docker runs

**Date:** 2026-07-23
**Status:** Approved by user, ready for planning

## Problem

Lodestar currently keeps two divergent databases depending on how it is run:

- `node server.js` → `./board.db` next to `server.js` (the default `DB_PATH`)
- `docker compose up` → `/data/board.db` on the named volume `board-data`, which
  lives inside Docker Desktop's Linux VM and is invisible to the host filesystem

Questions added in one mode never appear in the other. The user runs Docker as
the canonical mode, but wants direct Node runs (for development) to see the
same, real board. The data currently in the local `./board.db` is the real
board; the named volume's copy is stale and disposable.

## Decision

**Approach A — one shared SQLite file via a bind mount.** Keep SQLite and the
zero-npm-dependency server exactly as they are; move the database to a host
folder that the container bind-mounts. Alternatives considered and rejected:

- *A networked DB server (Postgres / libsql on a port):* concurrent-safe and
  multi-user-ready, but adds a client library, credentials, and a second
  always-on service to store one person's board. Revisit if/when a hosted
  multi-user phase gets real requirements.
- *Docker-only ownership of the DB:* simplest rule (one writer ever), but
  contradicts the requirement that direct Node runs see the real board.

## Design

### Layout

```
lodestar/
└── data/
    └── board.db      ← the one true database (gitignored)
```

### Changes

1. **`server.js` — default path.**
   `const DB_PATH = process.env.BOARD_DB || join(ROOT, 'data', 'board.db');`
   The existing `mkdirSync(dirname(DB_PATH), { recursive: true })` already
   creates `data/` on first boot. `BOARD_DB` still overrides everything, so the
   e2e suite (which points `BOARD_DB` at a temp dir) is untouched.

2. **`server.js` — legacy auto-migrate.** On startup, if the old default
   `./board.db` exists and `./data/board.db` does not, **move** (rename) the old
   file into place before opening the database. The real data migrates itself on
   the first run of the new code; no machine silently starts a fresh empty DB
   beside a full legacy one. The move only applies when `BOARD_DB` is unset
   (i.e. only for the default path).

3. **`docker-compose.yml` — bind mount instead of named volume.**
   ```yaml
   volumes:
     - ./data:/data      # was: board-data:/data
   ```
   `BOARD_DB: /data/board.db` stays. The top-level `volumes:` block is removed.
   The container's `/data/board.db` is the host's `./data/board.db`. The
   container runs as root, so bind-mount file ownership is not an issue.

4. **`.gitignore`** — add `data/` (the existing `*.db` patterns already cover
   the file itself; this also covers any future files under `data/`).

5. **`README.md`** — update the storage-model section: the board lives in
   `./data/`; copy that folder to back up or move machines. Document the one
   operational rule: do not run Docker and `node server.js` at the same time
   (SQLite locking is unreliable across the VM boundary; in practice the
   port-3000 collision prevents it accidentally). Update the new-laptop
   instructions accordingly.

### Cleanup (one-time, manual)

- After verifying the new setup, remove the stale named volume:
  `docker volume rm lodestar_board-data`. Its contents are stale by the user's
  explicit call; no merge is needed.

### Error handling

- Simultaneous host + container writers: documented rule, no code guard
  (YAGNI — the port collision is the real-world guard).
- First boot with neither old nor new DB present: unchanged behaviour —
  `mkdirSync` + SQLite create an empty database.

### Testing

- e2e suite unaffected (uses `BOARD_DB`).
- New test coverage: starting the server with no `BOARD_DB` (from a temp copy
  of the project layout) creates `data/board.db`, not `./board.db`; and a
  pre-existing legacy `./board.db` is moved to `data/board.db` with its
  contents intact.
- Manual acceptance: add a question under Docker, stop the container, run
  `node server.js`, see the same question (and vice versa).

## Future phases (out of scope here, recorded for context)

- **Phase 2 — hosted/static mode:** the app already keeps the full board in
  `localStorage` and treats the server as a sync target; a hosted mode is
  mostly "tolerate a missing API", with optional SSO for a signed-in area.
- **Phase 3 — document attachments + RAG:** documents as plain files under
  `./data/files/<card-id>/…` with a metadata table in `board.db`; chunks and
  their MiniLM vectors (already computed browser-side by transformers.js for
  the Overview map) persisted as rows/BLOBs in the same `board.db`; brute-force
  cosine search in JS, with `sqlite-vec` as the escalation path. No FAISS, no
  separate vector database. Phase 1's `./data/` folder is the single home for
  all of it.
