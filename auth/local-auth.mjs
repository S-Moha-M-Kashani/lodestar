// auth/local-auth.mjs — the local trust boundary, as pure functions.
//
// Lodestar is a personal board that holds a private life, and until this
// module existed its only defence was "nobody else is on this Wi-Fi". That is
// not a boundary: a laptop on university Wi-Fi answered every peer that knew
// its address, and a page open in any tab could read the whole board across
// origins. This file is the four things that replace that assumption —
//
//   1. which Host header names this service (DNS-rebinding aliases do not),
//   2. which Origin/Referer may make a browser mutate it,
//   3. a password verifier over Node's own scrypt, and
//   4. session tokens and a login throttle.
//
// It is deliberately free of `node:http` and of server.js: everything here is
// a value in, a value out, so tests/auth.test.js exercises the whole boundary
// with no socket, no database and no spawned process — the same rule that
// keeps js/core/asana.js and js/core/merge.js unit-testable. server.js does
// the wiring; this file does the deciding.
//
// Zero npm dependencies, like the rest of the backend: node:crypto has scrypt,
// randomBytes and timingSafeEqual, which is the whole shopping list.

import { createHash, randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';

// --------------------------------------------------------------------------
// Passwords
// --------------------------------------------------------------------------

// The stored verifier is one line of text, safe to paste into an ignored .env,
// and it carries its own parameters:
//
//   scrypt$1$<N>$<r>$<p>$<salt base64url>$<key base64url>
//
// Versioned (`$1$`) because a hash that does not say how it was made cannot be
// re-read after the cost parameters move, and "the parameters are whatever the
// code says today" is how a password file quietly becomes unverifiable. The
// parameters travel WITH the hash, so an old hash keeps verifying against the
// cost it was minted at and a regenerated one picks up the new default.
export const HASH_SCHEME = 'scrypt';
export const HASH_VERSION = 1;

// N=16384 (2^14), r=8, p=1 → about 16 MB and ~40 ms per verification on this
// machine. Chosen rather than the more common 2^15 for one boring reason and
// one real one: node's scrypt defaults to a 32 MB `maxmem` and 2^15 lands
// exactly on it (128·N·r = 32 MiB), so the stronger-looking constant throws
// unless every call site also remembers to raise maxmem — a footgun waiting
// for the one call site that forgets. And 40 ms is already ~10^4 times the
// cost of a plain hash for an attacker who has to guess a password nobody
// transmits over a network: this service answers loopback only.
export const SCRYPT_PARAMS = { N: 16384, r: 8, p: 1, keyLen: 32 };

const b64 = (buf) => buf.toString('base64url');

/** Mint a verifier for `password`. The plaintext is never returned or kept. */
export function hashPassword(password, params = SCRYPT_PARAMS) {
  if (typeof password !== 'string' || password === '') {
    throw new Error('A password is required');
  }
  const { N, r, p, keyLen } = params;
  const salt = randomBytes(16);
  const key = scryptSync(password, salt, keyLen, { N, r, p });
  return [HASH_SCHEME, HASH_VERSION, N, r, p, b64(salt), b64(key)].join('$');
}

/** Read a stored verifier. Returns null for anything this code cannot verify —
 *  wrong scheme, unknown version, missing field, non-numeric cost, junk. */
export function parsePasswordHash(stored) {
  if (typeof stored !== 'string') return null;
  const parts = stored.trim().split('$');
  if (parts.length !== 7) return null;
  const [scheme, version, N, r, p, salt, key] = parts;
  if (scheme !== HASH_SCHEME || Number(version) !== HASH_VERSION) return null;
  const nums = [Number(N), Number(r), Number(p)];
  if (nums.some((n) => !Number.isInteger(n) || n <= 0)) return null;
  // 2^20 · 8 · 128 is a gigabyte; a hash asking for more than that is not a
  // hash this server should try to honour at boot.
  if (nums[0] > 1 << 20 || nums[1] > 64 || nums[2] > 16) return null;
  let saltBuf; let keyBuf;
  try {
    saltBuf = Buffer.from(salt, 'base64url');
    keyBuf = Buffer.from(key, 'base64url');
  } catch { return null; }
  if (saltBuf.length < 8 || keyBuf.length < 16) return null;
  return { N: nums[0], r: nums[1], p: nums[2], salt: saltBuf, key: keyBuf };
}

/** True when `password` produced `stored`. False for a wrong password AND for
 *  a malformed hash, on purpose: the caller has one branch, so a login
 *  response cannot accidentally tell an attacker which of the two it was. The
 *  distinction that an operator does need is made once, at boot, where a
 *  malformed hash stops the server outright. */
export function verifyPassword(password, stored) {
  const parsed = parsePasswordHash(stored);
  if (!parsed || typeof password !== 'string') return false;
  const { N, r, p, salt, key } = parsed;
  let derived;
  try {
    derived = scryptSync(password, salt, key.length, { N, r, p });
  } catch { return false; }
  // Lengths are equal by construction (keyLen is taken from the stored key),
  // but timingSafeEqual throws on a mismatch rather than returning false, so
  // the guard stays.
  return derived.length === key.length && timingSafeEqual(derived, key);
}

/** Constant-time string comparison, for the service token. Compared as sha256
 *  digests so two different lengths are still compared in constant time —
 *  timingSafeEqual throws on unequal lengths, and an exception is itself a
 *  timing signal that leaks the secret's length. */
export function secretEquals(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const digest = (s) => createHash('sha256').update(s, 'utf8').digest();
  return timingSafeEqual(digest(a), digest(b));
}

// --------------------------------------------------------------------------
// Which Host names this service
// --------------------------------------------------------------------------

// The loopback names a browser can legitimately put in Host for a service
// listening on 127.0.0.1. Anything else — a LAN name, a laptop's Bonjour
// hostname, an attacker's domain pointed at 127.0.0.1 — is a rebinding alias
// and is refused before a single row is read.
const LOOPBACK_NAMES = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

/** Split `host:port` without tripping over IPv6 literals. Three shapes reach
 *  here: `name:port`, a bracketed literal `[::1]:3000`, and a bare literal
 *  `::1` that carries no port at all. Splitting on the last colon handles the
 *  first two and mangles the third, which is why the bracket case is read
 *  explicitly rather than inferred. */
function splitHost(value) {
  if (value.startsWith('[')) {
    const close = value.indexOf(']');
    if (close === -1) return { name: value, port: '' };
    const rest = value.slice(close + 1);
    return { name: value.slice(0, close + 1), port: rest.startsWith(':') ? rest.slice(1) : '' };
  }
  const at = value.indexOf(':');
  if (at === -1) return { name: value, port: '' };
  // A second colon with no brackets is a bare IPv6 literal, never a port.
  if (value.indexOf(':', at + 1) !== -1) return { name: value, port: '' };
  return { name: value.slice(0, at), port: value.slice(at + 1) };
}

/** True when `hostHeader` names this loopback service on `port`.
 *
 *  `extra` is the exact-match escape hatch for a container: inside Docker the
 *  brain dials `http://lodestar:3000`, a name no loopback rule can predict, so
 *  compose passes it through LODESTAR_ALLOWED_HOSTS. It is an allowlist of
 *  whole strings — never a suffix or wildcard match, which is precisely the
 *  shape of check that lets `evil-lodestar.example.com` through. */
export function hostAllowed(hostHeader, { port, extra = [] } = {}) {
  if (typeof hostHeader !== 'string' || hostHeader === '') return false;
  const value = hostHeader.trim().toLowerCase();
  // A Host with whitespace or a comma is either two headers joined by a proxy
  // or a smuggling attempt. Neither is this service's traffic.
  if (/[\s,]/.test(value)) return false;
  if (extra.some((h) => h.trim().toLowerCase() === value)) return true;
  const { name, port: given } = splitHost(value);
  if (!LOOPBACK_NAMES.has(name)) return false;
  // The port must be stated and must be the one we are actually listening on.
  // A Host naming the right name but the wrong port is not this service, and
  // an omitted port means :80, which this server never is.
  return given !== '' && Number(given) === Number(port);
}

/** True when a browser-supplied `Origin` is this service. `null` (the literal
 *  string a browser sends for an opaque origin) is never approved. */
export function originAllowed(origin, opts = {}) {
  if (typeof origin !== 'string' || origin === '' || origin === 'null') return false;
  let url;
  try { url = new URL(origin); } catch { return false; }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
  // `new URL('http://localhost:3000').host` keeps the port, drops a default
  // one — which is exactly the string hostAllowed wants.
  return hostAllowed(url.host, opts);
}

/** Same, for a `Referer` — a full URL rather than a bare origin. */
export function refererAllowed(referer, opts = {}) {
  if (typeof referer !== 'string' || referer === '') return false;
  try { return originAllowed(new URL(referer).origin, opts); } catch { return false; }
}

/** The provenance verdict for one request: 'ok' when nothing objects,
 *  'blocked' when a header is present and names somewhere else.
 *
 *  A missing Origin AND Referer is 'ok' on purpose. Browsers attach at least
 *  one of them to a cross-origin state-changing request, so absence means a
 *  non-browser client — curl, the brain, a test — and those have already had
 *  to authenticate to reach this check. Refusing them instead would buy no
 *  browser safety and would break every script on the machine. */
export function provenanceOf(headers, opts = {}) {
  const origin = headers.origin;
  if (typeof origin === 'string' && origin !== '') {
    return originAllowed(origin, opts) ? 'ok' : 'blocked';
  }
  const referer = headers.referer || headers.referrer;
  if (typeof referer === 'string' && referer !== '') {
    return refererAllowed(referer, opts) ? 'ok' : 'blocked';
  }
  return 'ok';
}

// --------------------------------------------------------------------------
// Sessions
// --------------------------------------------------------------------------

export const SESSION_COOKIE = 'lodestar_session';

// Twelve hours idle, seven days absolute. A personal board is opened and left
// open all day, so an idle window shorter than a working day would mean
// re-typing the password after lunch; an absolute cap means a session cannot
// live for ever by being touched, which is the property that makes a stolen
// cookie a finite problem. Both are wall-clock, both are checked on every
// request, and neither survives a restart — the store is a Map in this
// process, which is a security benefit here and saves a second durable
// credential store.
export const IDLE_MS = 12 * 60 * 60 * 1000;
export const ABSOLUTE_MS = 7 * 24 * 60 * 60 * 1000;

/** Cookies as an object. Values are returned raw; a malformed header yields
 *  whatever pairs could be read rather than throwing at the boundary. */
export function parseCookies(header) {
  const out = {};
  if (typeof header !== 'string') return out;
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    const name = part.slice(0, eq).trim();
    if (name !== '') out[name] = part.slice(eq + 1).trim();
  }
  return out;
}

