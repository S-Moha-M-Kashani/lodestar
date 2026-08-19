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
  // Snapshots live in the db/ subfolder now; nothing lands flat in backups/.
  const dbDir = join(backupsDir, 'db');
  let files = readdirSync(dbDir);
  assert.equal(files.filter((f) => f.startsWith('board-')).length, 1);
  assert.ok(!files.some((f) => f.startsWith('assistant-')));
  assert.ok(!readdirSync(backupsDir).some((f) => f.endsWith('.db')),
    'no .db file may sit flat in backupsDir any more');

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
  // The stubs are plain text, not SQLite: snapshotted, but never json-exported.
  assert.deepEqual(both.jsonPaths, []);
  files = readdirSync(dbDir);
  assert.equal(readFileSync(join(dbDir, files.find((f) => f.startsWith('assistant-'))), 'utf8'),
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
  files = readdirSync(dbDir).filter((f) => f.endsWith('.db'));
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
  assert.match(r.localPath, /[\\/]db[\\/]board-2026-07-24/, 'snapshot lands under backups/db/');
  assert.equal(readFileSync(r.localPath, 'utf8'), 'DBDATA');
  const log = readFileSync(logPath, 'utf8');
  assert.match(log, /copy .*board-2026-07-24.*\.db gdrive:lodestar-backups\/db\//);
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
  // jsonPaths is always an array, even when no export was possible.
  assert.deepEqual(r.jsonPaths, []);
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
  const files = readdirSync(join(backupsDir, 'db')).filter((f) => f.endsWith('.db'));
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
  const files = readdirSync(join(backupsDir, 'db')).filter((f) => f.endsWith('.db'));
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

  const kept = readdirSync(join(dir, 'db')).filter((f) => f.startsWith('board-'));
  assert.equal(kept.length, 70, 'nothing inside the age floor may be pruned');
  assert.ok(kept.some((f) => f.includes('2026-06-01')),
    'the oldest day inside the floor survived a 30-snapshot burst');

  // And the floor is a floor, not an amnesty: past it, the count still rules.
  runBackup({ dbPath, backupsDir: dir, keep: 20, keepDays: 90,
              now: new Date(Date.UTC(2026, 11, 31)), rcloneBin: '/nonexistent' });
  const after = readdirSync(join(dir, 'db')).filter((f) => f.startsWith('board-'));
  assert.equal(after.length, 20, 'everything past the floor prunes to keep');
});

