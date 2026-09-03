// tests/auth.test.js — the local trust boundary, decided without a socket.
//
// Everything server.js does at the edge is a decision made in
// auth/local-auth.mjs, so the decisions are tested here as values and the
// wiring is tested over HTTP in tests/server.test.js. One test per way the
// boundary can be wrong; the edge cases are extra asserts inside the test they
// belong to rather than tests of their own.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hashPassword, parsePasswordHash, verifyPassword, secretEquals,
  resolvePasswordHash, MIN_PASSWORD_LENGTH,
  hostAllowed, originAllowed, refererAllowed, provenanceOf,
  parseCookies, sessionCookie, clearedCookie, SessionStore, SESSION_COOKIE,
  LoginThrottle,
} from '../auth/local-auth.mjs';

// This is a unit test.
test('a minted hash verifies its own password and nothing else', () => {
  const stored = hashPassword('correct horse battery staple');
  assert.match(stored, /^scrypt\$1\$16384\$8\$1\$[\w-]+\$[\w-]+$/);
  // The plaintext is nowhere in the verifier.
  assert.ok(!stored.includes('horse'));

  assert.equal(verifyPassword('correct horse battery staple', stored), true);
  assert.equal(verifyPassword('Correct horse battery staple', stored), false);
  assert.equal(verifyPassword('', stored), false);

  // Two hashes of the same password differ: the salt is random, so a stolen
  // .env cannot be recognised by comparing it to a precomputed table.
  assert.notEqual(hashPassword('same'), hashPassword('same'));

  // An empty password is refused at mint time rather than producing a
  // verifier that anything could satisfy.
  assert.throws(() => hashPassword(''), /password is required/);
});

// This is a unit test. A malformed hash must fail exactly the way a wrong
// password fails, so the login route has one branch and cannot leak which.
test('a malformed hash is unverifiable and indistinguishable from a wrong password', () => {
  const good = hashPassword('pw');
  const junk = [
    '', '   ', 'not-a-hash', 'scrypt$1$16384$8$1$onlysixfields',
    'scrypt$2$16384$8$1$AAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAA', // unknown version
    'bcrypt$1$16384$8$1$AAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAA', // wrong scheme
    'scrypt$1$abc$8$1$AAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAA',   // non-numeric cost
    'scrypt$1$16384$8$1$AA$AA',                               // salt/key too short
    `scrypt$1$99999999$8$1$${good.split('$')[5]}$${good.split('$')[6]}`, // absurd cost
    null, undefined, 42,
  ];
  for (const bad of junk) {
    assert.equal(parsePasswordHash(bad), null, `parsed junk: ${String(bad)}`);
    assert.equal(verifyPassword('pw', bad), false, `verified junk: ${String(bad)}`);
  }
  // And the good one still reads back with its parameters intact, which is
  // what lets an old hash keep verifying after the default cost moves.
  const parsed = parsePasswordHash(good);
  assert.equal(parsed.N, 16384);
  assert.equal(parsed.key.length, 32);
});

