// tests/boundary.test.js — the trust boundary as the network sees it.
//
// auth/local-auth.mjs is unit-tested in tests/auth.test.js; this file asserts
// that server.js actually asks it, in the right order, before it reads or
// writes anything. Every test here talks to a real spawned server over a real
// socket, because the property under test is where the check sits relative to
// the router — and that is invisible to a test that calls the router directly.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { connect } from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { networkInterfaces } from 'node:os';
import { copyFileSync, cpSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  startServer, rawFetch, authorize, TEST_PASSWORD, TEST_SERVICE_TOKEN, authEnv,
  waitForLine,
} from './helpers/server-harness.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** A request with headers exactly as given — including Host, which `fetch`
 *  will not let a caller set. Host forgery is the whole point of half of these
 *  tests, so they cannot go through fetch at all. */
function raw(port, { method = 'GET', path = '/', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, method, path, headers,
                          setHost: false }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { text += c; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, text }));
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

const jsonHeaders = (port, extra = {}) =>
  ({ host: `127.0.0.1:${port}`, 'content-type': 'application/json', ...extra });

const seed = (base, cards) => fetch(base + '/api/state', {
  method: 'PUT', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ version: 1, cards }),
});
const titles = async (base) =>
  (await (await fetch(base + '/api/state')).json()).cards.map((c) => c.title).sort();

// --------------------------------------------------------------------------
// 1. Host
// --------------------------------------------------------------------------

// This is an integration test. A page on any domain can point that domain at
// 127.0.0.1 and make the browser connect here — DNS rebinding — and the only
// thing it cannot forge is the Host header. So the allowlist is the defence,
// and it has to run before the router: a rejected alias must not be able to
// read a card, write one, or even learn that a route exists.
test('only a local Host reaches the router, and a rejected one changes nothing', async () => {
  const s = await startServer();
  try {
    await seed(s.base, [{ id: 'keep', columnId: 'inbox', title: 'Still here' }]);
    const cookie = (await rawFetch(s.base + '/api/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: TEST_PASSWORD }),
    })).headers.get('set-cookie').split(';')[0];

    for (const host of [`localhost:${s.port}`, `127.0.0.1:${s.port}`, `[::1]:${s.port}`]) {
      const res = await raw(s.port, { path: '/api/state', headers: { host, cookie } });
      assert.equal(res.status, 200, `an approved Host was refused: ${host}`);
    }

    for (const host of ['evil.example.com', `evil.example.com:${s.port}`,
                        `192.168.1.42:${s.port}`, `my-laptop.local:${s.port}`,
                        `lodestar:${s.port}`, 'localhost', `localhost:${s.port + 1}`]) {
      const read = await raw(s.port, { path: '/api/state', headers: { host, cookie } });
      assert.equal(read.status, 403, `an alias reached the router: ${host}`);
      assert.ok(!read.text.includes('Still here'),
        `a rejected Host was answered with board data: ${host}`);

      // And the same alias cannot mutate. This PUT omits every card, which on
      // an accepted request soft-deletes the lot — so if the check ran after
      // the router the board would be empty by the end of this loop.
      const write = await raw(s.port, {
        method: 'PUT', path: '/api/state', headers: { host, cookie, 'content-type': 'application/json' },
        body: JSON.stringify({ version: 1, cards: [] }),
      });
      assert.equal(write.status, 403, `an alias mutated the board: ${host}`);
    }

    assert.deepEqual(await titles(s.base), ['Still here'],
      'the board was changed by a request that never should have reached it');

    // The Host check runs before the session check too: a rejected alias is
    // told nothing about whether it would have been authorised.
    const noCookie = await raw(s.port, { path: '/api/state', headers: { host: 'evil.example.com' } });
    assert.equal(noCookie.status, 403);
  } finally { await s.stop(); }
});

