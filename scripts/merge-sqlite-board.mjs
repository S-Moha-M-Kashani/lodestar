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
    const dst = new DatabaseSync(into);
    try {
      dst.exec('BEGIN');
      // Boards first: a card carries a board_id, and inserting the parent first
      // keeps the foreign key satisfiable where one is enforced.
      const boards = copyMissing(src, dst, 'boards', ['id']);
      const cards = copyMissing(src, dst, 'cards', ['id']);
      const categories = copyMissing(src, dst, 'categories', ['board_id', 'id']);
      dst.exec('COMMIT');
      return { boards, cards, categories };
    } catch (err) {
      dst.exec('ROLLBACK');
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
