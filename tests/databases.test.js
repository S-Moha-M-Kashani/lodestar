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

  const port = 21000 + Math.floor(Date.now() % 30000);
  const proc = spawn('node', ['server.js'], {
    cwd: tmpRoot,
    env: {
      ...process.env, PORT: String(port), NODE_NO_WARNINGS: '1',
      LODESTAR_BACKUP_ON_WRITE: '0', ...safeEnv(tmpRoot),
      BOARD_DB: '', // empty means unset here: the default path must apply
      ASSISTANT_DB: '', // same — the default real/ path must apply
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {});
  try {
    await waitForLine(proc, new RegExp(`Lodestar running at http://localhost:${port}\\b`));

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

// This is a configuration invariant.
test("the RAG lab's experiment ledger lives in databases/test/", () => {
  // The lab now writes a row per finished experiment into raglab.db. Where a
  // .db goes in this repo is a settled question and the answer matters here:
  // databases/real/ is the only folder `npm run backup` walks, so a lab ledger
  // placed there would push a person's own board out of the newest-100 window
  // with runs that are reproducible from the fixtures. databases/test/ is
  // disposable by definition, which is exactly what an experiment log is —
  // and being there means the backup script needs no exception, a rule that
  // cannot be forgotten rather than one that has to be remembered.
  const ledger = readFileSync(join(ROOT, 'brain/tests/raglab/ledger.py'), 'utf8');
  assert.match(ledger, /'databases'\s*\/\s*'test'\s*\/\s*'raglab\.db'/,
    "the ledger's default path must be databases/test/raglab.db");
  assert.ok(!/databases['"\s/]*\/?\s*['"]real/.test(ledger),
    'the lab must never write into databases/real/');
  // *.db is already ignored globally, so nothing here can be committed by
  // accident — and unlike the two :3001 boards, this one gets no `!` exception.
  const lines = readFileSync(join(ROOT, '.gitignore'), 'utf8').split('\n').map((l) => l.trim());
  assert.ok(lines.includes('*.db') && !lines.some((l) => l.includes('!databases/test/raglab.db')),
    'the experiment ledger is derived and machine-specific: it must stay ignored');
});