// This is an end-to-end test: it asks the operating system where the listener
// actually is, which no assertion about a constant can do.
test('the listener is on loopback and on no other interface', async () => {
  const s = await startServer();
  try {
    // Loopback answers.
    await new Promise((resolve, reject) => {
      const sock = connect(s.port, '127.0.0.1', () => { sock.end(); resolve(); });
      sock.on('error', reject);
    });

    // Every non-loopback IPv4 this machine has must refuse. On a laptop that
    // is the Wi-Fi address a peer on the same network would dial; in a
    // container with no such address there is nothing to test and the test
    // says so rather than passing quietly.
    const lan = Object.values(networkInterfaces()).flat()
      .filter((n) => n && n.family === 'IPv4' && !n.internal).map((n) => n.address);
    if (lan.length === 0) {
      console.log('  (no non-loopback IPv4 on this machine — nothing to refuse)');
      return;
    }
    for (const address of lan) {
      const refused = await new Promise((resolve) => {
        const sock = connect({ port: s.port, host: address });
        const done = (v) => { sock.destroy(); resolve(v); };
        sock.setTimeout(2000, () => done(true)); // a firewalled drop is a refusal too
        sock.on('connect', () => done(false));
        sock.on('error', () => done(true));
      });
      assert.ok(refused,
        `the board answered on ${address}:${s.port} — that is the address a `
        + 'peer on the same Wi-Fi would use');
    }
  } finally { await s.stop(); }
});

// --------------------------------------------------------------------------
// 2. The login boundary
// --------------------------------------------------------------------------

// This is an integration test.
test('nothing private is served before login', async () => {
  const s = await startServer({ login: false });
  try {
    for (const path of ['/api/state', '/api/boards', '/api/trash', '/api/chat/messages',
                        '/api/chat/sessions', '/api/proposals', '/api/edits',
                        '/api/agent/chat', '/api/rag/chat/reindex',
                        '/styles.css', '/js/main.js']) {
      const res = await rawFetch(s.base + path);
      assert.equal(res.status, 401, `${path} answered before login`);
      const body = await res.text();
      assert.equal(body, '{"error":"Unauthorized"}',
        `${path} said more than "no" to a stranger`);
    }

    // The app shell sends a browser to the door rather than 401-ing at it.
    for (const path of ['/', '/index.html']) {
      const res = await rawFetch(s.base + path, { redirect: 'manual' });
      assert.equal(res.status, 302);
      assert.equal(res.headers.get('location'), '/login');
    }

    // The door itself, and the one liveness ping, are public — and the ping
    // says nothing but that the process is up.
    const page = await rawFetch(s.base + '/login');
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-type'), /text\/html/);
    const health = await rawFetch(s.base + '/api/health');
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { ok: true });

    // A mutation is refused before it can mutate: seed a card through an
    // authenticated session, then try to wipe the board without one.
    await authorize(s.base);
    await seed(s.base, [{ id: 'a', columnId: 'inbox', title: 'Private' }]);
    const wipe = await rawFetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [] }),
    });
    assert.equal(wipe.status, 401);
    assert.deepEqual(await titles(s.base), ['Private']);
  } finally { await s.stop(); }
});

