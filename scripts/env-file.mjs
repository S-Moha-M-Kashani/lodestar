// scripts/env-file.mjs — the board's own reading of .env.
//
// Until this existed, nothing read .env for the board at all. `npm start` is
// `node server.js`, so a verifier pasted into .env reached the process only if
// the operator had also exported it — and the obvious way to do that,
// `set -a; . ./.env; set +a`, destroys the value on the way: a scrypt verifier
// is `scrypt$1$16384$8$1$…` and the shell expands every `$1` in it to nothing.
// The file was, in practice, unreadable by the one program it was written for,
// and the failure looked like a typo.
//
// Two rules, both deliberately narrow:
//
//   1. Only LODESTAR_* keys travel. .env is a shared file — it also carries
//      OPENROUTER_API_KEY and LANGSMITH_API_KEY, which belong to the brain.
//      docker compose hands the board container seven variables and none of
//      the brain's secrets; a loader that read the whole file would quietly
//      undo that, and "the LLM key lives only in the brain's env" would stop
//      being true of a native run as well.
//   2. It fills in; it does not override. A value already in the environment
//      wins, so a container's own settings and a one-off
//      `LODESTAR_DEV_KEY=… npm start` are still the last word. "Already in the
//      environment" excludes blank on purpose: `${VAR:-}` in
//      docker-compose.yml passes an empty string when the host has nothing,
//      and reading that as configured would leave the container refusing to
//      boot over a .env sitting in its own mounted tree.
//
// The password is the one setting with two spellings, and it is filled in as a
// GROUP for that reason: if the environment names either spelling, neither is
// taken from the file. Per-key filling looked right and was not — the test
// harness exports the hash, this repo's own .env carries the plaintext, and
// every spawned server then booted with both named and refused. Two rules were
// colliding, each correct alone: "the file fills the gaps" and "two
// credentials is an operator error". A layer, not a key, is what the second
// rule is really about.
//
// Alternatives considered
// -----------------------
//
// **`process.loadEnvFile()` / `node --env-file`.** Node's own loader is
// otherwise exactly this, and it was the first choice. Three things rule it
// out, each of them one of the rules above: it cannot filter by prefix, so the
// board would hold the brain's keys; it treats a variable set to the empty
// string as set, so compose's `${VAR:-}` would shadow the file it is meant to
// fall back to; and it throws ENOENT when the file is absent, which is the
// normal case in CI and in a container. `node:util`'s parseEnv is the same
// parser without the policy, which leaves the policy here where it can be read.
//
// **dotenv.** A dependency for a file format, in a backend that has had one npm
// package in its life. parseEnv is in the standard library and handles the two
// things that actually matter here — comments, and quotes around a value
// carrying a `$`.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseEnv } from 'node:util';

// Every variable the board is configured with shares this prefix, which is
// what makes a prefix sufficient rather than a hand-kept list of names that
// the next variable would silently fall off.
const BOARD_PREFIX = 'LODESTAR_';

// The two spellings of the one credential; see the note above.
const CREDENTIAL_KEYS = ['LODESTAR_AUTH_PASSWORD', 'LODESTAR_AUTH_PASSWORD_HASH'];

/** True when `env` really holds `key` — blank counts as unset. */
const held = (env, key) =>
  typeof env[key] === 'string' && env[key].trim() !== '';

/** The LODESTAR_* pairs in one .env's text, values verbatim. */
export function boardEnvFrom(text) {
  const out = {};
  for (const [key, value] of Object.entries(parseEnv(text))) {
    if (key.startsWith(BOARD_PREFIX)) out[key] = value;
  }
  return out;
}

/** Fill `env` in from `file` for every key `env` does not really hold, and
 *  treat the two credential spellings as one key for that test. Mutates and
 *  returns `env`, which is `process.env` at boot. */
export function fillMissing(env, file) {
  const credentialGiven = CREDENTIAL_KEYS.some((key) => held(env, key));
  for (const [key, value] of Object.entries(file)) {
    if (credentialGiven && CREDENTIAL_KEYS.includes(key)) continue;
    if (held(env, key)) continue;
    env[key] = value;
  }
  return env;
}

/** Read `<root>/.env` and fill the board's own settings in from it. A missing
 *  or unreadable file is not an error: a container is configured by compose and
 *  CI by its runner, and neither has one to read. */
export function applyEnvFile({ root, env = process.env, file = '.env' } = {}) {
  let text;
  try {
    text = readFileSync(join(root, file), 'utf8');
  } catch {
    return env;
  }
  return fillMissing(env, boardEnvFrom(text));
}