export class SessionStore {
  constructor({ idleMs = IDLE_MS, absoluteMs = ABSOLUTE_MS, now = Date.now } = {}) {
    this.idleMs = idleMs;
    this.absoluteMs = absoluteMs;
    this.now = now;
    // key: sha256 of the raw token. The raw token exists in exactly two
    // places — the Set-Cookie header on the way out and the browser's cookie
    // jar — so a heap dump or a stray log of this map cannot mint a session.
    this.records = new Map();
  }

  static key(token) {
    return createHash('sha256').update(String(token), 'utf8').digest('hex');
  }

  /** A new session. Returns the raw token, the only time it is ever readable. */
  create() {
    const token = randomBytes(32).toString('base64url');
    const at = this.now();
    this.records.set(SessionStore.key(token), { born: at, seen: at });
    this.sweep();
    return token;
  }

  /** True when the token names a live session; touches it so idle time runs
   *  from the last request rather than from login. */
  verify(token) {
    if (typeof token !== 'string' || token === '') return false;
    const key = SessionStore.key(token);
    const rec = this.records.get(key);
    if (!rec) return false;
    const at = this.now();
    if (at - rec.born >= this.absoluteMs || at - rec.seen >= this.idleMs) {
      this.records.delete(key);
      return false;
    }
    rec.seen = at;
    return true;
  }

