// tests/backup.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readdirSync, chmodSync, readFileSync } from 'node:fs';
  import { DatabaseSync } from 'node:sqlite';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runBackup } from '../scripts/backup-db.mjs';

function makeStubRclone(dir, { failCopy = false } = {}) {
  const logPath = join(dir, 'rclone.log');
  const bin = join(dir, 'rclone');
  // A tiny shell stub: `version` exits 0; `copy` logs args and exits 0 or 1.
  writeFileSync(bin, `#!/bin/sh
if [ "$1" = "version" ]; then echo "rclone v-stub"; exit 0; fi
echo "$@" >> "${logPath}"
exit ${failCopy ? 1 : 0}
`);
  chmodSync(bin, 0o755);
  return { bin, logPath };
}

// This is an integration test: real files on disk and a stub rclone binary.
test('no DB → status no-db, exits without throwing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const r = runBackup({ dbPath: join(dir, 'missing.db'), backupsDir: join(dir, 'backups') });
  assert.equal(r.status, 'no-db');
  assert.equal(r.pushed, false);
  // Same answer when the databases/ folder is missing or holds no .db at all.
  const empty = runBackup({ databasesDir: join(dir, 'no-such-databases'),
                            backupsDir: join(dir, 'backups') });
  assert.equal(empty.status, 'no-db');
  assert.equal(empty.pushed, false);
});

// This is an integration test: real files on disk and a stub rclone binary.
test('a databases/ run snapshots every real .db, skipping chroma-data and test data', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  // The sweep reads databases/real/ — the home of the records worth a Drive
  // snapshot. databases/test/ is the :3001 sandbox: disposable by definition,
  // and backing it up would push throwaway boards to the same Drive folder.
  const databasesDir = join(dir, 'databases', 'real');
  const backupsDir = join(dir, 'backups');
  const missingRclone = join(dir, 'no-such-rclone');
  mkdirSync(databasesDir, { recursive: true });
  writeFileSync(join(databasesDir, 'board.db'), 'BOARD');

  // Only board.db exists yet: one snapshot, no invented assistant one.
  const first = runBackup({ databasesDir, backupsDir, rcloneBin: missingRclone,
                            now: new Date('2026-08-02T10:00:00Z') });
  assert.equal(first.localPaths.length, 1);
  let files = readdirSync(backupsDir);
  assert.equal(files.filter((f) => f.startsWith('board-')).length, 1);
  assert.ok(!files.some((f) => f.startsWith('assistant-')));

  // assistant.db and chroma-data/ appear beside it, and the test stack's
  // databases land in the sibling test/ folder: both real .db files are
  // snapshotted; chroma-data is derived bulk and test data is disposable —
  // neither is ever backed up.
  writeFileSync(join(databasesDir, 'assistant.db'), 'ASSISTANT');
  mkdirSync(join(databasesDir, 'chroma-data'), { recursive: true });
  writeFileSync(join(databasesDir, 'chroma-data', 'chroma.sqlite3'), 'CHROMA');
  mkdirSync(join(dir, 'databases', 'test'), { recursive: true });
  writeFileSync(join(dir, 'databases', 'test', 'board-3001.db'), 'TESTBOARD');
  const both = runBackup({ databasesDir, backupsDir, rcloneBin: missingRclone,
                           now: new Date('2026-08-02T10:01:00Z') });
  assert.equal(both.localPaths.length, 2);
  files = readdirSync(backupsDir);
  assert.equal(readFileSync(join(backupsDir, files.find((f) => f.startsWith('assistant-'))), 'utf8'),
    'ASSISTANT');
  assert.ok(!files.some((f) => f.includes('chroma')), 'chroma-data is never backed up');
  assert.ok(!files.some((f) => f.includes('3001')), 'test databases are never backed up');

  // Prune applies per database name: board's snapshots must not be able to
  // evict assistant's (lexically, assistant-* sorts before every board-*).
  // keepDays: 0 switches the age floor off — this test and the two other prune
  // tests pin the count ceiling, which is only observable in isolation: with
  // the 90-day default every snapshot here is younger than the floor and
  // nothing prunes at all. The floor itself has its own test.
  for (let i = 2; i <= 5; i++) {
    runBackup({ databasesDir, backupsDir, rcloneBin: missingRclone, keep: 3,
                keepDays: 0, now: new Date(`2026-08-02T10:0${i}:00Z`) });
  }
  files = readdirSync(backupsDir).filter((f) => f.endsWith('.db'));
  assert.equal(files.filter((f) => f.startsWith('board-')).length, 3);
  assert.equal(files.filter((f) => f.startsWith('assistant-')).length, 3);
});

