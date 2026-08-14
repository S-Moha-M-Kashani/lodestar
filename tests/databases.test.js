// tests/databases.test.js — the databases/ folder and its real/test split.
//
// Contract under test: `resolveBoardDb` and `resolveAssistantDb` in
// scripts/db-location.mjs decide where the two SQLite records live and perform
// one-time moves of files from their older homes. server.js calls both at boot.
//
// The layout they enforce — real data and test data never share a folder:
//
//   databases/real/  board.db, assistant.db, chroma-data/        (:3000 stack)
//   databases/test/  board-3001.db, assistant-3001.db,
//                    chroma-data-3001/                           (:3001 stack)
//
//   resolveBoardDb({ root, env }) -> absolute path server.js should open
//
//   - env.BOARD_DB set              -> returned verbatim, nothing touched
//                                      (Docker, the :3001 test board, every
//                                      test harness pass an explicit path and
//                                      must never migrate).
//   - databases/real/board.db exists -> returned, nothing touched — migrations
//                                      only run when the target does not exist.
//   - databases/board.db exists     -> the pre-split home: backed up first,
//                                      then moved to databases/real/board.db.
//   - legacy root/board.db exists   -> backed up first, then moved to
//                                      databases/real/board.db.
//   - none exist                    -> databases/real/board.db (fresh clone).
//
//   resolveAssistantDb({ root, env }) — the same rules for assistant.db:
//   env.ASSISTANT_DB verbatim; databases/real/assistant.db wins; a pre-split
//   databases/assistant.db is backed up and moved; default is the real/ path.
//   (assistant.db never lived at the repo root, so there is no root legacy.)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync,
  readdirSync, cpSync, copyFileSync, rmSync,
} from 'node:fs';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveBoardDb, resolveAssistantDb } from '../scripts/db-location.mjs';
import { startServer, waitForLine } from './helpers/server-harness.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// env that keeps the migration's pre-move backup local and off Google Drive.
function safeEnv(root) {
  return {
    LODESTAR_BACKUP_DIR: join(root, 'test-backups'),
    LODESTAR_RCLONE_BIN: join(root, 'no-such-rclone'),
  };
}

// This is an integration test: real files on disk.
test('legacy board.db is backed up, then moved into databases/real/', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  writeFileSync(join(root, 'board.db'), 'DBDATA');
  const env = safeEnv(root);

  const p = resolveBoardDb({ root, env });
  assert.equal(p, join(root, 'databases', 'real', 'board.db'));
  assert.equal(readFileSync(p, 'utf8'), 'DBDATA', 'content survives the move');
  assert.ok(!existsSync(join(root, 'board.db')), 'legacy file is moved, not copied');

  // Backed up FIRST: a board-*.db snapshot with the same bytes exists.
  const backups = readdirSync(env.LODESTAR_BACKUP_DIR)
    .filter((f) => f.startsWith('board-') && f.endsWith('.db'));
  assert.equal(backups.length, 1, 'exactly one pre-move backup');
  assert.equal(readFileSync(join(env.LODESTAR_BACKUP_DIR, backups[0]), 'utf8'), 'DBDATA');

  // A second boot is a no-op: same path back, no second backup.
  assert.equal(resolveBoardDb({ root, env }), p);
  assert.equal(readFileSync(p, 'utf8'), 'DBDATA');
  assert.equal(readdirSync(env.LODESTAR_BACKUP_DIR).length, 1);
});

