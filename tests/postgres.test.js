// tests/postgres.test.js — the Postgres mirror of the two SQLite records.
//
// Why there is a Postgres at all: on 2026-08-30 a board created on the native
// server was invisible under Docker and vice versa, because `npm start` opens
// databases/real/board.db while the composed container opens /data/board.db on
// a Docker-owned volume. Two databases, one URL, and the browser's cached copy
// refilled the cards on whichever one was empty, so only the *boards* looked
// lost. A server both stacks dial over TCP has one copy of the rows by
// construction.
//
// Why the server is NOT in this repo's compose file: a database that lives in
// an application's compose project shares that project's lifecycle, and
// `docker compose down -v` here would then destroy it — with several projects
// on one server, one careless command in one repo takes out all of them. That
// is the failure above with higher stakes. The server is its own small compose
// project holding one database per project; Lodestar owns only its schema and
// its connection.
//
// So what this repo can still pin is the schema — and it is derived from
// server.js rather than listed by hand, because a mirror allowed to fall behind
// the original is a migration that loses a column on the day it runs. Nothing
// in the backend talks to Postgres yet; these tests describe the target.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (...p) => readFileSync(join(ROOT, ...p), 'utf8');

// The schema lives outside databases/, which .gitignore excludes wholesale and
// tests/databases.test.js asserts is empty of tracked files. DDL is source, not
// data: it has to be in the repository a clone gets.
const SCHEMA_FILE = 'scripts/postgres/001-schema.sql';

// ---------------------------------------------------------------------------
// The schema, derived from the two SQLite databases server.js creates.
// ---------------------------------------------------------------------------

// Every table server.js creates, and every column it can end up with — the
// CREATE TABLE blocks plus the ALTER TABLE migrations that run at boot. Read
// out of the source rather than out of a live .db file: a developer's database
// is one machine's migration history, and this has to fail on the day a column
// is added, not on the day someone happens to run the server.
function sqliteSchema() {
  const src = read('server.js');
  const tables = new Map();
  const add = (t, c) => {
    if (!tables.has(t)) tables.set(t, new Set());
    tables.get(t).add(c);
  };
  for (const m of src.matchAll(
    /CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\(([\s\S]*?)\n\s*\);/g)) {
    const [, name, body] = m;
    for (const line of body.split('\n')) {
      const col = line.trim().match(/^(\w+)\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC)\b/i);
      if (col) add(name, col[1]);
    }
  }
  for (const m of src.matchAll(/ALTER TABLE (\w+) ADD COLUMN (\w+)/g)) {
    add(m[1], m[2]);
  }
  assert.ok(tables.size > 0, 'read no tables at all out of server.js');
  return tables;
}

// The same, for the Postgres DDL: table name -> { schema, cols }.
function postgresSchema() {
  const src = read(SCHEMA_FILE);
  const tables = new Map();
  const CONSTRAINT = /^(primary|foreign|unique|check|constraint|like|exclude)$/i;
  for (const m of src.matchAll(
    /CREATE TABLE (?:IF NOT EXISTS )?(?:(\w+)\.)?(\w+)\s*\(([\s\S]*?)\n\);/g)) {
    const [, schema, name, body] = m;
    const cols = new Set();
    for (const line of body.split('\n')) {
      const col = line.trim().match(/^(\w+)\s+\S/);
      if (col && !CONSTRAINT.test(col[1])) cols.add(col[1]);
    }
    tables.set(name, { schema, cols });
  }
  assert.ok(tables.size > 0, `read no tables at all out of ${SCHEMA_FILE}`);
  return tables;
}

// This is a configuration invariant.
test('the Postgres schema mirrors every SQLite table, column for column', () => {
  assert.ok(existsSync(join(ROOT, SCHEMA_FILE)),
    `${SCHEMA_FILE} does not exist — there is nothing to apply to the server`);
  const sqlite = sqliteSchema();
  const pg = postgresSchema();
  for (const [table, cols] of sqlite) {
    const mirrored = pg.get(table);
    assert.ok(mirrored,
      `server.js creates "${table}" but ${SCHEMA_FILE} does not — a mirror ` +
        `missing a table is a migration that silently drops it`);
    assert.deepEqual(
      [...mirrored.cols].sort(),
      [...cols].sort(),
      `"${table}" has different columns in Postgres than in SQLite`,
    );
  }
});