// This is an integration test.
test('login issues a protected session, and logout ends it', async () => {
  const s = await startServer({ login: false });
  try {
    const attempt = (password) => rawFetch(s.base + '/api/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password }),
    });

    const wrong = await attempt('not the password');
    assert.equal(wrong.status, 401);
    assert.deepEqual(await wrong.json(), { error: 'Login failed' },
      'the failure must not say which part failed, or echo what was typed');
    assert.equal(wrong.headers.get('set-cookie'), null);

    const ok = await attempt(TEST_PASSWORD);
    assert.equal(ok.status, 200);
    const setCookie = ok.headers.get('set-cookie');
    for (const attr of ['HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=']) {
      assert.ok(setCookie.includes(attr), `the session cookie lacks ${attr}`);
    }
    const cookie = setCookie.split(';')[0];
    const token = cookie.split('=')[1];
    // The raw token exists in the Set-Cookie header and nowhere else — not in
    // the body, which is what a page's own JavaScript could read and leak.
    assert.equal(await ok.text(), '{"ok":true}');
    assert.ok(token.length >= 40);

    const authed = await rawFetch(s.base + '/api/state', { headers: { cookie } });
    assert.equal(authed.status, 200, 'the session did not open the board');

    // Logged in, /login stops being a page and becomes a redirect home.
    const door = await rawFetch(s.base + '/login', { headers: { cookie }, redirect: 'manual' });
    assert.equal(door.status, 302);
    assert.equal(door.headers.get('location'), '/');

    const out = await rawFetch(s.base + '/api/logout', { method: 'POST', headers: { cookie } });
    assert.equal(out.status, 200);
    assert.match(out.headers.get('set-cookie'), /Max-Age=0/);
    const after = await rawFetch(s.base + '/api/state', { headers: { cookie } });
    assert.equal(after.status, 401, 'a logged-out token still opened the board');

    // A token that never existed, and a session from a server that has since
    // restarted, are the same thing: sessions live in one process's memory.
    const other = await startServer({ login: false });
    try {
      const stale = await rawFetch(other.base + '/api/state', { headers: { cookie } });
      assert.equal(stale.status, 401, 'a session survived the process that issued it');
    } finally { await other.stop(); }
  } finally { await s.stop(); }
});