// This is an integration test: real files on disk.
test('a board.db in the pre-split databases/ home moves into databases/real/', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  mkdirSync(join(root, 'databases'));
  writeFileSync(join(root, 'databases', 'board.db'), 'PRESPLIT');
  const env = safeEnv(root);

  const p = resolveBoardDb({ root, env });
  assert.equal(p, join(root, 'databases', 'real', 'board.db'));
  assert.equal(readFileSync(p, 'utf8'), 'PRESPLIT', 'content survives the move');
  assert.ok(!existsSync(join(root, 'databases', 'board.db')),
    'the pre-split file is moved, not copied');

  // Backed up FIRST, exactly like the root-level legacy.
  const backups = readdirSync(env.LODESTAR_BACKUP_DIR)
    .filter((f) => f.startsWith('board-') && f.endsWith('.db'));
  assert.equal(backups.length, 1, 'exactly one pre-move backup');

  // When both older homes hold a file, the newer home (databases/) wins and
  // the stale root file is left for the user — never silently merged or lost.
  const both = mkdtempSync(join(tmpdir(), 'dbloc-'));
  mkdirSync(join(both, 'databases'));
  writeFileSync(join(both, 'databases', 'board.db'), 'CURRENT');
  writeFileSync(join(both, 'board.db'), 'STALE-LEGACY');
  const bothEnv = safeEnv(both);
  const q = resolveBoardDb({ root: both, env: bothEnv });
  assert.equal(q, join(both, 'databases', 'real', 'board.db'));
  assert.equal(readFileSync(q, 'utf8'), 'CURRENT');
  assert.equal(readFileSync(join(both, 'board.db'), 'utf8'), 'STALE-LEGACY',
    'the stray root file is left in place, not deleted');
});

// This is an integration test: real files on disk.
test('an existing databases/real/board.db is never overwritten', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  mkdirSync(join(root, 'databases', 'real'), { recursive: true });
  writeFileSync(join(root, 'databases', 'real', 'board.db'), 'CURRENT');
  writeFileSync(join(root, 'databases', 'board.db'), 'STALE-PRESPLIT');
  writeFileSync(join(root, 'board.db'), 'STALE-LEGACY');
  const env = safeEnv(root);

  const p = resolveBoardDb({ root, env });
  assert.equal(p, join(root, 'databases', 'real', 'board.db'));
  assert.equal(readFileSync(p, 'utf8'), 'CURRENT', 'the target wins; migration must not re-run');
  assert.equal(readFileSync(join(root, 'databases', 'board.db'), 'utf8'), 'STALE-PRESPLIT',
    'stale older files are left for the user, not deleted');
  assert.equal(readFileSync(join(root, 'board.db'), 'utf8'), 'STALE-LEGACY');
  assert.ok(!existsSync(env.LODESTAR_BACKUP_DIR), 'nothing to migrate means nothing to back up');
});

// This is an integration test: real files on disk.
test('explicit BOARD_DB wins verbatim and suppresses the migration', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  writeFileSync(join(root, 'board.db'), 'DBDATA');
  const explicit = join(root, 'elsewhere', 'x.db');
  const env = { ...safeEnv(root), BOARD_DB: explicit };

  assert.equal(resolveBoardDb({ root, env }), explicit);
  assert.equal(readFileSync(join(root, 'board.db'), 'utf8'), 'DBDATA', 'legacy file untouched');
  assert.ok(!existsSync(join(root, 'databases')), 'no databases/ folder conjured up');
  assert.ok(!existsSync(env.LODESTAR_BACKUP_DIR), 'no backup taken');

  // Fresh clone: no legacy, no target — just the default path, nothing created.
  const fresh = mkdtempSync(join(tmpdir(), 'dbloc-'));
  const freshEnv = safeEnv(fresh);
  assert.equal(resolveBoardDb({ root: fresh, env: freshEnv }),
    join(fresh, 'databases', 'real', 'board.db'));
  assert.ok(!existsSync(freshEnv.LODESTAR_BACKUP_DIR), 'fresh clone: no backup taken');
});