  revoke(token) {
    if (typeof token !== 'string') return false;
    return this.records.delete(SessionStore.key(token));
  }

  /** Drop expired records. Called on create so an abandoned browser's session
   *  cannot pile up for ever; verify already removes the one it is asked about. */
  sweep() {
    const at = this.now();
    for (const [key, rec] of this.records) {
      if (at - rec.born >= this.absoluteMs || at - rec.seen >= this.idleMs) {
        this.records.delete(key);
      }
    }
  }

  get size() { return this.records.size; }
}

/** The Set-Cookie line for a session. HttpOnly so no script can read it,
 *  SameSite=Strict so no cross-site navigation carries it, Path=/ because the
 *  whole app is protected. Deliberately NOT Secure: this service is http on
 *  loopback, and a Secure cookie would simply never be stored — a flag that
 *  silently breaks login is worse than one that is honestly absent. */
export function sessionCookie(token, { maxAgeMs = ABSOLUTE_MS } = {}) {
  return `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/; `
    + `Max-Age=${Math.floor(maxAgeMs / 1000)}`;
}

export function clearedCookie() {
  return `${SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`;
}

// --------------------------------------------------------------------------
// Login throttling
// --------------------------------------------------------------------------

// One bucket for the whole service, not one per address — the same reasoning
// as the assistant's rate limiter in server.js: this is a single-user board
// reached over loopback and through tunnels, so every attempt shares one
// source and a map keyed by it would be bookkeeping rather than protection.
//
// A lockout rather than a sleep, for the same reason the assistant refuses
// rather than paces: holding a connection open to punish a guesser is a way to
// run out of sockets. Five wrong passwords is well past a typo and well short
// of annoying; a minute is long enough to make guessing pointless and short
// enough that the owner just waits.
export const LOGIN_MAX_FAILURES = 5;
export const LOGIN_LOCKOUT_MS = 60 * 1000;

