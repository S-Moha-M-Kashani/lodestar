// tests/mergedb.test.js — the one-time rescue of a stranded board file.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mergeSqliteBoard } from '../scripts/merge-sqlite-board.mjs';

const SCHEMA = `
  CREATE TABLE boards (id TEXT PRIMARY KEY, name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, deleted_at INTEGER);
  CREATE TABLE cards (id TEXT PRIMARY KEY, board_id TEXT NOT NULL DEFAULT 'main',
    column_id TEXT NOT NULL, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    num INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER, pending INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE categories (board_id TEXT NOT NULL, id TEXT NOT NULL,
    label TEXT NOT NULL, h INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board_id, id));
`;

function make(path, rows) {
  const db = new DatabaseSync(path);
  db.exec(SCHEMA);
  for (const [sql, ...args] of rows) db.prepare(sql).run(...args);
  db.close();
}

const board = (id, name) =>
  ['INSERT INTO boards (id, name, created_at, updated_at) VALUES (?, ?, 1, 1)', id, name];
const card = (id, boardId, title) =>
  ['INSERT INTO cards (id, board_id, column_id, title, created_at, updated_at) ' +
   'VALUES (?, ?, \'inbox\', ?, 1, 1)', id, boardId, title];

// This is a unit test: two temporary SQLite files, no server.
test('a stranded board and its cards are added, and nothing is overwritten', () => {
  const dir = mkdtempSync(join(tmpdir(), 'mergedb-'));
  const from = join(dir, 'stranded.db');
  const into = join(dir, 'real.db');
  make(from, [board('main', 'Lodestar'), board('b-only-there', 'Moha-Pari'),
    card('c-shared', 'main', 'stranded title'), card('c-only-there', 'b-only-there', 'rescued')]);
  make(into, [board('main', 'Lodestar'), board('b-only-here', 'Pari'),
    card('c-shared', 'main', 'real title'), card('c-only-here', 'b-only-here', 'kept')]);

  const added = mergeSqliteBoard({ from, into });
  assert.deepEqual(added, { boards: 1, cards: 1, categories: 0 });

  const db = new DatabaseSync(into, { readOnly: true });
  assert.deepEqual(
    db.prepare('SELECT id FROM boards ORDER BY id').all().map((r) => r.id),
    ['b-only-here', 'b-only-there', 'main'],
    'both same-named boards survive — they are different boards');
  assert.equal(
    db.prepare('SELECT title FROM cards WHERE id = ?').get('c-shared').title,
    'real title',
    'a card present in both keeps the destination copy: the merge never overwrites');
  assert.ok(db.prepare('SELECT 1 AS x FROM cards WHERE id = ?').get('c-only-there'),
    'the stranded card was not rescued');
  db.close();
});

// This is a unit test.
test('a card whose board is missing is still rescued, never dropped', () => {
  // Its board row could have been purged. Losing the card because its parent
  // is gone would be the merge doing the damage it exists to prevent.
  const dir = mkdtempSync(join(tmpdir(), 'mergedb-'));
  const from = join(dir, 'stranded.db');
  const into = join(dir, 'real.db');
  make(from, [card('c-orphan', 'b-vanished', 'orphan')]);
  make(into, [board('main', 'Lodestar')]);
  assert.deepEqual(mergeSqliteBoard({ from, into }), { boards: 0, cards: 1, categories: 0 });
  const db = new DatabaseSync(into, { readOnly: true });
  assert.equal(db.prepare('SELECT board_id FROM cards WHERE id = ?').get('c-orphan').board_id,
    'b-vanished', 'the card kept its board id rather than being re-homed silently');
  db.close();
});

// This is a unit test.
test('running it twice adds nothing the second time', () => {
  const dir = mkdtempSync(join(tmpdir(), 'mergedb-'));
  const from = join(dir, 'stranded.db');
  const into = join(dir, 'real.db');
  make(from, [board('b-x', 'X'), card('c-x', 'b-x', 'x')]);
  make(into, [board('main', 'Lodestar')]);
  assert.deepEqual(mergeSqliteBoard({ from, into }), { boards: 1, cards: 1, categories: 0 });
  assert.deepEqual(mergeSqliteBoard({ from, into }), { boards: 0, cards: 0, categories: 0 });
});