// This is an integration test: real files on disk.
test('resolveAssistantDb mirrors the board rules for assistant.db', () => {
  // Fresh clone: the default is the real/ path, nothing created.
  const fresh = mkdtempSync(join(tmpdir(), 'dbloc-'));
  assert.equal(resolveAssistantDb({ root: fresh, env: safeEnv(fresh) }),
    join(fresh, 'databases', 'real', 'assistant.db'));

  // Explicit ASSISTANT_DB wins verbatim (the :3001 test board, every harness).
  const explicit = join(fresh, 'elsewhere', 'a.db');
  assert.equal(
    resolveAssistantDb({ root: fresh, env: { ...safeEnv(fresh), ASSISTANT_DB: explicit } }),
    explicit);

  // A pre-split databases/assistant.db is backed up, then moved to real/.
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  mkdirSync(join(root, 'databases'));
  writeFileSync(join(root, 'databases', 'assistant.db'), 'CHATS');
  const env = safeEnv(root);
  const p = resolveAssistantDb({ root, env });
  assert.equal(p, join(root, 'databases', 'real', 'assistant.db'));
  assert.equal(readFileSync(p, 'utf8'), 'CHATS', 'content survives the move');
  assert.ok(!existsSync(join(root, 'databases', 'assistant.db')),
    'the pre-split file is moved, not copied');
  const backups = readdirSync(env.LODESTAR_BACKUP_DIR)
    .filter((f) => f.startsWith('assistant-') && f.endsWith('.db'));
  assert.equal(backups.length, 1, 'exactly one pre-move backup');

  // A second boot is a no-op, and an existing target is never overwritten.
  assert.equal(resolveAssistantDb({ root, env }), p);
  assert.equal(readFileSync(p, 'utf8'), 'CHATS');
  assert.equal(readdirSync(env.LODESTAR_BACKUP_DIR).length, 1);
});

// This is a unit test: an empty temp root, so nothing on disk is read, moved
// or created — only what the resolvers return (or throw) is asserted.
test('LODESTAR_REFUSE_REAL_DB turns a forgotten env var into an error', () => {
  const root = mkdtempSync(join(tmpdir(), 'lodestar-refuse-'));

  // The incident: a script sets the wrong variable name, so nothing overrides
  // the default and resolution lands on the real board.
  const wrong = { LODESTAR_DB: '/tmp/scratch.db' };
  assert.match(resolveBoardDb({ root, env: wrong }), /databases\/real\/board\.db$/,
    'without the guard, a typo still resolves to real data');

  // With the guard, the same mistake is loud instead of silent.
  assert.throws(
    () => resolveBoardDb({ root, env: { ...wrong, LODESTAR_REFUSE_REAL_DB: '1' } }),
    /refusing to open databases\/real/,
    'the guard must name what it refused and what to set instead');
  assert.throws(
    () => resolveAssistantDb({ root, env: { LODESTAR_REFUSE_REAL_DB: '1' } }),
    /refusing to open databases\/real/,
    'the chat record needs the same guard as the board');

  // And the guard never blocks an explicit path — that is the whole point of
  // setting one, and the test harnesses all do.
  assert.equal(
    resolveBoardDb({ root, env: { BOARD_DB: '/tmp/x.db', LODESTAR_REFUSE_REAL_DB: '1' } }),
    '/tmp/x.db');
});

// This is a configuration invariant.
test('the :3001 stack keeps its databases under databases/test/', () => {
  // The pairing itself (ports, AGENT_URL) is covered in server.test.js and
  // ports.test.js; what this pins is the *location*: persistent test data
  // lives in databases/test/, never at the repo root and never beside the
  // real records in databases/real/.
  const scripts = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).scripts;
  const board = scripts['test-board'];
  assert.match(board, /BOARD_DB=databases\/test\/board-3001\.db/,
    'the test board must keep its board in databases/test/');
  assert.match(board, /ASSISTANT_DB=databases\/test\/assistant-3001\.db/,
    'the test board must keep its chat record in databases/test/');
});