export class LoginThrottle {
  constructor({ maxFailures = LOGIN_MAX_FAILURES, lockoutMs = LOGIN_LOCKOUT_MS,
                now = Date.now } = {}) {
    this.maxFailures = maxFailures;
    this.lockoutMs = lockoutMs;
    this.now = now;
    this.failures = 0;
    this.until = 0;
  }

  /** Seconds the caller must wait, or 0 when it may try. */
  retryAfter() {
    const left = this.until - this.now();
    return left > 0 ? Math.ceil(left / 1000) : 0;
  }

  recordFailure() {
    this.failures += 1;
    if (this.failures >= this.maxFailures) {
      this.until = this.now() + this.lockoutMs;
      // Hold at the threshold rather than resetting: once a burst has earned a
      // lockout, the next single wrong password re-earns it instead of buying
      // four more free guesses per minute.
      this.failures = this.maxFailures - 1;
    }
  }

  recordSuccess() {
    this.failures = 0;
    this.until = 0;
  }
}

// --------------------------------------------------------------------------
// Alternatives considered
// --------------------------------------------------------------------------
//
// **A password library (bcrypt, argon2, @node-rs/argon2).** Argon2id is the
// better KDF — it resists GPU and ASIC attack in a way scrypt only partly
// does — and every one of those packages is a native addon. server.js has had
// exactly one npm dependency in its life (`pg`, because Postgres has no
// node:sqlite twin) and the rule that kept it that way is worth more here than
// the margin between two memory-hard KDFs: the threat this verifier faces is
// somebody typing guesses at a loopback socket, throttled at five a minute,
// not an offline crack of a leaked database. If the hash ever leaves this
// machine, the `$1$` version prefix is the hinge that lets a `$2$` argon2id
// format land beside it and verify both.
//
// **A session library (express-session, iron-session, JWT).** All of them
// solve the problem this app does not have — sessions that survive a restart,
// or that a second process can validate. One process, one user, a Map, and
// deliberate invalidation on restart. A JWT would be actively worse: it moves
// the session's authority into a token the server cannot revoke, and revoking
// on logout is a requirement here.
//
// **A CSRF token library.** The classic double-submit token defends a cookie
// that browsers attach to cross-site requests. SameSite=Strict means this one
// is not attached at all, so a token would guard a door that is already shut;
// the Origin/Referer check above is the belt to that braces, and both are
// free. The measurement that would change this: a browser this app must
// support that ignores SameSite. There is none.
//
// **A DNS-rebinding library / Host regex.** Rejected in favour of the exact
// set above. Every rebinding bug in the wild is a *pattern* that matched more
// than its author meant — a suffix check, an unanchored regex, a wildcard.
// A set of four names and one exact-match escape hatch cannot have that bug.
