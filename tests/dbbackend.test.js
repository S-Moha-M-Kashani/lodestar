// tests/dbbackend.test.js — which store server.js opens.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chooseBackend, BACKENDS } from '../db/backend.mjs';

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
  assert.throws(() => chooseBackend({ LODESTAR_DB_BACKEND: 'auto' }), /auto/);
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