// This is an end-to-end test: a copy of the real server.js booting without
// BOARD_DB proves the wiring — the default path is databases/real/board.db and
// the boot migration actually runs and preserves the cards.
test('server boot migrates a legacy board.db and still serves its cards', async () => {
  // Seed a genuine legacy DB by running the real server against it once.
  const tmpRoot = mkdtempSync(join(tmpdir(), 'dbloc-e2e-'));
  const legacy = join(tmpRoot, 'board.db');
  const seed = await startServer({ env: { BOARD_DB: legacy } });
  try {
    const put = await fetch(seed.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [
        { id: 'c1', columnId: 'inbox', title: 'Survives the move' },
      ] }),
    });
    assert.equal(put.status, 200);
  } finally { await seed.stop(); }
  assert.ok(existsSync(legacy), 'seeding produced a legacy board.db');

  // A copy of the server in the temp root, so its ROOT is the temp root and
  // the real repo's files are never in reach. server.js has zero npm
  // dependencies, so the copy is self-sufficient.
  copyFileSync(join(ROOT, 'server.js'), join(tmpRoot, 'server.js'));
  cpSync(join(ROOT, 'scripts'), join(tmpRoot, 'scripts'), { recursive: true });

  // PORT=0: the kernel picks a free one and the server reports it, the same way
  // the shared harness does. A port derived from the clock collides with
  // whichever other suite happens to start alongside this one.
  const proc = spawn('node', ['server.js'], {
    cwd: tmpRoot,
    env: {
      ...process.env, PORT: '0', NODE_NO_WARNINGS: '1',
      LODESTAR_BACKUP_ON_WRITE: '0', ...safeEnv(tmpRoot),
      BOARD_DB: '', // empty means unset here: the default path must apply
      ASSISTANT_DB: '', // same — the default real/ path must apply
      // This is the one test in the repo that deliberately wants the default
      // resolution, so it must be immune to whatever the developer exported:
      // an inherited LODESTAR_REFUSE_REAL_DB would refuse this boot outright.
      LODESTAR_REFUSE_REAL_DB: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {});
  try {
    const [, bound] = await waitForLine(
      proc, /Lodestar running at http:\/\/localhost:(\d+)\b/);
    const port = Number(bound);

    assert.ok(existsSync(join(tmpRoot, 'databases', 'real', 'board.db')),
      'the board now lives in databases/real/');
    assert.ok(!existsSync(legacy), 'the legacy file was moved away');
    assert.ok(existsSync(join(tmpRoot, 'databases', 'real', 'assistant.db')),
      'the chat record is created beside the board, in databases/real/');

    const state = await (await fetch(`http://127.0.0.1:${port}/api/state`)).json();
    assert.deepEqual(state.cards.map((c) => c.title), ['Survives the move']);

    const backups = readdirSync(join(tmpRoot, 'test-backups'))
      .filter((f) => f.startsWith('board-') && f.endsWith('.db'));
    assert.equal(backups.length, 1, 'the boot migration backed up before moving');
  } finally {
    proc.kill('SIGKILL');
    try { rmSync(tmpRoot, { recursive: true, force: true }); } catch {}
  }
});

// This is a configuration invariant.
test('.gitignore keeps every Chroma store out of the repository', () => {
  // *.db already ignores the SQLite files; these lines are what keep the Chroma
  // stores out, whose files are not *.db and would otherwise be committed.
  //
  // It used to be one bare `databases/` line. It is two now because the folder
  // stopped being uniformly private: the :3001 sandbox's boards ship with the
  // repo so a checkout gets a working test board, while real/ never leaves this
  // machine and the test store is 94 MB of derived files a re-index rebuilds.
  // Asserting each store by name rather than the folder is the point — the risk
  // was never "the folder is unignored", it was "a store gets committed".
  const lines = readFileSync(join(ROOT, '.gitignore'), 'utf8').split('\n').map((l) => l.trim());
  assert.ok(lines.includes('databases/real/'),
    '.gitignore must ignore databases/real/ — real board, assistant and Chroma data');
  assert.ok(lines.includes('databases/test/chroma-data-3001/'),
    '.gitignore must ignore the test Chroma store: derived, and 94 MB of it');
});