// This is an integration test.
test('a burst of wrong passwords is locked out; a live session is not', async () => {
  // A one-second lockout rather than the shipped minute: the behaviour under
  // test is "bounded and it ends", and a test that waits sixty seconds to see
  // the end is a test nobody runs.
  const s = await startServer({ env: { LODESTAR_LOGIN_LOCKOUT_MS: '1000' } });
  try {
    const attempt = (password) => rawFetch(s.base + '/api/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    let last;
    for (let i = 0; i < 5; i += 1) last = await attempt('wrong');
    assert.equal(last.status, 401, 'the fifth wrong password is still a wrong password');

    const locked = await attempt('wrong');
    assert.equal(locked.status, 429);
    assert.match(locked.headers.get('retry-after'), /^\d+$/);
    // Even the right password waits — otherwise the lockout is a hint that the
    // guess was close.
    assert.equal((await attempt(TEST_PASSWORD)).status, 429);
    // And junk that is not even JSON cannot probe the throttle for free.
    assert.equal((await rawFetch(s.base + '/api/login',
      { method: 'POST', body: 'not json' })).status, 429);

    // The owner, already logged in, is untouched by any of it.
    assert.equal((await fetch(s.base + '/api/state')).status, 200);

    await new Promise((r) => setTimeout(r, 1100));
    assert.equal((await attempt(TEST_PASSWORD)).status, 200, 'the lockout never ended');
  } finally { await s.stop(); }
});

// This is an integration test. The service token is how the brain reads cards
// and posts proposals without anybody putting the user's password in a second
// service's environment.
test('a service token authenticates a non-browser caller, and only the right one', async () => {
  const s = await startServer({ env: { LODESTAR_SERVICE_TOKEN: TEST_SERVICE_TOKEN } });
  try {
    const withToken = (token) => rawFetch(s.base + '/api/state',
      { headers: { authorization: `Bearer ${token}` } });
    assert.equal((await withToken(TEST_SERVICE_TOKEN)).status, 200);
    assert.equal((await withToken('x'.repeat(43))).status, 401);
    assert.equal((await withToken('')).status, 401);
  } finally { await s.stop(); }

  // With no token configured, Bearer authenticates nothing at all — an unset
  // secret must not be matchable by sending an empty one.
  const bare = await startServer({ login: false });
  try {
    const res = await rawFetch(bare.base + '/api/state',
      { headers: { authorization: 'Bearer ' } });
    assert.equal(res.status, 401);
  } finally { await bare.stop(); }
});

// This is an integration test.
test('the server refuses to boot without a password verifier', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-auth-'));
  // A copy of the server rather than the repo itself, and the reason is the
  // feature: server.js reads the .env beside it, so run in this repo it finds
  // the real password and boots — every case below would then assert the
  // opposite of what it saw. This directory deliberately has no .env at all,
  // which also pins that a missing one is not an error.
  copyFileSync(join(ROOT, 'server.js'), join(dir, 'server.js'));
  for (const sub of ['scripts', 'db', 'auth']) {
    cpSync(join(ROOT, sub), join(dir, sub), { recursive: true });
  }
  // Both credentials are blanked in the base environment, not just the one
  // each case is about, so a developer's own exported password cannot decide
  // this test either.
  const boot = (env, timeout = 15_000) => spawnSync('node', ['server.js'], {
    cwd: dir, encoding: 'utf8', timeout,
    env: { ...process.env, PORT: '0', LODESTAR_BACKUP_ON_WRITE: '0',
           BOARD_DB: join(dir, 'board.db'), ASSISTANT_DB: join(dir, 'assistant.db'),
           LODESTAR_AUTH_PASSWORD_HASH: '', LODESTAR_AUTH_PASSWORD: '',
           ...env },
  });
  try {
    const missing = boot({ LODESTAR_AUTH_PASSWORD_HASH: '' });
    assert.notEqual(missing.status, 0, 'the board opened with no way to protect it');
    assert.match(missing.stderr, /npm run auth:setup/);
    assert.equal(missing.stdout, '', 'a refused boot must not report a running server');

    const malformed = boot({ LODESTAR_AUTH_PASSWORD_HASH: 'scrypt$1$nonsense' });
    assert.notEqual(malformed.status, 0);
    assert.match(malformed.stderr, /not a hash/);

    // There is one legal auth mode, and a typo is a refusal rather than a
    // silent fallback to whichever branch the typo happens to miss.
    const typo = boot({ ...authEnv(), LODESTAR_AUTH_MODE: 'optional' });
    assert.notEqual(typo.status, 0);
    assert.match(typo.stderr, /no way to switch authentication off/);

    // A service token too short to survive guessing is refused at boot rather
    // than discovered later.
    const weak = boot({ ...authEnv(), LODESTAR_SERVICE_TOKEN: 'short' });
    assert.notEqual(weak.status, 0);
    assert.match(weak.stderr, /LODESTAR_SERVICE_TOKEN/);

    // Naming both is refused rather than resolved. The operator gets one
    // clear error instead of a password whose value depends on which line the
    // server happened to prefer.
    const both = boot({ ...authEnv(), LODESTAR_AUTH_PASSWORD: 'a real password' });
    assert.notEqual(both.status, 0);
    assert.match(both.stderr, /both/i);

    // A password too short to be worth hashing is refused at boot, the same
    // way `npm run auth:setup` refuses it at mint time.
    const short = boot({ LODESTAR_AUTH_PASSWORD: 'abc' });
    assert.notEqual(short.status, 0);
    assert.match(short.stderr, /LODESTAR_AUTH_PASSWORD/);

    // And with a real verifier it boots and says the boundary out loud —
    // whichever of the two forms supplied it.
    const ok = boot({ ...authEnv() }, 3000);
    assert.match(ok.stdout, /bound to 127\.0\.0\.1/);

    const plain = boot({ LODESTAR_AUTH_PASSWORD: 'a real password' }, 3000);
    assert.match(plain.stdout, /bound to 127\.0\.0\.1/,
      'a plaintext password in the environment is a verifier too');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

// This is an integration test. The password is now something the operator can
// change by editing one line, which only works if the server reads that file
// itself — until now nothing did, so a hash sitting in .env reached the
// process only if the shell had exported it, and `set -a; . ./.env` mangles a
// scrypt hash on the way (the shell eats every `$1$` in it as a positional
// parameter).
test('the server reads .env from its own directory, and the environment outranks it', async () => {
  const root = mkdtempSync(join(tmpdir(), 'lodestar-env-'));
  // The same self-sufficient copy tests/databases.test.js boots: server.js
  // plus the local modules it imports at boot. js/ is deliberately absent —
  // the server warns and serves the API alone, which is all a login needs.
  copyFileSync(join(ROOT, 'server.js'), join(root, 'server.js'));
  copyFileSync(join(ROOT, 'login.html'), join(root, 'login.html'));
  for (const dir of ['scripts', 'db', 'auth']) {
    cpSync(join(ROOT, dir), join(root, dir), { recursive: true });
  }
  // Quoted, the way .env's own comments insist on, and carrying a `$` for the
  // same reason: this file is read by three different things and only one of
  // them may be allowed to interpret that character.
  writeFileSync(join(root, '.env'),
    "LODESTAR_AUTH_PASSWORD='written in the file $1$'\n");

  // Neither credential set at all, so anything that arrives came from the
  // file. Deleted rather than blanked: a developer's own exported password
  // must not be what decides this test.
  const baseEnv = () => {
    const env = { ...process.env, PORT: '0', NODE_NO_WARNINGS: '1',
                  LODESTAR_BACKUP_ON_WRITE: '0',
                  BOARD_DB: join(root, 'board.db'),
                  ASSISTANT_DB: join(root, 'assistant.db') };
    delete env.LODESTAR_AUTH_PASSWORD_HASH;
    delete env.LODESTAR_AUTH_PASSWORD;
    return env;
  };

  const login = async (port, password) => (await raw(port, {
    method: 'POST', path: '/api/login',
    headers: { host: `127.0.0.1:${port}`, 'content-type': 'application/json' },
    body: JSON.stringify({ password }),
  })).status;

  const run = async (env, check) => {
    const proc = spawn('node', ['server.js'],
      { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
    proc.stderr.on('data', () => {});
    try {
      const [, bound] = await waitForLine(
        proc, /Lodestar running at http:\/\/localhost:(\d+)\b/);
      await check(Number(bound));
    } finally { proc.kill('SIGKILL'); }
  };

  try {
    // 1. The file alone is enough, which is the whole point: edit one line,
    //    restart, done.
    await run(baseEnv(), async (port) => {
      assert.equal(await login(port, 'written in the file $1$'), 200);
      assert.equal(await login(port, 'written in the file'), 401,
        'the $ must survive the file, the parser and the hash');
    });

    // 2. An EMPTY environment variable must not shadow the file. docker
    //    compose passes `${LODESTAR_AUTH_PASSWORD:-}` as an empty string when
    //    the host has none, and a loader that reads that as "already set"
    //    leaves the container refusing to boot over a .env sitting right
    //    there in the mounted tree.
    await run(
      { ...baseEnv(), LODESTAR_AUTH_PASSWORD_HASH: '', LODESTAR_AUTH_PASSWORD: '' },
      async (port) => {
        assert.equal(await login(port, 'written in the file $1$'), 200);
      });

    // 3. A real value outranks the file, so a container's own environment and
    //    a one-off `LODESTAR_AUTH_PASSWORD=… node server.js` both still win.
    //    Note what this also means: the file supplied no second credential
    //    here, so the both-are-set refusal does not fire — filling in is not
    //    the same act as naming two.
    await run({ ...baseEnv(), LODESTAR_AUTH_PASSWORD: 'from the environment' },
      async (port) => {
        assert.equal(await login(port, 'from the environment'), 200);
        assert.equal(await login(port, 'written in the file $1$'), 401);
      });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

// --------------------------------------------------------------------------
// 3. Browser provenance
// --------------------------------------------------------------------------

// This is an integration test. SameSite=Strict already means a cross-site page
// carries no cookie; this is the second, independent defence — the one that
// still holds if a browser ever disagrees with us about what a "site" is.
test('an authenticated mutation is refused when it comes from somewhere else', async () => {
  const s = await startServer();
  try {
    await seed(s.base, [{ id: 'keep', columnId: 'inbox', title: 'Mine' }]);
    const cookie = (await rawFetch(s.base + '/api/login', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: TEST_PASSWORD }),
    })).headers.get('set-cookie').split(';')[0];

    const wipe = (extra) => raw(s.port, {
      method: 'PUT', path: '/api/state',
      headers: jsonHeaders(s.port, { cookie, ...extra }),
      body: JSON.stringify({ version: 1, cards: [] }),
    });

    for (const extra of [
      { origin: 'https://evil.example.com' },
      { origin: 'http://localhost:1' },
      { origin: 'null' },
      { referer: 'https://evil.example.com/trap.html' },
      // Origin wins over a friendly-looking Referer.
      { origin: 'https://evil.example.com', referer: `http://localhost:${s.port}/` },
    ]) {
      const res = await wipe(extra);
      assert.equal(res.status, 403, `a mutation from ${JSON.stringify(extra)} was allowed`);
    }
    assert.deepEqual(await titles(s.base), ['Mine'],
      'a refused mutation still reached the board');

    // A DELETE from elsewhere is refused the same way, before it can purge.
    const del = await raw(s.port, {
      method: 'DELETE', path: '/api/cards/keep',
      headers: jsonHeaders(s.port, { cookie, origin: 'https://evil.example.com' }),
    });
    assert.equal(del.status, 403);

    // The real browser, and a non-browser client with no provenance headers at
    // all, both go through.
    assert.equal((await wipe({ origin: `http://localhost:${s.port}` })).status, 200);
    await seed(s.base, [{ id: 'keep', columnId: 'inbox', title: 'Mine' }]);
    assert.equal((await wipe({ referer: `http://127.0.0.1:${s.port}/index.html` })).status, 200);
    assert.equal((await wipe({})).status, 200);
  } finally { await s.stop(); }
});

// --------------------------------------------------------------------------
// 4. Trash-first deletion
// --------------------------------------------------------------------------

// This is an integration test. DELETE /api/cards/:id is the one statement in
// the whole server that truly erases a card, and it used to delete by id
// alone: the two-step promise lived entirely in the browser's confirm dialog,
// which is not a boundary and is not the only caller.
test('a card can be purged only after it is in the Trash', async () => {
  const s = await startServer();
  try {
    await seed(s.base, [
      { id: 'live', columnId: 'inbox', title: 'Still working on it' },
      { id: 'doomed', columnId: 'inbox', title: 'On its way out' },
    ]);

    const purge = (id) => fetch(s.base + '/api/cards/' + id, { method: 'DELETE' });

    // A live card: the call is answered, and reports that nothing was purged.
    const onLive = await purge('live');
    assert.equal(onLive.status, 200);
    assert.deepEqual(await onLive.json(), { ok: false });
    assert.deepEqual(await titles(s.base), ['On its way out', 'Still working on it'],
      'a live card was destroyed by a single call');

    // A card nobody has ever heard of changes nothing either.
    assert.deepEqual(await (await purge('never-existed')).json(), { ok: false });
    assert.equal((await titles(s.base)).length, 2);

    // Soft-delete 'doomed' the way the browser does — by omitting it — then
    // purge it for real.
    await seed(s.base, [{ id: 'live', columnId: 'inbox', title: 'Still working on it' }]);
    const trash = await (await fetch(s.base + '/api/trash')).json();
    assert.deepEqual(trash.cards.map((c) => c.id), ['doomed']);

    const first = await purge('doomed');
    assert.deepEqual(await first.json(), { ok: true });
    assert.deepEqual((await (await fetch(s.base + '/api/trash')).json()).cards, []);

    // Purging it again is a no-op, not an error and not a second deletion.
    assert.deepEqual(await (await purge('doomed')).json(), { ok: false });
    assert.deepEqual(await titles(s.base), ['Still working on it']);
  } finally { await s.stop(); }
});
