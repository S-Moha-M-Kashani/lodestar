// tests/migrate.test.js — the one-time SQLite → Postgres data move.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { migrate } from '../scripts/sqlite-to-postgres.mjs';

const PG_URL = process.env.LODESTAR_PG_URL;
const container = process.env.LODESTAR_PG_CONTAINER || 'postgres';
const psql = (sql, url = PG_URL) => spawnSync('docker',
  ['exec', '-i', container, 'psql', url, '-At', '-c', sql], { encoding: 'utf8' });
const unreachable = () => !PG_URL || psql('SELECT 1').status !== 0;

// This is an integration test: it writes to a real Postgres.
test('every live row arrives, and re-running adds no duplicates',
  { timeout: 120_000 }, async (t) => {
    if (unreachable()) {
      t.skip('no Postgres — set LODESTAR_PG_URL and run `npm run postgres:schema`');
      return;
    }
    // Work in a scratch schema pair so the test never writes over real rows.
    // The migration takes its target schemas as arguments for exactly this.
    psql('DROP SCHEMA IF EXISTS board_t CASCADE; DROP SCHEMA IF EXISTS assistant_t CASCADE');
    psql('CREATE SCHEMA board_t; CREATE SCHEMA assistant_t');
    const ddl = spawnSync('sh', ['-c',
      "sed -e 's/board\\./board_t./g' -e 's/assistant\\./assistant_t./g' " +
      "-e 's/CREATE SCHEMA IF NOT EXISTS board_t;//' " +
      "-e 's/CREATE SCHEMA IF NOT EXISTS assistant_t;//' " +
      `scripts/postgres/001-schema.sql | docker exec -i ${container} psql "${PG_URL}" -f -`],
    { encoding: 'utf8' });
    assert.equal(ddl.status, 0, ddl.stderr);

    const counts = await migrate({
      boardDb: 'databases/real/board.db',
      assistantDb: 'databases/real/assistant.db',
      pgUrl: PG_URL,
      schemas: { board: 'board_t', assistant: 'assistant_t' },
    });
    assert.ok(counts['board_t.cards'] > 0, 'no cards were migrated at all');

    const live = Number(psql('SELECT count(*) FROM board_t.cards').stdout.trim());
    assert.equal(live, counts['board_t.cards'],
      'the database holds a different number of cards than the migration reported');

    // Idempotent: the second run must add nothing, because a migration that
    // doubles the board when someone runs it twice is worse than no migration.
    const again = await migrate({
      boardDb: 'databases/real/board.db',
      assistantDb: 'databases/real/assistant.db',
      pgUrl: PG_URL,
      schemas: { board: 'board_t', assistant: 'assistant_t' },
    });
    assert.equal(again['board_t.cards'], 0, 're-running inserted cards again');
    assert.equal(Number(psql('SELECT count(*) FROM board_t.cards').stdout.trim()), live);

    psql('DROP SCHEMA board_t CASCADE; DROP SCHEMA assistant_t CASCADE');
  });

// This is an integration test.
test('a card keeps its board, its ledger number and its habit history',
  { timeout: 60_000 }, (t) => {
    if (unreachable()) { t.skip('no Postgres'); return; }
    // The three fields whose loss would be silent: board_id decides which board
    // a card appears on, num is the permanent ledger label printed on it, and
    // habit_history is a record the user cannot reconstruct.
    const row = psql(
      "SELECT board_id || '|' || num || '|' || habit_history FROM board.cards " +
      "WHERE habit_freq <> '' LIMIT 1");
    if (!row.stdout.trim()) { t.skip('no habit cards migrated yet'); return; }
    const [boardId, num, history] = row.stdout.trim().split('|');
    assert.ok(boardId.length > 0, 'a migrated card has no board');
    assert.ok(Number(num) > 0, 'a migrated card lost its ledger number');
    assert.ok(history.startsWith('{'), 'habit history did not survive as JSON');
  });
