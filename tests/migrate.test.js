// tests/migrate.test.js — the one-time SQLite → Postgres data move.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BOARD_DB = join(ROOT, 'databases', 'real', 'board.db');
const ASSISTANT_DB = join(ROOT, 'databases', 'real', 'assistant.db');

const PG_URL = process.env.LODESTAR_PG_URL;
const container = process.env.LODESTAR_PG_CONTAINER || 'postgres';

// The connection string never appears in an argument vector: `docker exec -e
// NAME` with no value passes the variable through from this process's own
// environment, so `ps` on this machine sees the container name and a shell
// snippet, never the password. Same reason the psql output is scrubbed below.
const inContainer = (script, stdin) => spawnSync('docker',
  ['exec', '-i', '-e', 'LODESTAR_PG_URL', '-e', 'LODESTAR_SQL', container,
    'sh', '-c', script],
  { encoding: 'utf8', input: stdin, env: process.env });

const psql = (sql) => spawnSync('docker',
  ['exec', '-i', '-e', 'LODESTAR_PG_URL', '-e', 'LODESTAR_SQL', container,
    'sh', '-c', 'psql "$LODESTAR_PG_URL" -At -c "$LODESTAR_SQL"'],
  { encoding: 'utf8', env: { ...process.env, LODESTAR_SQL: sql } });

/** psql echoes its connection info into some errors. Anything URL-shaped is
 *  removed before a failure message is printed, or the assertion that reports
 *  a broken migration is also the line that leaks the password. */
const redact = (text) => (text || '').replace(/\w+:\/\/\S+/g, '<redacted-url>');

const count = (file, table) => {
  const db = new DatabaseSync(file, { readOnly: true });
  try { return db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n; }
  finally { db.close(); }
};

const pgCount = (qualified) =>
  Number(psql(`SELECT count(*) FROM ${qualified}`).stdout.trim());

const unreachable = () => !PG_URL || psql('SELECT 1').status !== 0;

// This is an integration test: it writes to a real Postgres.
test('every live row arrives, keeps its fields, and re-running adds no duplicates',
  { timeout: 120_000 }, async (t) => {
    if (unreachable()) {
      t.skip('no Postgres — set LODESTAR_PG_URL and run `npm run postgres:schema`');
      return;
    }
    if (!existsSync(BOARD_DB) || !existsSync(ASSISTANT_DB)) {
      t.skip('no databases/real records on this machine to migrate');
      return;
    }
    // Imported here rather than at module scope: this file also imports `pg`,
    // which is the project's only npm dependency, and `node --test tests/*.test.js`
    // is the documented no-ceremony entry point. A top-level import makes the
    // WHOLE suite unrunnable on a fresh clone; a lazy one behind the skip makes
    // this one test unrunnable, which is what a skip is for.
    // And skipped rather than failed when the driver is simply absent: with the
    // import lazy, a missing `pg` is a missing tool, not a broken migration.
    let migrate;
    try {
      ({ migrate } = await import('../scripts/sqlite-to-postgres.mjs'));
    } catch (err) {
      t.skip(`the pg driver is not installed (${err.code}) — run \`npm ci\``);
      return;
    }

    // Work in a scratch schema pair so the test never writes over real rows.
    // The migration takes its target schemas as arguments for exactly this.
    psql('DROP SCHEMA IF EXISTS board_t CASCADE; DROP SCHEMA IF EXISTS assistant_t CASCADE');
    try {
      // The rewrite happens in JS, not in a shell `sed`, so the result can be
      // CHECKED before it is applied: this DDL is aimed at a real server that
      // also holds the user's migrated board, and one unrenamed `board.cards`
      // in it would be a production DDL statement issued by a test.
      const ddl = readFileSync(join(ROOT, 'scripts', 'postgres', '001-schema.sql'), 'utf8')
        .replaceAll('SCHEMA IF NOT EXISTS board;', 'SCHEMA IF NOT EXISTS board_t;')
        .replaceAll('SCHEMA IF NOT EXISTS assistant;', 'SCHEMA IF NOT EXISTS assistant_t;')
        .replaceAll('board.', 'board_t.')
        .replaceAll('assistant.', 'assistant_t.');
      // Both spellings a schema is named in: `board.cards` and the bare
      // `CREATE SCHEMA … board;`. The second is the one that got away — the
      // rewrite this replaced only handled the dotted form, so the DDL created
      // the REAL schemas and then failed on tables in a scratch one that did
      // not exist.
      assert.doesNotMatch(ddl, /(?<![_\w])(board|assistant)\s*[.;]/,
        'the scratch DDL still names a real schema');
      const applied = inContainer('psql "$LODESTAR_PG_URL" -v ON_ERROR_STOP=1 -f -', ddl);
      assert.equal(applied.status, 0, redact(applied.stderr));

      const args = {
        boardDb: BOARD_DB,
        assistantDb: ASSISTANT_DB,
        pgUrl: PG_URL,
        schemas: { board: 'board_t', assistant: 'assistant_t' },
      };
      const counts = await migrate(args);
      assert.ok(counts['board_t.cards'] > 0, 'no cards were migrated at all');

      // Compared against SQLITE, table by table — not against the migration's
      // own report, which agrees with itself by construction. A copyTable that
      // silently skipped half the rows has to fail here.
      for (const [file, table, schema] of [
        [BOARD_DB, 'boards', 'board_t'], [BOARD_DB, 'cards', 'board_t'],
        [BOARD_DB, 'card_edits', 'board_t'], [BOARD_DB, 'categories', 'board_t'],
        [ASSISTANT_DB, 'sessions', 'assistant_t'],
        [ASSISTANT_DB, 'messages', 'assistant_t']]) {
        assert.equal(pgCount(`${schema}.${table}`), count(file, table),
          `${schema}.${table} holds a different number of rows than SQLite`);
      }

      // The three fields whose loss would be silent: board_id decides which
      // board a card appears on, num is the permanent ledger label printed on
      // it, and habit_history is a record the user cannot reconstruct. Checked
      // on a row this test migrated itself, against the SQLite it came from —
      // a habit card when the source has one, since that is where habit_history
      // is worth reading, and any card when it does not.
      const source = new DatabaseSync(BOARD_DB, { readOnly: true });
      let card;
      try {
        card = source.prepare(
          "SELECT id, board_id, num, habit_history FROM cards WHERE habit_freq <> '' LIMIT 1").get()
          ?? source.prepare('SELECT id, board_id, num, habit_history FROM cards LIMIT 1').get();
      } finally { source.close(); }
      const moved = psql("SELECT board_id || '|' || num || '|' || habit_history " +
        `FROM board_t.cards WHERE id = '${card.id}'`).stdout.trim();
      assert.equal(moved, `${card.board_id}|${card.num}|${card.habit_history}`,
        'a migrated card lost its board, its ledger number or its habit history');

      // Idempotent: the second run must add nothing, because a migration that
      // doubles the board when someone runs it twice is worse than no migration.
      const again = await migrate(args);
      assert.equal(again['board_t.cards'], 0, 're-running inserted cards again');
      assert.equal(pgCount('board_t.cards'), count(BOARD_DB, 'cards'));
    } finally {
      // In a finally, so a failed assertion leaves no scratch schemas behind
      // on a server this repo does not own.
      psql('DROP SCHEMA IF EXISTS board_t CASCADE; DROP SCHEMA IF EXISTS assistant_t CASCADE');
    }
  });
