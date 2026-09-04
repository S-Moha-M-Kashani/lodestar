// tests/envfile.test.js — what .env is allowed to tell the board server.
//
// The board reads .env itself, so that changing the password is editing one
// line rather than running a script, pasting its output, and remembering to
// export it. Two rules decide what that file may do, and both are values, so
// both are tested here rather than through a spawned process:
//
//   1. only LODESTAR_* keys travel. .env is a shared file — it also holds
//      OPENROUTER_API_KEY and LANGSMITH_API_KEY — and the board is not
//      supposed to hold either. docker compose hands the board container
//      exactly seven variables today, and a loader that read the whole file
//      would quietly undo that.
//   2. it fills in, it does not override. A value already in the environment
//      wins, and "already in the environment" excludes the empty string,
//      because that is what `${VAR:-}` in docker-compose.yml passes when the
//      host has nothing.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boardEnvFrom, fillMissing } from '../scripts/env-file.mjs';

// This is a unit test.
test('only LODESTAR_ keys travel out of .env, with their values intact', () => {
  const file = [
    '# The password, quoted because the value carries a $.',
    "LODESTAR_AUTH_PASSWORD='open $1$ sesame'",
    'LODESTAR_SERVICE_TOKEN=abc123',
    'OPENROUTER_API_KEY=sk-must-not-travel',
    'LANGSMITH_API_KEY=ls-must-not-travel',
    'BRAIN_LLM=ollama',
    'PORT=9999',
    '',
  ].join('\n');

  const out = boardEnvFrom(file);
  assert.deepEqual(Object.keys(out).sort(),
    ['LODESTAR_AUTH_PASSWORD', 'LODESTAR_SERVICE_TOKEN']);
  // The `$` is a literal all the way through. It is the character scrypt's
  // own hash format is built from, and the reason these values are quoted:
  // the shell eats it when the file is sourced, and so does docker compose.
  assert.equal(out.LODESTAR_AUTH_PASSWORD, 'open $1$ sesame');
  // The brain's own secrets are not the board's business, whichever file they
  // happen to share.
  assert.ok(!JSON.stringify(out).includes('must-not-travel'));
});

// This is a unit test.
test('.env fills a variable in but never overrides a real one', () => {
  const file = { LODESTAR_AUTH_PASSWORD: 'from the file',
                 LODESTAR_DEV_KEY: 'from the file' };

  // Absent, and empty, are both "not set": compose passes `${VAR:-}` as an
  // empty string, and a loader that read that as configured would leave a
  // container with no credential while .env sat in its mounted tree.
  assert.equal(fillMissing({}, file).LODESTAR_AUTH_PASSWORD, 'from the file');
  assert.equal(fillMissing({ LODESTAR_AUTH_PASSWORD: '' }, file)
    .LODESTAR_AUTH_PASSWORD, 'from the file');
  assert.equal(fillMissing({ LODESTAR_AUTH_PASSWORD: '   ' }, file)
    .LODESTAR_AUTH_PASSWORD, 'from the file');

  // A real value wins, so a container's environment and a one-off
  // `LODESTAR_AUTH_PASSWORD=… node server.js` are both still the last word.
  assert.equal(fillMissing({ LODESTAR_AUTH_PASSWORD: 'from the environment' }, file)
    .LODESTAR_AUTH_PASSWORD, 'from the environment');

  // Filling in is done in place on the object it was handed, and touches
  // nothing else in it.
  const env = { PATH: '/usr/bin', LODESTAR_AUTH_PASSWORD: 'kept' };
  fillMissing(env, file);
  assert.equal(env.PATH, '/usr/bin');
  assert.equal(env.LODESTAR_AUTH_PASSWORD, 'kept');
  assert.equal(env.LODESTAR_DEV_KEY, 'from the file');
});

// This is a unit test. The password is one setting with two spellings, so the
// file is a fallback for the PAIR and not for each name in it. Filled per key,
// an environment naming the hash gets the file's plaintext filled in beside it
// and the server then refuses the boot for naming both — which is what every
// spawned server in the suite did the moment .env was read at all.
test('naming either spelling of the password ignores both in the file', () => {
  const file = { LODESTAR_AUTH_PASSWORD: 'from the file',
                 LODESTAR_AUTH_PASSWORD_HASH: 'scrypt$1$from$the$file$x$y',
                 LODESTAR_DEV_KEY: 'from the file' };

  const hashGiven = fillMissing({ LODESTAR_AUTH_PASSWORD_HASH: 'scrypt$1$real' }, file);
  assert.equal(hashGiven.LODESTAR_AUTH_PASSWORD_HASH, 'scrypt$1$real');
  assert.equal(hashGiven.LODESTAR_AUTH_PASSWORD, undefined,
    'a plaintext filled in beside a given hash is a refused boot');

  const plainGiven = fillMissing({ LODESTAR_AUTH_PASSWORD: 'from the environment' }, file);
  assert.equal(plainGiven.LODESTAR_AUTH_PASSWORD, 'from the environment');
  assert.equal(plainGiven.LODESTAR_AUTH_PASSWORD_HASH, undefined);

  // Only the credentials are grouped. Everything else in the file still fills
  // in normally alongside them.
  assert.equal(hashGiven.LODESTAR_DEV_KEY, 'from the file');

  // And with nothing given, the file supplies what it has — including, if it
  // really does name both, the pair that server.js is right to refuse. The
  // grouping is about layers, not about hiding an operator's own mistake.
  const nothingGiven = fillMissing({}, file);
  assert.equal(nothingGiven.LODESTAR_AUTH_PASSWORD, 'from the file');
  assert.equal(nothingGiven.LODESTAR_AUTH_PASSWORD_HASH, 'scrypt$1$from$the$file$x$y');
});
