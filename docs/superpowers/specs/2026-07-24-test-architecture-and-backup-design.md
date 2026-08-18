# Test architecture + Google Drive backup — design

Date: 2026-07-24
Status: approved by user (brainstorming session)

## Goal

Make it safe to change Lodestar: every button, feature, and API branch is covered by
tests; a test-driven policy is written into CLAUDE.md; and the real `board.db` is
backed up locally **and** to Google Drive before any test run. The eval architecture
must be ready for multiple agents (more agents planned for August 2026).

## Context (coverage audit, 2026-07-24)

- `tests/e2e_test.py` already has ~125 checks covering most UI: card CRUD, drag &
  drop, keyboard moves, all filters, all 4 themes, import/export, undo/history,
  trash/purge, all views (Board/Backlog/Overview/Matrix/Areas/Review/Assistant),
  DB round-trips and the soft-delete omit guard.
- `brain/tests/` covers the Python package per module (agent loop, board tools,
  config, LLM providers, RAG/Leiden, server, websearch) — offline via
  `BRAIN_LLM=fake BRAIN_EMBEDDER=hash`.
- Untested: `server.js` error branches (405s, 400 guards, ~5 MB payload cap, proxy
  503 fallback, `/api/rag/*` routing, static whitelist/404, legacy column
  migrations) and a handful of frontend edge cases (listed in §2). There are no
  quality evals for RAG retrieval or agent tool-calling.

## Decisions made with the user

1. **Drive auth: rclone + OAuth.** Google blocks programmatic username/password
   login; a stored password would be both unsafe and non-functional. One-time
   `rclone config` browser sign-in; rclone stores an OAuth token in
   `~/.config/rclone/rclone.conf`. No password is ever stored in or read by this
   repo.
2. **TDD policy: tests-with-change.** Every feature or bug fix ships with tests in
   the same change; the full suite must pass before commit. No mandatory
   red-green-first ceremony, no pre-commit hook.
3. **Approach: three-layer fit** (chosen over "everything in the Python e2e" and
   "adopt a JS test framework"): `node:test` unit suite for `server.js`, extend the
   existing e2e, add an eval layer in the brain, keep the zero-npm-dependency rule.

## 1. Server unit suite (JS, new)

`tests/server.test.js`, run with `node --test tests/`. Boots `server.js` as a child
process on a temp port with a temp `BOARD_DB`, talks to it with `fetch`. No npm
dependencies. Covers:

- 405 on wrong methods for `/api/state`, `/api/trash`, `/api/cards/:id`.
- 400 guards: malformed JSON body, non-array `cards`, card missing `id`.
- ~5 MB payload cap in `readBody`.
- Proxy behaviour: `/api/agent/*` and `/api/rag/*` routing; 503 "assistant
  unavailable" fallback when the brain is down (point proxy at a dead port).
- Static file whitelist and 404 handling.
- Boot-time column migrations: create an old-schema `board.db`, boot the server,
  assert `PRAGMA table_info` shows the added columns and data survives.
- API-level soft-delete omit guard (PUT missing a card ⇒ card soft-deleted, still
  in `/api/trash`).

## 2. E2E gap fills (Python, extend `tests/e2e_test.py`)

Same `check(name, cond)` style and stable class-name selectors. Add:

- Category rail: CAT_LIMIT-full alert, duplicate-name alert, hue-picker selection
  asserted on the created category.
- Decisional-balance pro/con preview (cards tagged "decision").
- Server-offline banner (`announce` on push failure) and recovery.
- Assistant error path: brain unreachable ⇒ friendly error message in chat.
- Overview t-SNE "too few cards" fallback message.
- Backlog sort-by-type (distinct from Board sort).
- Import "add" mode adopts categories from the file's registry.

## 3. Brain eval architecture (Python, new — multi-agent foundation)

New package `brain/tests/evals/`, separate from unit tests: evals measure
*quality/behaviour*, unit tests measure correctness.

- **Scenario files** `brain/tests/evals/scenarios/*.json`. Each declares:
  `agent` (registry key), seeded board state, user message, expected tool calls
  (names + argument matchers), expected board effect, expected answer content.
  Adding an eval = adding a JSON file. No harness edits.
- **Harness** `brain/tests/evals/conftest.py`: builds the app via `create_app()`;
  resolves the agent under test from a small registry (one entry today). New
  agents = one registry line + scenario files — extend by adding implementations,
  never by editing call sites (matches the Protocol-seam invariant).
- **Tool-calling evals**: run scenarios through the agent loop with the scripted
  fake LLM; assert tool choice, arguments, multi-step sequencing, and error
  recovery. Deterministic, fully offline, runs in CI-less local dev.
- **RAG evals**: labelled card fixtures with known topic clusters; assert
  `find_related` precision / hit-rate@k against explicit thresholds. Offline with
  the `hash` embedder; the same fixtures re-run under `fastembed` when installed.
- **Live mode**: `BRAIN_EVAL_LIVE=1` + `OPENROUTER_API_KEY` re-runs tool-calling
  scenarios against the real LLM (pytest marker, skipped by default so nothing
  costs money accidentally).

Run: `uv run --project brain pytest brain/tests/evals -v`.

## 4. Backup before every test run (local + Google Drive)

`scripts/backup-db.mjs` — plain Node, zero npm dependencies, shells out to rclone:

1. Copy `board.db` (or `$BOARD_DB`) → `backups/board-<UTC timestamp>.db`.
2. Prune local backups to the most recent 30.
3. `rclone copy` the new backup to `gdrive:lodestar-backups/` (remote name
   configurable via `LODESTAR_RCLONE_REMOTE`, default `gdrive`).

Failure policy: if rclone is missing/unconfigured/offline ⇒ keep the local backup,
print a loud warning with setup instructions, exit 0 — tests are never blocked.
Missing `board.db` (fresh checkout) ⇒ note and exit 0.

Wiring — every test entry point backs up first:

- npm scripts call the backup before running tests (see §5).
- `tests/e2e_test.py` invokes the backup script at startup (subprocess), so running
  it directly still backs up. (The e2e suite already uses temp DBs; the backup is a
  safety net + automatic off-site copy of real life data.)

The backup script gets its own unit test, `tests/backup.test.js` (picked up by the
same `node --test tests/` run): temp dirs + a stub `rclone` on PATH asserting
invocation, pruning, and the warn-don't-block path.

`backups/` is git-ignored.

## 5. Command surface + CLAUDE.md policy

`package.json` scripts:

- `npm run backup` — backup only.
- `npm run test:server` — backup → `node --test tests/`.
- `npm run test:all` — backup → JS suite → brain unit tests → brain evals → e2e.

CLAUDE.md gains a **Testing policy** section:

- Tests-with-change rule: every feature or bug fix ships with tests in the same
  change; the full relevant suite passes before commit.
- Command table (the scripts above + existing pytest/e2e commands).
- Guarantee: every test entry point backs up `board.db` locally and to Google
  Drive (rclone/OAuth; no password stored).
- Pointer to `brain/tests/evals/` scenario format for future agents.

## Error handling summary

- Backup: warn-don't-block on any rclone failure; local copy is the floor.
- Server tests: each test gets a fresh temp DB/port; child process killed in
  teardown even on assertion failure.
- Evals: thresholds explicit in scenario/fixture files; live-mode tests skipped
  unless explicitly enabled.

## Out of scope

- Pre-commit hooks (rejected by user in favour of tests-with-change).
- New npm/JS test frameworks (zero-dependency rule).
- Restore-from-Drive tooling (manual `rclone copy` back suffices for now).
- The new agents themselves (August 2026) — only the architecture that hosts them.