// This is a unit test. Which verifier the server boots with is one decision
// with two right answers and four wrong ones, so it is made here as a value
// rather than inline at boot. The two right answers are the pre-minted hash
// `npm run auth:setup` prints and a plaintext password typed straight into
// .env — the second one exists because changing a password should be editing
// one line, not running a script and pasting its output.
test('the boot verifier comes from a hash or a plaintext password, never both', () => {
  const minted = hashPassword('from the hash');
  const fromHash = resolvePasswordHash({ LODESTAR_AUTH_PASSWORD_HASH: minted });
  assert.equal(fromHash.hash, minted);
  assert.equal(fromHash.source, 'hash');

  // The editable form. What matters is that the password on the line is the
  // one that logs in, and that what the server ends up holding is still a
  // hash — the plaintext is hashed at boot and not kept.
  const fromPlain = resolvePasswordHash({ LODESTAR_AUTH_PASSWORD: 'open sesame' });
  assert.equal(fromPlain.source, 'password');
  assert.equal(verifyPassword('open sesame', fromPlain.hash), true);
  assert.equal(verifyPassword('open sesam', fromPlain.hash), false);
  assert.ok(!fromPlain.hash.includes('open sesame'));
  assert.ok(parsePasswordHash(fromPlain.hash), 'the resolver must hand back a readable verifier');

  // Surrounding whitespace is stripped, like every other env value the server
  // reads. A password whose last character is an invisible one nobody can see
  // in .env is a login that fails for no visible reason.
  assert.equal(
    verifyPassword('open sesame',
      resolvePasswordHash({ LODESTAR_AUTH_PASSWORD: '  open sesame  ' }).hash),
    true);

  // `$` is a literal. It is the character scrypt's own format is built out of
  // and the reason .env values here are quoted; a resolver that let a shell or
  // an interpolator eat it would accept a different password than the one
  // written down, and say nothing.
  assert.equal(
    verifyPassword('a$1$16384$b',
      resolvePasswordHash({ LODESTAR_AUTH_PASSWORD: 'a$1$16384$b' }).hash),
    true);

  // Neither set: refuse, and name both ways out.
  assert.throws(() => resolvePasswordHash({}), /npm run auth:setup/);
  assert.throws(() => resolvePasswordHash({}), /LODESTAR_AUTH_PASSWORD\b/);

  // An empty value is "not set", not "set to nothing" — that is what every
  // test harness and every `${VAR:-}` in docker-compose.yml actually passes,
  // and reading it as a configured-but-blank password would be a board with no
  // door on it.
  assert.throws(
    () => resolvePasswordHash({ LODESTAR_AUTH_PASSWORD_HASH: '', LODESTAR_AUTH_PASSWORD: '   ' }),
    /npm run auth:setup/);

  // Both set: refuse rather than pick. Either choice buys the same bad
  // afternoon — an operator edits the plaintext, restarts, and the old
  // password still works because a stale hash outranked it (or the reverse,
  // and a hash they carefully minted is silently ignored).
  assert.throws(
    () => resolvePasswordHash({ LODESTAR_AUTH_PASSWORD_HASH: minted,
                                LODESTAR_AUTH_PASSWORD: 'open sesame' }),
    /both/i);

  // A malformed hash keeps its own message, so an operator reading a refused
  // boot is told which of the two problems they have. (A login response must
  // never make that distinction; a boot error is the one place it belongs.)
  assert.throws(
    () => resolvePasswordHash({ LODESTAR_AUTH_PASSWORD_HASH: 'scrypt$1$nonsense' }),
    /not a hash/);

  // Too short to be worth hashing, enforced here as well as in
  // scripts/set-password.mjs — .env is now a second way in, and a minimum that
  // only guards one of them is not a minimum.
  assert.throws(
    () => resolvePasswordHash({
      LODESTAR_AUTH_PASSWORD: 'x'.repeat(MIN_PASSWORD_LENGTH - 1) }),
    new RegExp(String(MIN_PASSWORD_LENGTH)));
  assert.ok(
    resolvePasswordHash({ LODESTAR_AUTH_PASSWORD: 'x'.repeat(MIN_PASSWORD_LENGTH) }).hash,
    'exactly the minimum is long enough');
});

// This is a unit test.
test('secretEquals compares service tokens without leaking length', () => {
  const token = 'a'.repeat(48);
  assert.equal(secretEquals(token, token), true);
  assert.equal(secretEquals(token, 'a'.repeat(47)), false);
  assert.equal(secretEquals(token, ''), false);
  assert.equal(secretEquals('', ''), true);
  assert.equal(secretEquals(token, undefined), false);
});

// This is a unit test. The Host allowlist is the DNS-rebinding defence: a page
// on any domain can point that domain at 127.0.0.1, but it cannot change the
// Host header its browser sends.
test('Host is accepted only for this loopback service on this port', () => {
  const at = { port: 3000, extra: ['lodestar:3000'] };
  for (const ok of ['localhost:3000', '127.0.0.1:3000', '[::1]:3000',
                    'LOCALHOST:3000', 'lodestar:3000']) {
    assert.equal(hostAllowed(ok, at), true, `rejected ${ok}`);
  }
  for (const no of [
    'evil.example.com:3000',      // a rebinding alias pointed at loopback
    'lodestar.example.com:3000',  // a suffix/prefix of an allowed name
    'notlodestar:3000',
    '192.168.1.42:3000',          // the LAN address a Wi-Fi peer would use
    'my-laptop.local:3000',
    'localhost',                  // no port means :80, which we never are
    'localhost:3001',             // the sandbox board, not this one
    'localhost:3000,evil.com',    // two headers joined by a proxy
    'localhost :3000',
    '', null, undefined,
  ]) {
    assert.equal(hostAllowed(no, at), false, `accepted ${String(no)}`);
  }
  // The port really is compared, not assumed: the same server on 3001 accepts
  // the mirror-image set.
  assert.equal(hostAllowed('localhost:3001', { port: 3001 }), true);
  // With no extra hosts configured, the container name is just another alias.
  assert.equal(hostAllowed('lodestar:3000', { port: 3000 }), false);
});

