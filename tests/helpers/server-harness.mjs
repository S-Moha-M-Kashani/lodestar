// tests/helpers/server-harness.mjs
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { randomBytes } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hashPassword } from '../../auth/local-auth.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

// --------------------------------------------------------------------------
// The test credential
// --------------------------------------------------------------------------

// Random, per test process, and hashed once — scrypt is deliberately slow and
// there are ~60 startServer calls in this suite. Random rather than a constant
// so no plaintext password of any kind is ever written into a tracked file,
// and so nothing can grow a habit of relying on knowing it.
export const TEST_PASSWORD = randomBytes(18).toString('base64url');
const TEST_HASH = hashPassword(TEST_PASSWORD);

// A test-only service token, for the routes that check one. Same reasoning.
export const TEST_SERVICE_TOKEN = randomBytes(32).toString('base64url');

// --------------------------------------------------------------------------
// One authenticated fetch, installed once
// --------------------------------------------------------------------------

// Every server the suite starts is now behind a login, and there are about a
// hundred bare `fetch(s.base + '/api/…')` calls across six test files that
// were written before there was one. Rather than thread a cookie through all
// of them — churn in tests that have nothing to do with authentication, and a
// hundred chances to forget — the cookie is attached here, in the one place
// that already knows which servers this suite started.
//
// The scope is the whole point: the wrapper only ever adds a header to an
// origin THIS harness launched and is still running, and it holds no other
// power. A request to any other host — a real service, another port, anything
// a future test dials — passes through completely untouched. Nothing about the
// server's behaviour is softened: the gate is fully on, and the tests that
// probe it (tests/server.test.js) use `rawFetch` below to arrive with no
// credential at all, exactly like a stranger.
export const rawFetch = globalThis.fetch;
const cookies = new Map(); // origin -> Cookie header value

function authenticatedFetch(input, init = {}) {
  const target = typeof input === 'string' ? input : (input?.url ?? '');
  let cookie;
  try { cookie = cookies.get(new URL(target).origin); } catch { cookie = undefined; }
  if (!cookie) return rawFetch(input, init);
  const headers = new Headers(init.headers || (typeof input === 'object' ? input.headers : undefined));
  if (!headers.has('cookie')) headers.set('cookie', cookie);
  return rawFetch(input, { ...init, headers });
}
globalThis.fetch = authenticatedFetch;

/** Log in to a server this suite started and remember the session for it, so
 *  the plain `fetch` calls in the tests are authenticated from here on. Also
 *  used by the two tests that spawn `server.js` themselves rather than through
 *  startServer. */
export async function authorize(base, password = TEST_PASSWORD) {
  const res = await rawFetch(base + '/api/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) throw new Error(`test login failed: ${res.status}`);
  const setCookie = res.headers.get('set-cookie') || '';
  const value = setCookie.split(';')[0];
  if (!value) throw new Error('test login returned no session cookie');
  cookies.set(new URL(base).origin, value);
  return value;
}

/** Forget a server's session — ports are reused, so a stale entry would hand
 *  the next server on that port a cookie it never issued. */
export function forget(base) {
  try { cookies.delete(new URL(base).origin); } catch { /* not a url */ }
}

/** The environment every spawned server needs to boot now that authentication
 *  is required. Exported because two tests spawn `server.js` directly. */
export function authEnv(extra = {}) {
  return { LODESTAR_AUTH_PASSWORD_HASH: TEST_HASH, ...extra };
}

function waitForLine(proc, regex, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for ${regex}; saw:\n${buf}`));
    }, timeoutMs);
    function onData(chunk) {
      buf += chunk.toString();
      const match = buf.match(regex);
      if (match) { cleanup(); resolve(match); }
    }
    function onExit(code) {
      cleanup();
      reject(new Error(`server exited early (code ${code}); saw:\n${buf}`));
    }
    function cleanup() {
      clearTimeout(timer);
      proc.stdout.off('data', onData);
      proc.off('exit', onExit);
    }
    proc.stdout.on('data', onData);
    proc.on('exit', onExit);
  });
}

export async function startServer({ env = {}, login = true } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-srv-'));
  const dbPath = join(dir, 'board.db');
  // Both databases point into the temp dir: without ASSISTANT_DB a spawned
  // test server would write chat rows into the repo's real databases/.
  const assistantDbPath = join(dir, 'assistant.db');
  // PORT=0 asks the kernel for a free port and the server reports the one it
  // got. Every test file used to derive a port from the clock instead, so two
  // suites starting in the same millisecond-ish window picked the same number
  // and one of them died at bind — a flake that moved to a different test on
  // every run and had nothing to do with the test it failed.
  const proc = spawn('node', ['server.js'], {
    cwd: ROOT,
    // Write-triggered backups are OFF by default here: most tests create cards,
    // and each one would otherwise drop a snapshot of a temp board into the
    // user's real backups/ and evict a genuine one. The backup tests opt in
    // explicitly via `env`, pointing at a temp directory.
    env: { ...process.env, PORT: '0', BOARD_DB: dbPath,
           ASSISTANT_DB: assistantDbPath, NODE_NO_WARNINGS: '1',
           LODESTAR_BACKUP_ON_WRITE: '0', ...authEnv(), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {}); // drain
  const [, bound] = await waitForLine(proc, /Lodestar running at http:\/\/localhost:(\d+)\b/);
  const port = Number(bound);
  const base = `http://127.0.0.1:${port}`;
  // `login: false` is for the tests that want to see the closed door.
  if (login) await authorize(base);
  const stop = async () => {
    forget(base);
    proc.kill('SIGKILL');
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
  };
  return { port, dbPath, assistantDbPath, dir, base, proc, stop };
}

export { waitForLine };
