// tests/databases.test.js — Stage 1 of Session 7: the databases/ folder.
//
// Contract under test: `resolveBoardDb` in scripts/db-location.mjs decides
// where the board database lives and performs the one-time move of a legacy
// root-level board.db into databases/. server.js calls it at boot to get
// DB_PATH.
//
//   resolveBoardDb({ root, env }) -> absolute path server.js should open
//
//   - env.BOARD_DB set        -> returned verbatim, nothing touched (Docker,
//                                the :3001 test board, and every test harness
//                                pass an explicit path and must never migrate).
//   - databases/board.db exists -> returned, nothing touched — the migration
//                                only runs when the target does not exist.
//   - legacy root/board.db exists -> backed up first (via runBackup, honouring
//                                LODESTAR_BACKUP_DIR / LODESTAR_RCLONE_BIN /
//                                LODESTAR_RCLONE_REMOTE / LODESTAR_BACKUP_KEEP
//                                from the given env), then moved to
//                                databases/board.db.
//   - neither exists          -> databases/board.db (fresh clone; server.js's
//                                existing mkdirSync creates the folder).

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
import { resolveBoardDb } from '../scripts/db-location.mjs';
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
test('legacy board.db is backed up, then moved into databases/', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  writeFileSync(join(root, 'board.db'), 'DBDATA');
  const env = safeEnv(root);

  const p = resolveBoardDb({ root, env });
  assert.equal(p, join(root, 'databases', 'board.db'));
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
test('an existing databases/board.db is never overwritten', () => {
  const root = mkdtempSync(join(tmpdir(), 'dbloc-'));
  mkdirSync(join(root, 'databases'));
  writeFileSync(join(root, 'databases', 'board.db'), 'CURRENT');
  writeFileSync(join(root, 'board.db'), 'STALE-LEGACY');
  const env = safeEnv(root);

  const p = resolveBoardDb({ root, env });
  assert.equal(p, join(root, 'databases', 'board.db'));
  assert.equal(readFileSync(p, 'utf8'), 'CURRENT', 'the target wins; migration must not re-run');
  assert.equal(readFileSync(join(root, 'board.db'), 'utf8'), 'STALE-LEGACY',
    'the stray legacy file is left for the user, not deleted');
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
    join(fresh, 'databases', 'board.db'));
  assert.ok(!existsSync(freshEnv.LODESTAR_BACKUP_DIR), 'fresh clone: no backup taken');
});

// This is an end-to-end test: a copy of the real server.js booting without
// BOARD_DB proves the wiring — the default path is databases/board.db and the
// boot migration actually runs and preserves the cards.
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

  const port = 21000 + Math.floor(Date.now() % 30000);
  const proc = spawn('node', ['server.js'], {
    cwd: tmpRoot,
    env: {
      ...process.env, PORT: String(port), NODE_NO_WARNINGS: '1',
      LODESTAR_BACKUP_ON_WRITE: '0', ...safeEnv(tmpRoot),
      BOARD_DB: '', // empty means unset here: the default path must apply
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {});
  try {
    await waitForLine(proc, new RegExp(`Lodestar running at http://localhost:${port}\\b`));

    assert.ok(existsSync(join(tmpRoot, 'databases', 'board.db')),
      'the board now lives in databases/');
    assert.ok(!existsSync(legacy), 'the legacy file was moved away');

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
test('.gitignore covers the databases/ folder', () => {
  // *.db already ignores the SQLite files; this line is what keeps Stage 3's
  // chroma-data/ (whose files are not *.db) out of the repository.
  const lines = readFileSync(join(ROOT, '.gitignore'), 'utf8').split('\n').map((l) => l.trim());
  assert.ok(lines.includes('databases/'), '.gitignore must ignore databases/');
});