// This is a configuration invariant.
test("the sweep's default folder is databases/real, where the records now live", () => {
  // After the real/test split, a default still reading databases/ finds no
  // .db at all and `npm run backup` reports "nothing to back up" forever —
  // the quietest possible way to lose the backup habit.
  const src = readFileSync(new URL('../scripts/backup-db.mjs', import.meta.url), 'utf8');
  assert.match(src, /join\(ROOT, 'databases', 'real'\)/,
    'runBackup must default its sweep to databases/real');
});

// This is an integration test: real files on disk and a stub rclone binary.
test('happy path copies locally and invokes rclone copy', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'DBDATA');
  const backupsDir = join(dir, 'backups');
  const { bin, logPath } = makeStubRclone(dir);
  const r = runBackup({ dbPath, backupsDir, remote: 'gdrive', rcloneBin: bin,
                        now: new Date('2026-07-24T10:00:00Z') });
  assert.equal(r.status, 'ok');
  assert.equal(r.pushed, true);
  assert.ok(existsSync(r.localPath));
  assert.equal(readFileSync(r.localPath, 'utf8'), 'DBDATA');
  const log = readFileSync(logPath, 'utf8');
  assert.match(log, /copy .*board-2026-07-24.*\.db gdrive:lodestar-backups\//);
});

// This is an integration test: real files on disk and a stub rclone binary.
test('rclone copy failure → status rclone-failed but local kept, no throw', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'DBDATA');
  const backupsDir = join(dir, 'backups');
  const { bin } = makeStubRclone(dir, { failCopy: true });
  const r = runBackup({ dbPath, backupsDir, rcloneBin: bin });
  assert.equal(r.status, 'rclone-failed');
  assert.equal(r.pushed, false);
  assert.ok(existsSync(r.localPath));
});

// This is an integration test: real files on disk and a stub rclone binary.
test('missing rclone binary → status rclone-missing, local kept', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'DBDATA');
  const r = runBackup({ dbPath, backupsDir: join(dir, 'backups'),
                        rcloneBin: join(dir, 'no-such-rclone') });
  assert.equal(r.status, 'rclone-missing');
  assert.ok(existsSync(r.localPath));
});

// This is an integration test: real files on disk and a stub rclone binary.
test('keep defaults to 100 backups', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'DBDATA');
  const backupsDir = join(dir, 'backups');
  const missingRclone = join(dir, 'no-such-rclone');
  // 101 snapshots with distinct timestamps, no explicit keep.
  for (let i = 0; i < 101; i++) {
    const mm = String(Math.floor(i / 60)).padStart(2, '0');
    const ss = String(i % 60).padStart(2, '0');
    runBackup({ dbPath, backupsDir, rcloneBin: missingRclone, keepDays: 0,
                now: new Date(`2026-07-28T10:${mm}:${ss}Z`) });
  }
  const files = readdirSync(backupsDir).filter((f) => f.endsWith('.db'));
  assert.equal(files.length, 100, 'the default retention is 100 files, not 30');
  // The oldest (second 00) is the one evicted.
  assert.ok(!files.some((f) => f.includes('10-00-00')));
});