// This is a configuration invariant.
test('the two SQLite files stay two Postgres schemas', () => {
  // board.db and assistant.db are separate files, which is exactly why
  // sessions.board_id carries no foreign key: SQLite cannot reference across
  // one. Collapsing both into a single namespace would quietly make that
  // constraint expressible and the two records one thing; keeping the boundary
  // as a schema keeps the decision a decision.
  const pg = postgresSchema();
  const home = (t) => pg.get(t)?.schema;
  for (const t of ['boards', 'cards', 'card_edits', 'categories']) {
    assert.equal(home(t), 'board', `${t} belongs to the board schema`);
  }
  for (const t of ['sessions', 'messages']) {
    assert.equal(home(t), 'assistant', `${t} belongs to the assistant schema`);
  }
});

// This is a configuration invariant.
test('the schema mirrors no table that belongs to something else', () => {
  const pg = postgresSchema();
  // sqlite_sequence is SQLite's own bookkeeping for AUTOINCREMENT. It shows up
  // in sqlite_master, so a mirror generated from a live database rather than
  // from server.js picks it up — and Postgres identity columns need nothing of
  // the sort.
  assert.ok(!pg.has('sqlite_sequence'),
    'sqlite_sequence is SQLite internals, not a table this project owns');
  // brain-checkpoints.db is LangGraph's, created and migrated by its own
  // saver. Hand-writing those tables here would pin a private schema this
  // project does not control and cannot keep current.
  for (const t of ['checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'writes']) {
    assert.ok(!pg.has(t),
      `"${t}" belongs to LangGraph's checkpointer, which owns and migrates ` +
        `its own schema — PostgresSaver.setup() creates it, not this file`);
  }
});

// This is a configuration invariant.
test('the schema can be applied more than once', () => {
  // The server is not ours, so its image's run-once init directory is not the
  // delivery mechanism: this file is applied by hand, against a database that
  // may already hold some of it. Every statement therefore has to be safe to
  // repeat, or the second run aborts halfway and leaves the schema part-built.
  const src = read(SCHEMA_FILE);
  for (const m of src.matchAll(/^\s*CREATE\s+(?!OR REPLACE\b)(\w+)\s+(?!IF NOT EXISTS\b)(\S+)/gim)) {
    assert.fail(
      `"CREATE ${m[1]} ${m[2]}" is not repeatable — write it as ` +
        `CREATE ${m[1].toUpperCase()} IF NOT EXISTS (or CREATE OR REPLACE), ` +
        `because this file is applied by hand and will be applied twice`);
  }
});

// This is a configuration invariant.
test('this repo does not own the database server', () => {
  // The inverse of a normal service test, and the whole point of the split: a
  // postgres service here would put the shared server back under Lodestar's
  // lifecycle, where `docker compose down -v` reaches it — and with several
  // projects on one server that command stops being survivable.
  const compose = read('docker-compose.yml');
  assert.ok(!/\n {2}postgres:\n/.test(compose),
    'docker-compose.yml defines a postgres service — the shared server lives ' +
      'in its own compose project so that no application\'s teardown can ' +
      'destroy another application\'s rows');
  assert.ok(!/\n {2}postgres-data:/.test(compose),
    'the database volume is declared here, so `docker compose down -v` in ' +
      'this repo would destroy it');
});

// This is a configuration invariant.
test('applying the schema names no credentials', () => {
  // The connection string carries a password. It belongs in the environment on
  // the machine, never in a file the repository publishes — and never spelled
  // out a second time in package.json, which is the copy that leaks.
  const script = JSON.parse(read('package.json')).scripts['postgres:schema'];
  assert.ok(script, 'package.json has no "postgres:schema" script');
  assert.ok(script.includes(SCHEMA_FILE),
    `the script does not apply ${SCHEMA_FILE}`);
  assert.match(script, /\$\{?LODESTAR_PG_URL/,
    'the script must take its connection from LODESTAR_PG_URL, not a literal');
  assert.ok(!/:\/\/[^\s'"]*:[^\s'"@]+@/.test(script),
    'the script contains an inline user:password — that is a committed secret');
});

// ---------------------------------------------------------------------------
// The claims that need the real server. Both skip when it is not reachable.
// ---------------------------------------------------------------------------

// No driver — and the reason is no longer "this project has zero npm
// dependencies", because it now has one: `pg`, added for the migration script.
// The decision stands on its own ground. What these two tests check is the
// SCHEMA and the server's independence from it, so they need a client, not a
// binding; shelling out to psql keeps them runnable with no install step at
// all, which is what makes them work on a fresh clone and in CI where the
// driver may not be there yet. psql on the host is used when present, otherwise
// the psql inside the server's own container — so neither a host install nor
// this repo owning the service is required. No default password: an unreachable
// server skips.
const PG_URL = process.env.LODESTAR_PG_URL
  || 'postgresql://lodestar@localhost:5432/lodestar';
const PG_CONTAINER = process.env.LODESTAR_PG_CONTAINER || 'postgres';

// The connection string never appears in an argument vector, on either path:
// `sh -c` reads it out of the environment (`docker exec -e NAME` with no value
// forwards this process's own env into the container), so `ps` never sees it
// — same reason as tests/migrate.test.js's `inContainer`/`psql`. Small local
// copy rather than a shared import: two call sites do not earn an abstraction.
const PG_ENV = { ...process.env, LODESTAR_PG_URL: PG_URL };

function query(sql) {
  const onPath = spawnSync('psql', ['--version'], { encoding: 'utf8' }).status === 0;
  const env = { ...PG_ENV, LODESTAR_SQL: sql };
  return onPath
    ? spawnSync('sh', ['-c', 'psql "$LODESTAR_PG_URL" -At -c "$LODESTAR_SQL"'],
      { encoding: 'utf8', env })
    : spawnSync('docker',
      ['exec', '-i', '-e', 'LODESTAR_PG_URL', '-e', 'LODESTAR_SQL', PG_CONTAINER,
        'sh', '-c', 'psql "$LODESTAR_PG_URL" -At -c "$LODESTAR_SQL"'],
      { encoding: 'utf8', env });
}

// psql echoes its connection info into some errors — same reason
// tests/migrate.test.js redacts before printing a failure message.
const redact = (text) => (text || '').replace(/\w+:\/\/\S+/g, '<redacted-url>');

const unreachable = () => query('SELECT 1').status !== 0;

// This is an integration test: it drives the real server.
test('every mirrored table exists in the running database', { timeout: 60_000 }, (t) => {
  if (unreachable()) {
    t.skip(`no Postgres at ${redact(PG_URL)} — start the shared server and run ` +
      '`npm run postgres:schema`');
    return;
  }
  const listed = query(
    "SELECT table_schema || '.' || table_name FROM information_schema.tables " +
    "WHERE table_schema IN ('board', 'assistant') ORDER BY 1");
  assert.equal(listed.status, 0, `could not list tables: ${redact(listed.stderr)}`);
  const live = new Set(listed.stdout.trim().split('\n'));
  for (const [table, { schema }] of postgresSchema()) {
    assert.ok(live.has(`${schema}.${table}`),
      `${schema}.${table} is in ${SCHEMA_FILE} but not in the running ` +
        `database — the schema is applied by hand, so an edit here does not ` +
        `reach the server until \`npm run postgres:schema\` runs again`);
  }
});

// This is an integration test: it replaces the real server's container.
//
// Opt-in, and deliberately so. It is the proof of the requirement this whole
// service exists for — rows that outlive a redeploy — but the server belongs to
// another compose project, and a test suite here must not recreate another
// project's container by surprise. Point LODESTAR_PG_COMPOSE at that project's
// compose file to run it.
test('a row written before a redeploy is still there after it', { timeout: 180_000 }, (t) => {
  const composeFile = process.env.LODESTAR_PG_COMPOSE;
  if (!composeFile) {
    t.skip('set LODESTAR_PG_COMPOSE to the shared server\'s compose file to run this');
    return;
  }
  if (unreachable()) {
    t.skip(`no Postgres at ${redact(PG_URL)}`);
    return;
  }
  const id = 'redeploy-probe';
  query(`DELETE FROM board.boards WHERE id = '${id}'`);
  const wrote = query(
    `INSERT INTO board.boards (id, name, position, created_at, updated_at)
     VALUES ('${id}', 'probe', 0, 1, 1)`);
  assert.equal(wrote.status, 0, `could not write: ${redact(wrote.stderr)}`);

  // Not `restart`: replacing the container is what a redeploy actually does,
  // and it is the step that proves the rows are on a volume that outlives it.
  const up = spawnSync('docker',
    ['compose', '-f', composeFile, 'up', '-d', '--force-recreate', 'postgres'],
    { encoding: 'utf8' });
  assert.equal(up.status, 0, `could not recreate the container: ${redact(up.stderr)}`);
  for (let i = 0; i < 90 && unreachable(); i++) spawnSync('sleep', ['1']);

  const back = query(`SELECT name FROM board.boards WHERE id = '${id}'`);
  assert.equal(back.stdout.trim(), 'probe',
    'the row did not survive a container replacement — the data directory is ' +
      'not on a volume that outlives it');
  query(`DELETE FROM board.boards WHERE id = '${id}'`);
});