// This is an integration test: a real SQLite board on disk and a stub rclone binary.
test('a board DB exports one Import-JSON file per live board under json/', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bk-'));
  const databasesDir = join(dir, 'databases', 'real');
  mkdirSync(databasesDir, { recursive: true });

  // A minimal but real board.db on the current server schema: a boards table
  // with two live boards and a soft-deleted one, and cards spread across all
  // three (live / soft-deleted / pending). Each LIVE board must get its own
  // file holding exactly its own live cards — a merged export imports one
  // board's cards into another, and a deleted board's cards into anything.
  const db = new DatabaseSync(join(databasesDir, 'board.db'));
  db.exec(`CREATE TABLE boards (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER)`);
  const bins = db.prepare('INSERT INTO boards VALUES (?, ?, ?, ?, ?, ?)');
  bins.run('main', 'Lodestar', 0, 1000, 1000, null);
  bins.run('b-two', 'Second board', 1, 1000, 1000, null);
  bins.run('b-gone', 'Deleted board', 2, 1000, 1000, 5000);
  db.exec(`CREATE TABLE cards (
    id TEXT PRIMARY KEY, board_id TEXT NOT NULL DEFAULT 'main',
    column_id TEXT NOT NULL, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'medium',
    type TEXT NOT NULL DEFAULT 'question', category TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL DEFAULT '',
    num INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER,
    effort TEXT NOT NULL DEFAULT 'medium', control TEXT NOT NULL DEFAULT 'influence',
    effort_src TEXT NOT NULL DEFAULT 'default', control_src TEXT NOT NULL DEFAULT 'default',
    deadline TEXT NOT NULL DEFAULT '', pending INTEGER NOT NULL DEFAULT 0,
    habit_freq TEXT NOT NULL DEFAULT '', habit_count INTEGER NOT NULL DEFAULT 1,
    habit_times TEXT NOT NULL DEFAULT '[]', habit_history TEXT NOT NULL DEFAULT '{}')`);
  db.exec('CREATE TABLE categories (id TEXT PRIMARY KEY, label TEXT, h INTEGER, position INTEGER)');
  const ins = db.prepare(`INSERT INTO cards
    (id, board_id, column_id, title, category, tags, created_at, updated_at, num, deleted_at, pending)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  ins.run('live', 'main', 'inbox', 'Live card', 'work', '["work","urgent"]', 1000, 2000, 7, null, 0);
  ins.run('gone', 'main', 'inbox', 'Soft-deleted card', '', '[]', 1000, 2000, 8, 3000, 0);
  ins.run('maybe', 'main', 'inbox', 'Pending proposal', '', '[]', 1000, 2000, 0, null, 1);
  // b-two's only card is soft-deleted: an empty live board is still a valid
  // restore target and must get a file with cards: [].
  ins.run('two-gone', 'b-two', 'inbox', 'Deleted on board two', '', '[]', 1000, 2000, 9, 3000, 0);
  // A live card on a deleted board: deleting a board stamps the board row, not
  // its cards — this card must appear in no export at all.
  ins.run('ghost', 'b-gone', 'inbox', 'Live card on a deleted board', '', '[]', 1000, 2000, 10, null, 0);
  db.prepare('INSERT INTO categories VALUES (?, ?, ?, ?)').run('work', 'Work', 220, 0);
  db.close();
  // A real SQLite DB with no cards table (assistant.db's shape): snapshotted
  // under db/ like any record, but never json-exported and never a throw.
  const adb = new DatabaseSync(join(databasesDir, 'assistant.db'));
  adb.exec('CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)');
  adb.close();
  // A pre-multi-board file: cards table, no boards table, no board_id column.
  // It must keep today's single merged export, named <dbname>-<stamp>.json.
  const ldb = new DatabaseSync(join(databasesDir, 'legacy.db'));
  ldb.exec(`CREATE TABLE cards (id TEXT PRIMARY KEY, column_id TEXT, title TEXT,
    created_at INTEGER, updated_at INTEGER, deleted_at INTEGER)`);
  ldb.exec("INSERT INTO cards VALUES ('l1', 'inbox', 'Old live', 1000, 2000, NULL)");
  ldb.exec("INSERT INTO cards VALUES ('l2', 'inbox', 'Old gone', 1000, 2000, 3000)");
  ldb.close();

  const backupsDir = join(dir, 'backups');
  const { bin, logPath } = makeStubRclone(dir);
  const r = runBackup({ databasesDir, backupsDir, remote: 'gdrive', rcloneBin: bin,
                        now: new Date('2026-08-19T10:00:00Z') });
  assert.equal(r.status, 'ok');

  const jsonDir = join(backupsDir, 'json');
  const stamp = '2026-08-19T10-00-00-000Z';
  const jsonFiles = readdirSync(jsonDir).sort();
  // One file per LIVE board, named <dbname>-<boardId>-<stamp>.json (board ids
  // are stable, names are not) — no file for the deleted board, no merged
  // board-<stamp>.json any more, and none for a DB without a cards table.
  assert.deepEqual(jsonFiles, [
    `board-b-two-${stamp}.json`,
    `board-main-${stamp}.json`,
    `legacy-${stamp}.json`,
  ]);
  // jsonPaths reports exactly the exports made, alongside the db/ localPaths.
  assert.deepEqual([...r.jsonPaths].sort(), jsonFiles.map((f) => join(jsonDir, f)));
  assert.equal(r.localPaths.length, 3);

  // main's export is the board's Import JSON shape, keys camelCased, holding
  // exactly its own live cards — not b-two's, not the deleted board's.
  const data = JSON.parse(readFileSync(join(jsonDir, `board-main-${stamp}.json`), 'utf8'));
  assert.equal(data.version, 1);
  assert.equal(data.cards.length, 1, 'soft-deleted, pending and other-board rows never export');
  const card = data.cards[0];
  assert.equal(card.id, 'live');
  assert.equal(card.columnId, 'inbox');
  assert.equal(card.category, 'work');
  assert.deepEqual(card.tags, ['work', 'urgent'], 'tags is a parsed array, not a JSON string');
  assert.equal(card.createdAt, 1000);
  assert.equal(card.updatedAt, 2000);
  assert.equal(card.num, 7);
  assert.deepEqual(data.categories, [{ id: 'work', label: 'Work', h: 220 }]);

  // b-two is live but has no live cards: still a file, categories are global
  // (same in every file), cards empty.
  const two = JSON.parse(readFileSync(join(jsonDir, `board-b-two-${stamp}.json`), 'utf8'));
  assert.deepEqual(two, { version: 1, cards: [], categories: data.categories });

  // The deleted board's live card is in no export anywhere.
  for (const f of jsonFiles) {
    const ids = JSON.parse(readFileSync(join(jsonDir, f), 'utf8')).cards.map((c) => c.id);
    assert.ok(!ids.includes('ghost'), `deleted board's card leaked into ${f}`);
  }

  // The legacy DB keeps the single merged file: its live cards, no board split.
  const legacy = JSON.parse(readFileSync(join(jsonDir, `legacy-${stamp}.json`), 'utf8'));
  assert.deepEqual(legacy.cards.map((c) => c.id), ['l1']);

  // Every export rides to its own Drive folder, json/ beside db/.
  const log = readFileSync(logPath, 'utf8');
  assert.match(log, /copy .*board-main-2026-08-19.*\.json gdrive:lodestar-backups\/json\//);
  assert.match(log, /copy .*board-b-two-2026-08-19.*\.json gdrive:lodestar-backups\/json\//);

  // Prune inside json/ keys on the full per-board prefix (<dbname>-<boardId>-),
  // same two rules, so one board's burst can never evict another's: after four
  // runs with keep=2, EACH board series holds 2 files — a prune keyed on the
  // db name alone would leave 2 in total. keepDays: 0 so the count ceiling is
  // observable — see the note in the databases/ test. The hyphen in b-two also
  // pins that stampOf still parses a stamp behind a hyphenated board id.
  for (let i = 1; i <= 3; i++) {
    runBackup({ databasesDir, backupsDir, rcloneBin: bin, keep: 2, keepDays: 0,
                now: new Date(`2026-08-19T10:0${i}:00Z`) });
  }
  const after = readdirSync(jsonDir);
  assert.equal(after.filter((f) => f.startsWith('board-main-')).length, 2);
  assert.equal(after.filter((f) => f.startsWith('board-b-two-')).length, 2);
  assert.ok(after.filter((f) => f.startsWith('board-main-')).every((f) => /10-0[23]/.test(f)),
    'per-board prune keeps the newest of that board');
  assert.equal(after.filter((f) => f.startsWith('legacy-')).length, 2);
});