// This is an integration test: real files on disk and a stub rclone binary.
test('a real SQLite DB is snapshotted consistently and stays readable', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  const db = new DatabaseSync(dbPath);
  db.exec('CREATE TABLE cards (id TEXT PRIMARY KEY, title TEXT)');
  db.exec("INSERT INTO cards VALUES ('a', 'First'), ('b', 'Second')");
  const r = runBackup({ dbPath, backupsDir: join(dir, 'backups'),
                        rcloneBin: join(dir, 'no-such-rclone') });
  assert.ok(existsSync(r.localPath));
  const snap = new DatabaseSync(r.localPath, { readOnly: true });
  assert.deepEqual(
    snap.prepare('SELECT id, title FROM cards ORDER BY id').all().map((x) => ({ ...x })),
    [{ id: 'a', title: 'First' }, { id: 'b', title: 'Second' }],
  );
});

// This is an integration test: real files on disk and a stub rclone binary.
test('a file that is not a SQLite DB still gets a byte-for-byte copy', () => {
  // The fallback path: whatever the snapshot mechanism, a non-SQLite board file
  // must never be silently skipped or truncated.
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'NOT-A-DATABASE');
  const r = runBackup({ dbPath, backupsDir: join(dir, 'backups'),
                        rcloneBin: join(dir, 'no-such-rclone') });
  assert.ok(existsSync(r.localPath));
  assert.equal(readFileSync(r.localPath, 'utf8'), 'NOT-A-DATABASE');
});

// This is an integration test: real files on disk and a stub rclone binary.
test('prune keeps only the newest N backups', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const dbPath = join(dir, 'board.db');
  writeFileSync(dbPath, 'DBDATA');
  const backupsDir = join(dir, 'backups');
  const { bin } = makeStubRclone(dir);
  // Run 5 backups with keep=3, distinct timestamps.
  for (let i = 0; i < 5; i++) {
    runBackup({ dbPath, backupsDir, rcloneBin: bin, keep: 3, keepDays: 0,
                now: new Date(`2026-07-24T10:0${i}:00Z`) });
  }
  const files = readdirSync(backupsDir).filter((f) => f.endsWith('.db'));
  assert.equal(files.length, 3);
  // The three newest (minutes 02,03,04) survive.
  assert.ok(files.every((f) => /10-0[234]/.test(f)));
});

// This is an integration test: real files on disk and a stub rclone binary.
test('a burst of snapshots cannot evict last month\'s', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-retention-'));
  const dbDir = mkdtempSync(join(tmpdir(), 'lodestar-retention-db-'));
  const dbPath = join(dbDir, 'board.db');
  new DatabaseSync(dbPath).exec('CREATE TABLE cards (id TEXT)');

  // One snapshot a day for 40 days, then 30 in a single day — the shape of an
  // agent session. With keep=20 and no age floor, every one of the 40 dies.
  const day = (n) => new Date(Date.UTC(2026, 5, 1 + n));
  for (let n = 0; n < 40; n += 1) {
    runBackup({ dbPath, backupsDir: dir, keep: 20, keepDays: 90, now: day(n),
                rcloneBin: '/nonexistent' });
  }
  for (let i = 0; i < 30; i += 1) {
    runBackup({ dbPath, backupsDir: dir, keep: 20, keepDays: 90,
                now: new Date(Date.UTC(2026, 6, 11, 0, 0, i)),
                rcloneBin: '/nonexistent' });
  }

  const kept = readdirSync(dir).filter((f) => f.startsWith('board-'));
  assert.equal(kept.length, 70, 'nothing inside the age floor may be pruned');
  assert.ok(kept.some((f) => f.includes('2026-06-01')),
    'the oldest day inside the floor survived a 30-snapshot burst');

  // And the floor is a floor, not an amnesty: past it, the count still rules.
  runBackup({ dbPath, backupsDir: dir, keep: 20, keepDays: 90,
              now: new Date(Date.UTC(2026, 11, 31)), rcloneBin: '/nonexistent' });
  const after = readdirSync(dir).filter((f) => f.startsWith('board-'));
  assert.equal(after.length, 20, 'everything past the floor prunes to keep');
});
