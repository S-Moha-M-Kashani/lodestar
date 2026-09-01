// tests/dbbackend.test.js — which store server.js opens.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chooseBackend, BACKENDS } from '../db/backend.mjs';
import { authEnv } from './helpers/server-harness.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// This is a unit test.
test('the default is sqlite, and it is chosen by absence not by fallback', () => {
  assert.equal(chooseBackend({}), 'sqlite');
  assert.equal(chooseBackend({ LODESTAR_DB_BACKEND: '' }), 'sqlite');
});

// This is a unit test.
test('each named backend is returned verbatim', () => {
  for (const name of BACKENDS) {
    // postgres additionally requires LODESTAR_PG_URL (covered on its own
    // below); supply it here so this loop is only testing the name.
    const env = { LODESTAR_DB_BACKEND: name };
    if (name === 'postgres') env.LODESTAR_PG_URL = 'postgresql://x/y';
    assert.equal(chooseBackend(env), name);
  }
});

// This is a unit test.
test('an unknown backend raises, and never falls back', () => {
  // The project's rule for every seam: no `auto` mode, and a typo must stop
  // the boot rather than silently open the wrong store. `postgress` opening
  // SQLite is how someone spends a week wondering where their writes went.
  assert.throws(() => chooseBackend({ LODESTAR_DB_BACKEND: 'postgress' }),
    /postgress/, 'the message must quote what was actually set');
  assert.throws(() => chooseBackend({ LODESTAR_DB_BACKEND: 'postgress' }),
    /sqlite/, 'the message must list the backends that do exist');
  // Matched on the ECHOED input, not on the word "auto": every message this
  // function throws ends "There is deliberately no auto mode", so a bare /auto/
  // passed for any input at all and asserted nothing about this one.
  assert.throws(() => chooseBackend({ LODESTAR_DB_BACKEND: 'auto' }), /is "auto"/,
    'the message must quote the input, which is what makes it a diagnosis');
});

// This is a unit test.
test('postgres requires a connection string, and says so at boot', () => {
  // Failing here beats failing on the first query: the second happens after
  // the server has told the user it is running.
  assert.throws(() => chooseBackend({ LODESTAR_DB_BACKEND: 'postgres' }),
    /LODESTAR_PG_URL/);
  assert.equal(
    chooseBackend({ LODESTAR_DB_BACKEND: 'postgres', LODESTAR_PG_URL: 'postgresql://x/y' }),
    'postgres');
});

// This is an integration test: it boots the real server.js.
test('server.js asks the seam at boot, and refuses a backend it cannot open',
  { timeout: 20_000 }, () => {
    // The seam had exactly one caller — this file — so setting the variable did
    // nothing at all. What must be true now: `postgres` is recognised AND
    // refused (there is no Postgres store yet), the refusal says so, and the
    // default boot names the store it opened.
    const dir = mkdtempSync(join(tmpdir(), 'lodestar-backend-'));
    // authEnv: the server refuses to boot without a password verifier now, so
    // every spawn in this file would otherwise fail for a reason that has
    // nothing to do with the storage seam it is about.
    const base = { ...process.env, PORT: '0', LODESTAR_BACKUP_ON_WRITE: '0',
      ...authEnv(),
      BOARD_DB: join(dir, 'board.db'), ASSISTANT_DB: join(dir, 'assistant.db') };
    const boot = (env, timeout = 15_000) => spawnSync('node', ['server.js'],
      { cwd: ROOT, encoding: 'utf8', timeout, env: { ...base, ...env } });

    const refused = boot({ LODESTAR_DB_BACKEND: 'postgres',
      LODESTAR_PG_URL: 'postgresql://x/y' });
    assert.notEqual(refused.status, 0, 'the server booted on a store it has not got');
    assert.match(refused.stderr, /not wired up yet/);

    const typo = boot({ LODESTAR_DB_BACKEND: 'postgress' });
    assert.notEqual(typo.status, 0, 'a typo booted the server on SQLite in silence');
    assert.match(typo.stderr, /postgress/);

    // sqlite proceeds, and says which store it opened. Killed by the spawn's
    // own timeout, since a server that boots never exits on its own.
    const ok = boot({ LODESTAR_DB_BACKEND: 'sqlite' }, 3000);
    assert.match(ok.stdout, /backend: sqlite/);
    rmSync(dir, { recursive: true, force: true });
  });