// This is a unit test.
test('browser provenance is approved only for a local origin', () => {
  const at = { port: 3000 };
  assert.equal(provenanceOf({ origin: 'http://localhost:3000' }, at), 'ok');
  assert.equal(provenanceOf({ origin: 'http://127.0.0.1:3000' }, at), 'ok');
  assert.equal(provenanceOf({ referer: 'http://localhost:3000/index.html' }, at), 'ok');

  for (const hostile of ['http://evil.example.com', 'https://evil.example.com',
                         'http://localhost:3001', 'null', 'file:///x', 'garbage']) {
    assert.equal(provenanceOf({ origin: hostile }, at), 'blocked', `allowed ${hostile}`);
  }
  assert.equal(provenanceOf({ referer: 'http://evil.example.com/x' }, at), 'blocked');

  // Origin wins when both are present, so a hostile page cannot smuggle a
  // friendly Referer past a hostile Origin.
  assert.equal(provenanceOf(
    { origin: 'http://evil.example.com', referer: 'http://localhost:3000/' }, at), 'blocked');

  // Neither header: a non-browser client, which has already authenticated.
  assert.equal(provenanceOf({}, at), 'ok');
  assert.equal(provenanceOf({ origin: '' }, at), 'ok');

  assert.equal(originAllowed('http://localhost:3000', at), true);
  assert.equal(refererAllowed('http://localhost:3000/js/main.js', at), true);
  assert.equal(refererAllowed('not a url', at), false);
});

// This is a unit test.
test('cookies parse, and the session cookie carries its protections', () => {
  assert.deepEqual(parseCookies('a=1; b=two'), { a: '1', b: 'two' });
  assert.deepEqual(parseCookies('junk; a=1'), { a: '1' });
  assert.deepEqual(parseCookies(undefined), {});

  const line = sessionCookie('tok');
  assert.match(line, /^lodestar_session=tok;/);
  for (const attr of ['HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=']) {
    assert.ok(line.includes(attr), `session cookie is missing ${attr}`);
  }
  // Not Secure on purpose: the service is http on loopback, and a Secure
  // cookie there is simply never stored.
  assert.ok(!/;\s*Secure/i.test(line));
  assert.match(clearedCookie(), /Max-Age=0/);
  assert.equal(parseCookies(line.split(';')[0])[SESSION_COOKIE], 'tok');
});

// This is a unit test.
test('a session expires by idle time, by absolute age, and on revoke', () => {
  let now = 1_000_000;
  const store = new SessionStore({ idleMs: 100, absoluteMs: 1000, now: () => now });

  const token = store.create();
  assert.ok(token.length >= 40, 'the token must be unguessable, not a counter');
  assert.notEqual(store.create(), token);
  assert.equal(store.verify(token), true);

  // Touching it keeps it alive past the idle window.
  now += 90; assert.equal(store.verify(token), true);
  now += 90; assert.equal(store.verify(token), true);

  // Left alone for the idle window, it is gone — and gone for good, not merely
  // reported expired.
  now += 100;
  assert.equal(store.verify(token), false);
  assert.equal(store.verify(token), false);

  // The absolute cap bites even for a session touched constantly.
  const busy = store.create();
  for (let i = 0; i < 19; i += 1) { now += 50; assert.equal(store.verify(busy), true); }
  now += 50;
  assert.equal(store.verify(busy), false, 'an absolute cap a session can outrun is not a cap');

  const live = store.create();
  assert.equal(store.revoke(live), true);
  assert.equal(store.verify(live), false);
  assert.equal(store.verify('not-a-token'), false);
  assert.equal(store.verify(''), false);

  // A fresh store is a restart: no token from the old one survives it.
  assert.equal(new SessionStore().verify(store.create()), false);
});

// This is a unit test.
test('login throttling locks a burst out and clears on success', () => {
  let now = 0;
  const t = new LoginThrottle({ maxFailures: 3, lockoutMs: 1000, now: () => now });

  assert.equal(t.retryAfter(), 0);
  t.recordFailure(); t.recordFailure();
  assert.equal(t.retryAfter(), 0, 'a typo or two must not lock the owner out');
  t.recordFailure();
  assert.equal(t.retryAfter(), 1);

  now += 999;
  assert.equal(t.retryAfter(), 1);
  now += 1;
  assert.equal(t.retryAfter(), 0, 'the lockout is bounded, not permanent');

  // One more wrong password re-earns the lockout immediately rather than
  // buying another full burst of free guesses.
  t.recordFailure();
  assert.equal(t.retryAfter(), 1);

  now += 1000;
  t.recordSuccess();
  assert.equal(t.retryAfter(), 0);
  t.recordFailure(); t.recordFailure();
  assert.equal(t.retryAfter(), 0, 'a successful login clears the counter');
});
