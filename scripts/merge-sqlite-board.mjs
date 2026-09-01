// scripts/merge-sqlite-board.mjs — rescue a stranded board file.
//
// One-time, for 2026-08-30: the composed container kept its board on a private
// Docker volume while `npm start` opened databases/real/board.db, so rows
// accumulated in a copy nothing will read once the compose file is fixed.
//
// Additive only. It inserts rows the destination lacks and touches nothing it
// already has — no updates, no deletes. Two boards with the same name are two
// boards, and deciding which is "the real one" is the user's call, not this
// script's. Safe to run twice; the second run adds nothing.
import { DatabaseSync } from 'node:sqlite';

// Copy rows whose key is absent from the destination. Columns are read from
// the source at runtime rather than listed here: the two files can be at
// different migration levels, and only the columns both have can be carried.
function copyMissing(src, dst, table, keyCols) {
  const cols = src.prepare(`PRAGMA table_info(${table})`).all().map((c) => c.name);
  const dstCols = new Set(dst.prepare(`PRAGMA table_info(${table})`).all().map((c) => c.name));
  const shared = cols.filter((c) => dstCols.has(c));
  const where = keyCols.map((k) => `${k} = ?`).join(' AND ');
  const exists = dst.prepare(`SELECT 1 AS x FROM ${table} WHERE ${where}`);
  const insert = dst.prepare(
    `INSERT INTO ${table} (${shared.join(', ')}) ` +
    `VALUES (${shared.map(() => '?').join(', ')})`);
  let added = 0;
  for (const row of src.prepare(`SELECT * FROM ${table}`).all()) {
    if (exists.get(...keyCols.map((k) => row[k]))) continue;
    insert.run(...shared.map((c) => row[c]));
    added += 1;
  }
  return added;
}

/** Merge `from` into `into`, additively. Returns the counts actually added. */
export function mergeSqliteBoard({ from, into }) {
  const src = new DatabaseSync(from, { readOnly: true });
  try {
    // Opening dst nested inside src's try/finally: if it throws (destination
    // locked, missing directory, corrupt file), src still gets closed.
    // `timeout` lets a blocked write wait instead of failing outright — this
    // script can run against the live board while a server holds it open.
    const dst = new DatabaseSync(into, { timeout: 5000 });
    try {
      // `BEGIN IMMEDIATE`, not a bare `BEGIN` (see server.js:275): a deferred
      // transaction only takes its write lock on the first write, by which
      // point it has already read, and SQLite refuses to make it WAIT there —
      // it returns SQLITE_BUSY at once and the timeout above never gets a say.
      dst.exec('BEGIN IMMEDIATE');
      // Boards first: a card carries a board_id, and inserting the parent first
      // keeps the foreign key satisfiable where one is enforced.
      const boards = copyMissing(src, dst, 'boards', ['id']);
      const cards = copyMissing(src, dst, 'cards', ['id']);
      const categories = copyMissing(src, dst, 'categories', ['board_id', 'id']);
      dst.exec('COMMIT');
      return { boards, cards, categories };
    } catch (err) {
      // The rollback can itself throw — most often because the transaction is
      // already gone — and that error would then hide the one that explains the
      // failure. The original is what propagates.
      try { dst.exec('ROLLBACK'); } catch { /* keep `err` */ }
      throw err;
    } finally {
      dst.close();
    }
  } finally {
    src.close();
  }
}

// Run directly: node scripts/merge-sqlite-board.mjs <from.db> <into.db>
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop())) {
  const [from, into] = process.argv.slice(2);
  if (!from || !into) {
    console.error('usage: node scripts/merge-sqlite-board.mjs <from.db> <into.db>');
    process.exit(1);
  }
  console.log(mergeSqliteBoard({ from, into }));
}
