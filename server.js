// Lodestar server — serves the static app and persists the board to a
// local SQLite file so cards survive restarts. Zero npm dependencies:
// Node's built-in http server and node:sqlite do all the work.
//
//   node server.js                 # http://localhost:3000, databases/board.db
//   PORT=4000 BOARD_DB=/tmp/x.db node server.js
//
// The board is stored one row per card. The client sends its whole state
// on every change (PUT /api/state); the server upserts the rows it sees and
// SOFT-deletes the rows it doesn't (marks them, never removes them) — so a
// partial or buggy save can never lose a card. That reading of "absent" only
// applies to a save that says which version of the board it was written
// against, or says nothing at all: one naming a version this board has moved
// past is applied additively and deletes nothing (`rev`, `mergeBoard`). Soft-deleted rows live on in
// the Trash (GET /api/trash) and are recoverable until an explicit, deliberate
// purge (DELETE /api/cards/:id). That purge is the ONLY thing that truly erases
// a card from the database; otherwise the only way to lose data is to delete
// the database file itself. The chat record works the same way and has exactly
// one purge of its own (DELETE /api/chat/trash/:id) — same shape, same rule:
// nothing is destroyed by the call that hides it.

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { mkdirSync, readdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createHash, randomBytes } from 'node:crypto';
import {
  resolvePasswordHash, verifyPassword, secretEquals, hostAllowed, provenanceOf,
  parseCookies, sessionCookie, clearedCookie, SessionStore, LoginThrottle,
  SESSION_COOKIE, ABSOLUTE_MS,
} from './auth/local-auth.mjs';
import { resolveBoardDb, resolveAssistantDb } from './scripts/db-location.mjs';
import { applyEnvFile } from './scripts/env-file.mjs';
import { chooseBackend } from './db/backend.mjs';

const ROOT = dirname(fileURLToPath(import.meta.url));

// .env, before anything below reads a variable out of the environment. It
// fills in the board's own settings and never overrides one that is really
// set, so a container and a one-off `VAR=… npm start` both still win; see
// scripts/env-file.mjs for why Node's own --env-file is not what does this.
applyEnvFile({ root: ROOT });

// Which store this process opens. Asked here, at boot, because a seam nothing
// calls is not a seam: until this line existed, LODESTAR_DB_BACKEND=postgres
// was accepted, logged nowhere, and served SQLite — a recognised value falling
// back in silence, which is the exact failure the seam was written to prevent,
// reached from the other side. `postgres` therefore RAISES rather than
// proceeding: there is no Postgres store yet, and the honest answer to "open
// Postgres" is a refusal naming the phase that will make it possible.
const DB_BACKEND = chooseBackend(process.env);
if (DB_BACKEND !== 'sqlite') {
  throw new Error(
    `LODESTAR_DB_BACKEND=${DB_BACKEND} is a real backend but the ${DB_BACKEND} `
    + 'store is not wired up yet — server.js still reads and writes SQLite '
    + 'only. Wiring it is the store phase of the Postgres migration (the data '
    + 'is already mirrored by scripts/sqlite-to-postgres.mjs). Unset '
    + 'LODESTAR_DB_BACKEND, or set it to sqlite, to boot.');
}
// --------------------------------------------------------------------------
// The local trust boundary, decided at boot
// --------------------------------------------------------------------------
//
// Until 2026-09-01 this server listened on every interface and asked nothing of
// anybody: a laptop on university Wi-Fi served the owner's whole private life
// to any peer who knew its address, and any page in any tab could read and
// rewrite the board across origins. Three things replace that, and all three
// are decided here, before a single database file is opened — a server that
// cannot protect the board must not get as far as touching it.
//
// The decisions themselves are in auth/local-auth.mjs, which knows nothing
// about HTTP and is unit-tested as values (tests/auth.test.js).

// Where the listener binds. Loopback by default and deliberately not a
// convenience switch: LODESTAR_BIND exists for ONE caller, a container, whose
// app must bind its own 0.0.0.0 because Docker forwards to the container's IP
// and a loopback bind inside it is reachable by nothing at all. The boundary
// there moves up one level to the host publication, which compose pins to
// "127.0.0.1:3000:3000". Setting this to 0.0.0.0 outside a container puts the
// board on the Wi-Fi, and no amount of login makes that a good idea.
const BIND = process.env.LODESTAR_BIND || '127.0.0.1';

// Host values this service answers to, beyond the loopback names it derives
// from the port it actually binds. Comma-separated, exact matches only. The
// one real user is compose again: inside the network the brain dials
// http://lodestar:3000, a name no loopback rule can predict.
const ALLOWED_HOSTS = (process.env.LODESTAR_ALLOWED_HOSTS || '')
  .split(',').map((h) => h.trim()).filter(Boolean);

// There is exactly one legal value, and that is the point. An auth mode with an
// `off` in it is a foot-gun with a documented name: the day something goes
// wrong at 1 a.m. it gets set, and it never gets unset. If a bypass is ever
// genuinely needed it can be its own reviewed change, with its own reasons —
// never an implicit fallback reached by mistyping this variable.
const AUTH_MODE = (process.env.LODESTAR_AUTH_MODE || 'required').trim();
if (AUTH_MODE !== 'required') {
  throw new Error(
    `LODESTAR_AUTH_MODE is "${AUTH_MODE}"; the only supported value is `
    + '"required". There is deliberately no way to switch authentication off.');
}

// One decision with two right answers — a minted hash, or the password itself
// on a line in .env — and it is made in auth/local-auth.mjs so that every way
// it can be wrong is a value a test can hold. Missing, malformed, too short
// and both-at-once each raise with their own message: this boot error is the
// one place those may be told apart, since a login response that distinguished
// them would tell a guesser whether the server is even configured.
const { hash: PASSWORD_HASH, source: PASSWORD_SOURCE } =
  resolvePasswordHash(process.env);

// A second credential, for one caller that is not a person: the brain, which
// reads cards and posts proposals over the same API the browser uses. It gets
// a token rather than the user's password, so the plaintext lives in exactly
// one head and no second service's environment. Absent = no service caller;
// present and short = a token that would not survive being guessed, which is
// worth refusing at boot rather than discovering later.
const SERVICE_TOKEN = (process.env.LODESTAR_SERVICE_TOKEN || '').trim();
if (SERVICE_TOKEN && SERVICE_TOKEN.length < 32) {
  throw new Error(
    'LODESTAR_SERVICE_TOKEN is shorter than 32 characters. Generate one with: '
    + "node -e \"console.log(require('crypto').randomBytes(32).toString('base64url'))\"");
}

// A third credential, and the only one that guards a *page* rather than the
// API: the developer trace at /dev/trace. Unset — the default, and what a
// normal board runs with — the whole surface is absent: no route, no storage,
// nothing to find. Set, it is a second lock in front of the ordinary session,
// because a trace is the system prompt, the question and every tool result in
// one record, and that deserves more than "you are already logged in".
const DEV_KEY = (process.env.LODESTAR_DEV_KEY || '').trim();
if (DEV_KEY && DEV_KEY.length < 12) {
  throw new Error(
    'LODESTAR_DEV_KEY is shorter than 12 characters — refusing to guard the '
    + 'developer trace with something guessable. Generate one with: '
    + "node -e \"console.log(require('crypto').randomBytes(24).toString('base64url'))\"");
}
// The unlock token lives here and only here: in this process's memory, never in
// a database, never in a file. Restarting the server locks the trace again,
// which is the feature — a debugging door that survives a restart is a door
// somebody forgot to close.
let devToken = '';

const sessions = new SessionStore();
// Overridable only because a test that waits a real minute to prove a lockout
// is a test nobody runs; the default is the one the server ships with.
const loginThrottle = new LoginThrottle({
  lockoutMs: Number(process.env.LODESTAR_LOGIN_LOCKOUT_MS) || undefined,
});

// `|| 3000` would be wrong here: PORT=0 is a real request — it asks the kernel
// for any free port, which is how the tests start servers that cannot collide —
// and zero is falsy, so it used to arrive as 3000 and bind the dev board's port.
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;
// databases/board.db by default (BOARD_DB overrides), moving a legacy
// root-level board.db in — backed up first — the first time it boots.
const DB_PATH = resolveBoardDb({ root: ROOT, env: process.env });
const AGENT_URL = process.env.AGENT_URL || 'http://127.0.0.1:9000';
// A token bucket in front of the brain. This is the only place that can refuse:
// the brain answers whatever reaches it, so a client stuck in a retry loop would
// spend real API credit or pin a local model for minutes. Capacity and refill
// are separate settings because they answer different questions — how many
// requests may land at once, and how fast they are earned back. The defaults sit
// far above deliberate use (nobody types 60 questions in a burst) and far below
// a runaway loop, which does thousands a minute.
//
// One bucket for the whole assistant surface, not one per client address: this
// is a single-user local board, so a map keyed by a value that never varies
// would be bookkeeping rather than protection.
//
// LangChain's `InMemoryRateLimiter` on the chat model was considered and is not
// this: it paces calls by *sleeping*, so under a flood it turns a fast refusal
// into a queue of held-open connections. Saying no is the point.
const AGENT_BURST = Number(process.env.LODESTAR_AGENT_BURST) || 60;
const AGENT_PER_MIN = Number(process.env.LODESTAR_AGENT_PER_MIN) || 240;
const agentBucket = { tokens: AGENT_BURST, at: Date.now() };

/** Spend one token. Returns null when there was one, otherwise the whole
 *  seconds until the next — refill is continuous, so a pause of any length is
 *  credited without a timer running. */
function agentRetryAfter() {
  const now = Date.now();
  const perMs = AGENT_PER_MIN / 60000;
  agentBucket.tokens = Math.min(AGENT_BURST,
    agentBucket.tokens + (now - agentBucket.at) * perMs);
  agentBucket.at = now;
  if (agentBucket.tokens >= 1) {
    agentBucket.tokens -= 1;
    return null;
  }
  return Math.max(1, Math.ceil((1 - agentBucket.tokens) / perMs / 1000));
}

// A new card on the board is worth a snapshot of the database. Off only
// when explicitly disabled — the test suites set this to '0' so they never add
// throwaway boards to the user's real backup history.
const BACKUP_ON_WRITE = process.env.LODESTAR_BACKUP_ON_WRITE !== '0';
const BACKUP_SCRIPT = join(ROOT, 'scripts', 'backup-db.mjs');

const COLUMN_IDS = ['inbox', 'in-progress', 'answered'];
// 'plan' was a type until 2026-08-28 and is now a date on every card. One
// still arriving as a plan — an older browser, an old export, an assistant
// working from memory — is stored as a task with its dates intact; coercing it
// to 'question' (the fallback for real nonsense) would have re-filed years of
// work as unanswered. Mirrors typeVal in js/core/cards.js.
const TYPES = ['question', 'problem', 'task', 'idea', 'dream', 'habit'];
const LEGACY_TYPES = { plan: 'task' };
const typeVal = (t) => (TYPES.includes(t) ? t : LEGACY_TYPES[t] || 'question');

// Categories are the user's own registry (id + label + oklch hue), stored in
// their own table and editable from the app. These defaults seed an empty DB.
const DEFAULT_CATEGORIES = [
  { id: 'work', label: 'Work', h: 255 },
  { id: 'love', label: 'Love', h: 15 },
  { id: 'family', label: 'Family', h: 60 },
  { id: 'health', label: 'Health', h: 150 },
  { id: 'mind', label: 'Mind', h: 295 },
  { id: 'music', label: 'Music', h: 340 },
  { id: 'travel', label: 'Travel', h: 200 },
  { id: 'home', label: 'Home', h: 90 },
  { id: 'money', label: 'Money', h: 40 },
];
const CAT_LIMIT = 24;
const catSlug = (s) =>
  String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);

/** Same shape/rules as the client: [{id, label, h}] for an array — empty
 *  included, deleting the last category is a real state to persist — or null
 *  when the field was absent or not an array at all. */
function sanitizeCategories(raw) {
  if (!Array.isArray(raw)) return null;
  const seen = new Set();
  const out = [];
  for (const c of raw) {
    if (!c || typeof c !== 'object') continue;
    const id = typeof c.id === 'string' ? catSlug(c.id) : '';
    const label = typeof c.label === 'string' && c.label.trim() ? c.label.trim().slice(0, 24) : '';
    const h = Number.isFinite(c.h) ? ((Math.round(c.h) % 360) + 360) % 360 : null;
    if (!id || !label || h === null || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, label, h });
    if (out.length >= CAT_LIMIT) break;
  }
  return out;
}
const iuVal = (v) => (v === 'high' || v === 'low' ? v : '');
// Effort & control always hold a value (the scale's midpoint until someone —
// or later the brain — sets them); the *_src columns record who set it.
const effortVal = (v) => (v === 'low' || v === 'high' ? v : 'medium');
const controlVal = (v) => (v === 'act' || v === 'none' ? v : 'influence');
const srcVal = (v) => (v === 'user' || v === 'ai' ? v : 'default');

// A deadline is an ISO calendar date ('YYYY-MM-DD') or unset (''). The
// round-trip through toISOString rejects shape-valid impossibilities
// like 2026-13-45 that a regex alone would let through.
const deadlineVal = (v) => {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return '';
  const d = new Date(v + 'T00:00:00Z');
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v ? v : '';
};

// --- The plan -------------------------------------------------------------
// When a card is meant to happen, as a *partial* ISO date: '2027', '2027-03'
// or '2027-03-04'. One string, so a day cannot exist without its month. A bad
// tail is dropped rather than the whole value — losing a year someone typed
// because the day was wrong is the worse trade. planSrc records who set it:
// while it is 'auto' the plan mirrors the deadline, and once a person or the
// brain has set one, nothing overwrites it. Mirrored from js/core/plan.js,
// because the server never trusts the client's validation.
const daysInMonth = (year, month) => new Date(year, month, 0).getDate();
const planVal = (v) => {
  if (typeof v !== 'string') return '';
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$/.exec(v.trim());
  if (!m) return '';
  const year = Number(m[1]);
  if (year < 1900 || year > 2999) return '';
  if (m[2] === undefined) return m[1];
  const month = Number(m[2]);
  if (month < 1 || month > 12) return m[1];
  if (m[3] === undefined) return `${m[1]}-${m[2]}`;
  const day = Number(m[3]);
  if (day < 1 || day > daysInMonth(year, month)) return `${m[1]}-${m[2]}`;
  return `${m[1]}-${m[2]}-${m[3]}`;
};
const planSrcVal = (v) => (v === 'user' || v === 'ai' ? v : 'auto');
const resolvePlan = (plan, planSrc, deadline) =>
  (planSrcVal(planSrc) === 'auto' ? deadlineVal(deadline) : planVal(plan));

// --- Habits ---------------------------------------------------------------
// A habit repeats: habitFreq names the calendar period, habitCount is how many
// times per period, habitTimes are optional reminder slots, and habitHistory
// holds the completions. None of these is coupled to the card's type — someone
// who stamps a habit as a task by mistake must find the history intact when
// they stamp it back. Only the type decides whether anything *reads* them.
const HABIT_FREQS = ['daily', 'weekly', 'monthly', 'yearly'];
const HABIT_MAX_COUNT = 99;
// ~13 months of daily periods, 400 weeks, or 400 years. A cap is needed because
// the whole board travels in every PUT /api/state.
const HABIT_MAX_PERIODS = 400;
const HABIT_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
// A period id: 2026 | 2026-07 | 2026-07-30 | 2026-W31.
const HABIT_PERIOD_RE =
  /^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?|-W(0[1-9]|[1-4]\d|5[0-3]))?$/;

const habitFreqVal = (v) => (HABIT_FREQS.includes(v) ? v : '');

const habitCountVal = (v) => {
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) ? Math.min(HABIT_MAX_COUNT, Math.max(1, n)) : 1;
};

/** Reminder slots: real clock times, in order, no repeats, never more than the
 *  target — a fifth slot on a 4×-a-day habit reminds you of nothing. */
const habitTimesVal = (v, count) => {
  if (!Array.isArray(v)) return [];
  const seen = new Set();
  for (const t of v) if (typeof t === 'string' && HABIT_TIME_RE.test(t)) seen.add(t);
  return [...seen].sort().slice(0, count);
};

/** Validate the completions entry by entry rather than accepting or rejecting
 *  the lot: one unreadable period must not cost the user the other 399. */
function habitHistoryVal(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
  const kept = [];
  for (const [period, stamps] of Object.entries(v)) {
    if (!HABIT_PERIOD_RE.test(period) || !Array.isArray(stamps)) continue;
    const clean = stamps.filter((t) => Number.isFinite(t)).sort((a, b) => a - b);
    if (clean.length) kept.push([period, clean]);
  }
  // Period ids sort lexicographically, so the tail is the recent history.
  kept.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return Object.fromEntries(kept.slice(-HABIT_MAX_PERIODS));
}

const safeJson = (json, fallback) => {
  try {
    const v = JSON.parse(json);
    return v === null || v === undefined ? fallback : v;
  } catch {
    return fallback;
  }
};

// --------------------------------------------------------------------------
// Database
// --------------------------------------------------------------------------

// Make sure the DB's directory exists — on Azure App Service we point
// BOARD_DB at /home/data (persistent storage) which may not exist on first boot.
mkdirSync(dirname(DB_PATH), { recursive: true });

// One file, several writers. Since 2026-08-31 the composed container and a
// native `npm start` open the SAME databases/real/board.db (before that they
// each had their own copy, so contention was impossible), and this machine also
// runs a third server on :3005. node:sqlite's default busy timeout is 0 and its
// default journal is rollback, which together mean a reader that arrives during
// another process's commit gets SQLITE_BUSY immediately — surfacing as a save
// silently refused with `400 "Invalid JSON: database is locked"`. That
// mislabeling is a second bug, fixed separately at every `Invalid JSON` catch
// below (`if (!(err instanceof SyntaxError) && !err.badRequest) throw err`): a
// store error is no longer reported as a bad request, even if it does outlast
// the timeout. `readBody`'s own payload-too-large rejection is exempted by
// that same `badRequest` flag — it is the caller's fault, not the store's.
//
// `timeout` makes a blocked statement wait instead of failing, and WAL is the
// part that actually fixes it: with a write-ahead log a reader and a writer
// coexist, so only writer-against-writer ever waits. WAL is a property of the
// file, not of the connection — it survives in the header once set, and every
// later opener inherits it.
// The third part of the same fix lives at every write instead: `BEGIN
// IMMEDIATE`, not a bare `BEGIN`. A deferred transaction takes its write lock
// only when the first write arrives, by which point it has already read — and
// SQLite refuses to make it WAIT there, because two transactions each holding a
// read snapshot and each waiting to upgrade is a deadlock. It returns
// SQLITE_BUSY at once and the timeout below never gets a say. Taking the lock
// up front is what makes the timeout mean anything, and it is why this file has
// no bare `BEGIN` left. Measured: with WAL and a 5 s timeout but a deferred
// BEGIN, tests/concurrency.test.js still failed with "database is locked".
//
// Five seconds: long enough to sit out any commit this server makes (they are
// single-statement writes over a few thousand rows), short enough that a truly
// stuck peer surfaces as an error rather than a hung browser.
const BUSY_TIMEOUT_MS = 5000;

const openDb = (path) => {
  const opened = new DatabaseSync(path, { timeout: BUSY_TIMEOUT_MS });
  opened.exec('PRAGMA journal_mode = WAL');
  return opened;
};

const db = openDb(DB_PATH);

// The boards themselves. Created and seeded BEFORE `cards`, because a card's
// board_id references this table and the very first card must have a board to
// point at. A board is soft-deleted like everything else here: `deleted_at`
// takes it out of the picker and out of every scoped read, and its cards sit
// untouched in the file until the board is either restored or purged.
db.exec(`
  CREATE TABLE IF NOT EXISTS boards (
    id         TEXT PRIMARY KEY,
    name       TEXT    NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER
  );
`);

// The board every card had before boards existed, and the one a caller that
// names none is answered with. Its id is fixed: it is written into the
// board_id column default below, so a database migrated by ALTER TABLE and one
// created fresh agree on where the existing cards live.
const DEFAULT_BOARD_ID = 'main';
if (db.prepare('SELECT COUNT(*) AS n FROM boards').get().n === 0) {
  const now = Date.now();
  db.prepare('INSERT INTO boards (id, name, position, created_at, updated_at) VALUES (?, ?, 0, ?, ?)')
    .run(DEFAULT_BOARD_ID, 'Lodestar', now, now);
}

db.exec(`
  CREATE TABLE IF NOT EXISTS cards (
    id         TEXT PRIMARY KEY,
    board_id   TEXT    NOT NULL DEFAULT 'main' REFERENCES boards(id),
    column_id  TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    notes      TEXT    NOT NULL DEFAULT '',
    priority   TEXT    NOT NULL DEFAULT 'medium',
    type       TEXT    NOT NULL DEFAULT 'question',
    category   TEXT    NOT NULL DEFAULT '',
    importance TEXT    NOT NULL DEFAULT '',
    urgency    TEXT    NOT NULL DEFAULT '',
    num        INTEGER NOT NULL DEFAULT 0,
    tags       TEXT    NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER,
    effort      TEXT NOT NULL DEFAULT 'medium',
    control     TEXT NOT NULL DEFAULT 'influence',
    effort_src  TEXT NOT NULL DEFAULT 'default',
    control_src TEXT NOT NULL DEFAULT 'default',
    deadline    TEXT NOT NULL DEFAULT '',
    pending     INTEGER NOT NULL DEFAULT 0,
    habit_freq    TEXT    NOT NULL DEFAULT '',
    habit_count   INTEGER NOT NULL DEFAULT 1,
    habit_times   TEXT    NOT NULL DEFAULT '[]',
    habit_history TEXT    NOT NULL DEFAULT '{}',
    plan          TEXT    NOT NULL DEFAULT '',
    plan_src      TEXT    NOT NULL DEFAULT 'auto'
  );
`);

// Migrate databases created before newer columns existed: add any that are
// missing so older board.db files keep working untouched. deleted_at is NULL
// for a live card and a timestamp once it has been soft-deleted (trashed).
const columnNames = new Set(db.prepare('PRAGMA table_info(cards)').all().map((c) => c.name));
if (!columnNames.has('importance')) db.exec("ALTER TABLE cards ADD COLUMN importance TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('urgency')) db.exec("ALTER TABLE cards ADD COLUMN urgency TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('deleted_at')) db.exec('ALTER TABLE cards ADD COLUMN deleted_at INTEGER');
if (!columnNames.has('type')) db.exec("ALTER TABLE cards ADD COLUMN type TEXT NOT NULL DEFAULT 'question'");
if (!columnNames.has('category')) db.exec("ALTER TABLE cards ADD COLUMN category TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('effort')) db.exec("ALTER TABLE cards ADD COLUMN effort TEXT NOT NULL DEFAULT 'medium'");
if (!columnNames.has('control')) db.exec("ALTER TABLE cards ADD COLUMN control TEXT NOT NULL DEFAULT 'influence'");
if (!columnNames.has('effort_src')) db.exec("ALTER TABLE cards ADD COLUMN effort_src TEXT NOT NULL DEFAULT 'default'");
if (!columnNames.has('control_src')) db.exec("ALTER TABLE cards ADD COLUMN control_src TEXT NOT NULL DEFAULT 'default'");
if (!columnNames.has('deadline')) db.exec("ALTER TABLE cards ADD COLUMN deadline TEXT NOT NULL DEFAULT ''");
// pending = 1 is a card the Assistant proposed and the user has not accepted
// yet: stored durably, but off the board until confirmed.
if (!columnNames.has('pending')) db.exec('ALTER TABLE cards ADD COLUMN pending INTEGER NOT NULL DEFAULT 0');
if (!columnNames.has('habit_freq')) db.exec("ALTER TABLE cards ADD COLUMN habit_freq TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('habit_count')) db.exec('ALTER TABLE cards ADD COLUMN habit_count INTEGER NOT NULL DEFAULT 1');
if (!columnNames.has('habit_times')) db.exec("ALTER TABLE cards ADD COLUMN habit_times TEXT NOT NULL DEFAULT '[]'");
if (!columnNames.has('habit_history')) db.exec("ALTER TABLE cards ADD COLUMN habit_history TEXT NOT NULL DEFAULT '{}'");
if (!columnNames.has('plan')) db.exec("ALTER TABLE cards ADD COLUMN plan TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('plan_src')) db.exec("ALTER TABLE cards ADD COLUMN plan_src TEXT NOT NULL DEFAULT 'auto'");
// Every card written before boards existed belongs to the default board, which
// the column default says without a single UPDATE. The REFERENCES clause is
// carried across too, so a migrated database and a fresh one have one schema
// rather than differing by a constraint only new files got.
if (!columnNames.has('board_id')) {
  // SQLite refuses ADD COLUMN with a REFERENCES clause and a non-NULL default
  // while foreign keys are enforced, because an existing row could violate the
  // constraint it is being given. (An empty table is accepted, which is why
  // this only ever shows up against a database with cards in it.) They go off
  // for the length of the one statement and straight back on, and the check
  // below asserts what the pragma was turned off to assume: every migrated card
  // now points at the seeded default board.
  db.exec('PRAGMA foreign_keys = OFF');
  db.exec("ALTER TABLE cards ADD COLUMN board_id TEXT NOT NULL DEFAULT 'main' REFERENCES boards(id)");
  db.exec('PRAGMA foreign_keys = ON');
  const orphans = db.prepare('PRAGMA foreign_key_check(cards)').all();
  if (orphans.length) {
    throw new Error(`${orphans.length} card(s) reference a board that does not exist`);
  }
}

// An edit the Assistant wants is a SUGGESTION, and it lives here rather than in
// `cards`. A pending row in `cards` would be a card — it would need a title, a
// ledger number, a place in Trash when discarded, and `readBoard` would have to
// learn to hide a second kind of thing. A suggestion is none of those: it is a
// note saying "these fields, on that card, if you agree". Nothing in this table
// can change a card. Only the user's own save does that, through the same
// whole-board PUT a hand edit goes through.
db.exec(`
  CREATE TABLE IF NOT EXISTS card_edits (
    id         TEXT PRIMARY KEY,
    card_id    TEXT    NOT NULL,
    fields     TEXT    NOT NULL,
    created_at INTEGER NOT NULL
  );
`);

// The user's category registry — per board since 2026-08-20. It was one shared
// table, and that shape is what let a category deleted on one board resurrect:
// every browser caches the registry per board, so a global registry was
// rewritten by whichever board's stale copy pushed last. Each board owns its
// rows now. Seeding happens ONLY when the table itself is first created or a
// board is — never from a zero count at boot, because a registry someone
// emptied on purpose is a real state, not a missing one.
const hadCategoriesTable = db.prepare(
  "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'categories'").get() !== undefined;
db.exec(`
  CREATE TABLE IF NOT EXISTS categories (
    board_id TEXT    NOT NULL DEFAULT 'main' REFERENCES boards(id),
    id       TEXT    NOT NULL,
    label    TEXT    NOT NULL,
    h        INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board_id, id)
  );
`);

function seedCategories(boardId) {
  const insert = db.prepare('INSERT INTO categories (board_id, id, label, h, position) VALUES (?, ?, ?, ?, ?)');
  DEFAULT_CATEGORIES.forEach((c, i) => insert.run(boardId, c.id, c.label, c.h, i));
}

// Migrate a shared-registry table: the primary key changes (id → board_id+id),
// which ALTER TABLE cannot do, so the table is rebuilt. Every board — deleted
// ones included, a restore must bring a board back whole — gets its own copy of
// the registry all of them showed yesterday; nothing changes on screen.
if (hadCategoriesTable
    && !db.prepare('PRAGMA table_info(categories)').all().some((c) => c.name === 'board_id')) {
  const shared = db.prepare('SELECT id, label, h, position FROM categories ORDER BY position ASC').all();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.exec('DROP TABLE categories');
    db.exec(`
      CREATE TABLE categories (
        board_id TEXT    NOT NULL DEFAULT 'main' REFERENCES boards(id),
        id       TEXT    NOT NULL,
        label    TEXT    NOT NULL,
        h        INTEGER NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (board_id, id)
      );
    `);
    const insert = db.prepare('INSERT INTO categories (board_id, id, label, h, position) VALUES (?, ?, ?, ?, ?)');
    for (const b of db.prepare('SELECT id FROM boards').all()) {
      shared.forEach((c) => insert.run(b.id, c.id, c.label, c.h, c.position));
    }
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
} else if (!hadCategoriesTable) {
  for (const b of db.prepare('SELECT id FROM boards').all()) seedCategories(b.id);
}

function readCategories(boardId) {
  return db.prepare('SELECT id, label, h FROM categories WHERE board_id = ? ORDER BY position ASC')
    .all(boardId).map((r) => ({ id: r.id, label: r.label, h: r.h }));
}

/** Replace one board's registry — it's config, not card data, so unlike cards
 *  it has no soft-delete: removing a category never touches any card row. */
function writeCategories(cats, boardId) {
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM categories WHERE board_id = ?').run(boardId);
    const insert = db.prepare('INSERT INTO categories (board_id, id, label, h, position) VALUES (?, ?, ?, ?, ?)');
    cats.forEach((c, i) => insert.run(boardId, c.id, c.label, c.h, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

const categoryIds = (boardId) => new Set(
  db.prepare('SELECT id FROM categories WHERE board_id = ?').all(boardId).map((r) => r.id));

/**
 * The additive half of the registry write, for a save that is based on a board
 * this one has moved past (see the `rev` check on PUT /api/state): insert the
 * ids this board lacks, and touch nothing else — no delete, no reorder, no
 * relabel.
 *
 * Doing nothing at all would be worse than this, not safer. `cleanCard` blanks
 * a `category` the registry does not know, and the registry is written before
 * the cards on purpose, so a client that added a life area while it was out of
 * date would get every card referencing it back with an empty category — a
 * field wipe in the one table that has no Trash.
 */
function mergeCategories(cats, boardId) {
  const have = categoryIds(boardId);
  const missing = cats.filter((c) => !have.has(c.id)).slice(0, Math.max(0, CAT_LIMIT - have.size));
  if (!missing.length) return;
  const next = db.prepare('SELECT COALESCE(MAX(position), -1) AS p FROM categories WHERE board_id = ?').get(boardId).p;
  const insert = db.prepare('INSERT INTO categories (board_id, id, label, h, position) VALUES (?, ?, ?, ?, ?)');
  db.exec('BEGIN IMMEDIATE');
  try {
    missing.forEach((c, i) => insert.run(boardId, c.id, c.label, c.h, next + 1 + i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}


// --------------------------------------------------------------------------
// Boards
// --------------------------------------------------------------------------
// One database, several boards. Cards, chats and categories all carry a
// board_id. The registry was shared at first ("colour means category
// everywhere"), but in practice one board's categories leaking onto every
// other read as a bug, and the shared table made deletions un-stickable —
// see the migration note above the categories table.

const BOARD_NAME_MAX = 60;
const newBoardId = () => 'b-' + Math.random().toString(36).slice(2) + Date.now().toString(36);

/** A board's name, or '' if it hasn't got a usable one. Blank refuses: an
 *  unnamed row in the picker is a board you can see and cannot choose. */
const boardName = (raw) => (typeof raw === 'string' ? raw.trim().slice(0, BOARD_NAME_MAX) : '');

const rowToBoard = (r) => ({
  id: r.id, name: r.name, position: r.position,
  createdAt: r.created_at, updatedAt: r.updated_at,
  ...(r.deleted_at ? { deletedAt: r.deleted_at } : {}),
  ...(r.n === undefined ? {} : { cardCount: r.n }),
});

// Counted in SQL for the same reason the chat list counts its messages there:
// the picker shows every board, and reading every board to render a list is how
// a list gets slow exactly when the feature starts being useful.
const BOARD_CARD_COUNT = `
  (SELECT COUNT(*) FROM cards c
    WHERE c.board_id = b.id AND c.deleted_at IS NULL AND c.pending = 0) AS n`;

function readBoards() {
  return db.prepare(
    `SELECT b.*, ${BOARD_CARD_COUNT} FROM boards b
      WHERE b.deleted_at IS NULL ORDER BY b.position ASC, b.created_at ASC`)
    .all().map(rowToBoard);
}

/** Deleted boards, newest deletion first — the picker's own trash. */
function readBoardsTrash() {
  return db.prepare(
    `SELECT b.*, ${BOARD_CARD_COUNT} FROM boards b
      WHERE b.deleted_at IS NOT NULL ORDER BY b.deleted_at DESC`)
    .all().map(rowToBoard);
}

/** The board a request that names none is answered with: the first live one.
 *  Deliberately not the constant — `main` can itself be deleted once there is
 *  somewhere else to go, and every caller written before boards existed must
 *  keep addressing a real board rather than a stamped one. */
function defaultBoardId() {
  const row = db.prepare(
    'SELECT id FROM boards WHERE deleted_at IS NULL ORDER BY position ASC, created_at ASC LIMIT 1').get();
  return row ? row.id : DEFAULT_BOARD_ID;
}

/** Which board this request is about. Returns null for a board that does not
 *  exist or has been deleted — the caller answers 400, because quietly serving
 *  another board's cards is how you edit the wrong board without noticing. */
function resolveBoard(url) {
  const raw = url.searchParams.get('board');
  if (!raw) return defaultBoardId();
  const row = db.prepare('SELECT id FROM boards WHERE id = ? AND deleted_at IS NULL').get(raw);
  return row ? row.id : null;
}

/** Same question, asked of a request body — the chat POST names its board there
 *  rather than in the query string, beside the session it also names. */
function resolveBoardId(raw) {
  if (raw === undefined || raw === null || raw === '') return defaultBoardId();
  if (typeof raw !== 'string') return null;
  const row = db.prepare('SELECT id FROM boards WHERE id = ? AND deleted_at IS NULL').get(raw);
  return row ? row.id : null;
}

function createBoard(rawName) {
  const name = boardName(rawName);
  if (!name) return null;
  const now = Date.now();
  const next = db.prepare('SELECT COALESCE(MAX(position), -1) + 1 AS p FROM boards').get().p;
  const id = newBoardId();
  db.prepare('INSERT INTO boards (id, name, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?)')
    .run(id, name, next, now, now);
  // A new board starts with the default life areas, not a copy of anyone
  // else's registry — creation is the one moment seeding is ever done.
  seedCategories(id);
  return rowToBoard({ id, name, position: next, created_at: now, updated_at: now, n: 0 });
}

function renameBoard(id, rawName) {
  const name = boardName(rawName);
  if (!name) return null;
  const { changes } = db.prepare(
    'UPDATE boards SET name = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL')
    .run(name, Date.now(), id);
  return changes ? readBoards().find((b) => b.id === id) ?? null : null;
}

const liveBoardCount = () =>
  db.prepare('SELECT COUNT(*) AS n FROM boards WHERE deleted_at IS NULL').get().n;

/** Soft-delete a board. Its cards and chats are not touched — this stamps one
 *  row, and every scoped read does the rest, which is what makes a restore
 *  bring the board back whole rather than partly.
 *
 *  Returns 'last' rather than deleting the only live board: a picker with
 *  nothing in it is a dead end, and it is never what anyone meant. */
function deleteBoard(id) {
  const live = db.prepare('SELECT id FROM boards WHERE id = ? AND deleted_at IS NULL').get(id);
  if (!live) return 'missing';
  if (liveBoardCount() <= 1) return 'last';
  db.prepare('UPDATE boards SET deleted_at = ? WHERE id = ?').run(Date.now(), id);
  return 'ok';
}

function restoreBoard(id) {
  const { changes } = db.prepare(
    'UPDATE boards SET deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL')
    .run(Date.now(), id);
  return changes ? readBoards().find((b) => b.id === id) ?? null : null;
}

/**
 * Erase a board and everything that belongs to it. This is the board-level
 * "Delete permanently", and `deleted_at IS NOT NULL` is load-bearing exactly as
 * it is for a card and for a chat message: only what is already in the trash can
 * be destroyed, so no single call both hides a board and erases it.
 *
 * The two databases are purged in their own transactions — they are separate
 * files and no transaction spans them. The board row goes last, so a failure
 * part-way leaves a board still in the trash rather than orphaned rows with
 * nothing to name them.
 */
function purgeBoard(id) {
  const stamped = db.prepare('SELECT id FROM boards WHERE id = ? AND deleted_at IS NOT NULL').get(id);
  if (!stamped) return null;

  const sessions = chatDb.prepare('SELECT id FROM sessions WHERE board_id = ?').all(id).map((r) => r.id);
  chatDb.exec('BEGIN IMMEDIATE');
  try {
    for (const sessionId of sessions) {
      chatDb.prepare('DELETE FROM messages WHERE session_id = ?').run(sessionId);
    }
    chatDb.prepare('DELETE FROM sessions WHERE board_id = ?').run(id);
    chatDb.exec('COMMIT');
  } catch (err) {
    chatDb.exec('ROLLBACK');
    throw err;
  }

  let cards = 0;
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`DELETE FROM card_edits WHERE card_id IN (SELECT id FROM cards WHERE board_id = ?)`).run(id);
    cards = db.prepare('DELETE FROM cards WHERE board_id = ?').run(id).changes;
    db.prepare('DELETE FROM categories WHERE board_id = ?').run(id);
    db.prepare('DELETE FROM boards WHERE id = ?').run(id);
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return { cards, sessions: sessions.length };
}

// --------------------------------------------------------------------------
// The chat record (assistant.db)
// --------------------------------------------------------------------------
// The assistant's transcript, given the same durability the board has — in its
// own file deliberately: the whole-board PUT /api/state soft-deletes every
// card it does not see, and one bad save must never sit next to two kinds of
// data. Chroma only ever holds chunks derived from these rows; this table is
// what a re-index rebuilds from.
//
// The record is cut into SESSIONS. Before it was, every turn carried a slice of
// one endless transcript plus its very first message as "framing", so a new
// question was answered in terms of the oldest one on the board. A session is
// the boundary that makes a conversation a conversation; the docs are in
// docs/decisions/2026-08-04-chat-sessions-design.md.
//
// Deleting is soft in both directions: `sessions.deleted_at` takes a whole
// chat's messages out of every live read, `messages.deleted_at` takes one turn
// out on its own. Chat has exactly one hard delete, and it is the board's shape
// — DELETE /api/chat/trash/:id, reachable only for a row already stamped, so no
// single call both hides a message and destroys it.

const ASSISTANT_DB_PATH = resolveAssistantDb({ root: ROOT, env: process.env });
mkdirSync(dirname(ASSISTANT_DB_PATH), { recursive: true });
// Same treatment as the board, for the same reason: two stacks now open this
// one file, and the chat record is written on every turn.
const chatDb = openDb(ASSISTANT_DB_PATH);
chatDb.exec(`
  CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
  );
`);

chatDb.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    board_id   TEXT    NOT NULL DEFAULT 'main',
    title      TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER
  );
`);

// A chat belongs to a board, and this column carries NO foreign key — `boards`
// lives in board.db and SQLite cannot reference across files. That separation
// is deliberate (the whole-board PUT must never sit next to two kinds of data),
// so the board id is checked by the server on the way in instead. `messages`
// needs no column of its own: a message belongs to a session, and the session
// knows its board.
const sessionColumns = new Set(chatDb.prepare('PRAGMA table_info(sessions)').all().map((c) => c.name));
if (!sessionColumns.has('board_id')) {
  chatDb.exec("ALTER TABLE sessions ADD COLUMN board_id TEXT NOT NULL DEFAULT 'main'");
}

// Same boot-time migration the board uses, for the same reason: an assistant.db
// written before sessions existed has to keep working untouched.
const chatColumns = new Set(chatDb.prepare('PRAGMA table_info(messages)').all().map((c) => c.name));
if (!chatColumns.has('session_id')) chatDb.exec("ALTER TABLE messages ADD COLUMN session_id TEXT NOT NULL DEFAULT ''");
// The assistant row's receipt. `steps` is the tool evidence, so reopening a
// historic chat shows what it was based on and not only its prose. `usage` and
// `cost` stay NULLABLE on purpose — pricing.py refuses to fabricate a zero, and
// a turn stored as cost 0 would be a measurement nobody made.
if (!chatColumns.has('steps')) chatDb.exec("ALTER TABLE messages ADD COLUMN steps TEXT NOT NULL DEFAULT '[]'");
if (!chatColumns.has('usage')) chatDb.exec('ALTER TABLE messages ADD COLUMN usage TEXT');
if (!chatColumns.has('cost')) chatDb.exec('ALTER TABLE messages ADD COLUMN cost REAL');

// The one index the query-plan evidence earned. Measured 2026-09-02 on a fresh
// schema filled with 20 sessions / 3000 messages, ANALYZE run first, each query
// prepared and run 200 times.
//
// It pays twice over. Reading ONE chat's transcript (`readChatSession`) went
// from `SCAN messages | USE TEMP B-TREE FOR ORDER BY` to `SEARCH messages USING
// INDEX messages_session (session_id=?)`, already in the wanted order:
// 0.295 ms → 0.165 ms a run, 1.8x. And the history panel's list
// (`readChatSessions`), whose per-chat `COUNT(*)` is a correlated subquery that
// scanned the whole of `messages` once per session, went 0.588 ms → 0.121 ms,
// 4.9x. That second number is the whole cost of opening the chats panel, and it
// grows with the message count — the one number in this schema that only ever
// goes up.
//
// `(session_id, created_at, id)` and not `session_id` alone, because both
// readers order by exactly that pair. The index then answers the ORDER BY as
// well as the predicate, which is what makes the temp B-tree disappear rather
// than merely shrink.
//
// NOTHING ELSE, and the negative result is written down here so the next person
// does not have to measure it again: `cards_board (board_id, position)` does
// improve `readBoard`'s plan — the temp B-tree goes away — and moves wall clock
// by 7%, 1.150 ms → 1.071 ms, which is inside the noise, because the query
// returns 360 of 2000 rows and the `SELECT *` row fetch dominates the scan it
// saves. On the trash query it buys nothing at all: `ORDER BY deleted_at DESC`
// needs a sort of its own either way. The rule this change works to is to add
// only the index the evidence identifies, and that is what "not identified"
// looks like.
//
// After the session_id migration above and never before it: an assistant.db
// written before sessions existed has no such column yet, and a CREATE INDEX
// naming one would raise at boot on a database that opens fine today.
chatDb.exec('CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, created_at, id)');

// The developer trace: one row per turn, holding the exact list of messages
// the brain handed the model. Created only when the trace is configured — a
// board with no dev key stores nothing, which is the strongest version of "the
// surface is absent" a table can have.
//
// Deliberately NOT the `messages` table. That one is the conversation: two
// roles, what the user said and what came back, and it is the thing the
// durability promise is about. This is the request that produced it — four
// roles, the system prompt, every tool result — and it is a debugging aid that
// must not be able to change what the transcript says.
if (DEV_KEY) {
  chatDb.exec(`
    CREATE TABLE IF NOT EXISTS traces (
      trace_id   TEXT PRIMARY KEY,
      session_id TEXT    NOT NULL,
      board_id   TEXT    NOT NULL DEFAULT '',
      status     TEXT    NOT NULL,
      model      TEXT    NOT NULL DEFAULT '',
      provider   TEXT    NOT NULL DEFAULT '',
      started_at INTEGER NOT NULL,
      ended_at   INTEGER,
      error      TEXT    NOT NULL DEFAULT '',
      usage      TEXT,
      entries    TEXT    NOT NULL,
      filed_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS traces_session ON traces (session_id, started_at);
  `);
}

const CHAT_ROLES = new Set(['user', 'assistant']);
// The chat every caller that names no session lands in. A curl, an eval, or a
// brain that was not told a session must still be recorded — losing the turn is
// the bug this record exists to prevent — and it must be visibly apart from a
// real conversation rather than mixed into one.
const ADHOC_SESSION = 'adhoc';
const ADHOC_TITLE = 'Unsessioned (API)';
// One per board, since a chat belongs to a board and an unsessioned turn is
// still a turn someone can go and read. The default board keeps the bare id:
// sixteen brain tests, the evals and every curl written before boards existed
// name no board, and their record must stay exactly where it has always been.
const adhocSessionId = (boardId) =>
  (boardId === DEFAULT_BOARD_ID ? ADHOC_SESSION : `${ADHOC_SESSION}-${boardId}`);
// What a pre-session record becomes. One session, so nothing is orphaned and
// `session_id` is never empty after boot: no read path needs a NULL branch.
const LEGACY_TITLE = 'Earlier conversations';
// A title is one line of the first message. Long enough to tell two chats
// apart, short enough that the history panel's rows stay one line each.
const TITLE_MAX = 60;

/** The title a chat gets from the message that opened it. Derived, never
 *  generated: a model call here would cost a request and a wait to improve on
 *  text the user just wrote, and it is the one place a paraphrase would sit
 *  permanently in the furniture. */
const chatTitleFrom = (content) =>
  String(content ?? '').split('\n')[0].trim().slice(0, TITLE_MAX) || 'New chat';

// Adopt a record written before sessions existed. Runs once: after it, no live
// message has an empty session_id, so the condition is false on every later boot.
{
  const orphans = chatDb.prepare(
    "SELECT id, content, created_at FROM messages WHERE session_id = '' ORDER BY created_at, id").all();
  if (orphans.length) {
    const first = orphans[0];
    const last = orphans[orphans.length - 1];
    const id = 'legacy-' + first.created_at;
    chatDb.exec('BEGIN IMMEDIATE');
    try {
      // Dated by its own messages, not by the migration: a chat that claims to
      // have started the moment you upgraded is a chat you cannot find again.
      chatDb.prepare(
        'INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)')
        .run(id, LEGACY_TITLE, first.created_at, last.created_at);
      chatDb.prepare("UPDATE messages SET session_id = ? WHERE session_id = ''").run(id);
      chatDb.exec('COMMIT');
    } catch (err) {
      chatDb.exec('ROLLBACK');
      throw err;
    }
  }
}

const rowToChatMessage = (r) => ({
  id: r.id,
  sessionId: r.session_id,
  // Present only where the read joined the session — the index needs it to
  // keep one board's conversations out of another's recall.
  ...(r.board_id ? { boardId: r.board_id } : {}),
  role: r.role,
  content: r.content,
  createdAt: r.created_at,
  steps: safeJson(r.steps, []),
  usage: safeJson(r.usage, null),
  cost: r.cost === null || r.cost === undefined ? null : r.cost,
});

// A message is live when neither it nor the chat holding it has been deleted.
// The join is the whole of what "deleting a chat" means to every other reader:
// the rows survive in the file, and nothing live returns them.
const LIVE_MESSAGES = `
  FROM messages m LEFT JOIN sessions s ON s.id = m.session_id
  WHERE m.deleted_at IS NULL AND s.deleted_at IS NULL`;

/** The live record. One board's, or — with no board named — every board's,
 *  which only the brain's index maintenance asks for. */
function readChatMessages(boardId) {
  // created_at before id, so an imported older transcript reads in order.
  const scope = boardId ? ' AND s.board_id = ?' : '';
  const args = boardId ? [boardId] : [];
  return chatDb.prepare(
    `SELECT m.*, s.board_id AS board_id ${LIVE_MESSAGES}${scope} ORDER BY m.created_at, m.id`)
    .all(...args).map(rowToChatMessage);
}

/** Every live chat, newest activity first — the history panel's list.
 *  messageCount is counted in SQL: the panel lists every chat, and reading
 *  every transcript to render a list is how a list becomes slow at exactly the
 *  point the feature becomes useful. */
function readChatSessions(boardId) {
  return chatDb.prepare(`
    SELECT s.id, s.title, s.created_at, s.updated_at,
           (SELECT COUNT(*) FROM messages m
             WHERE m.session_id = s.id AND m.deleted_at IS NULL) AS n
      FROM sessions s WHERE s.deleted_at IS NULL AND s.board_id = ?
      ORDER BY s.updated_at DESC, s.created_at DESC`)
    .all(boardId)
    .map((r) => ({ id: r.id, title: r.title, createdAt: r.created_at,
                   updatedAt: r.updated_at, messageCount: r.n }));
}

/** One chat and its whole transcript, or null when there is no such live chat. */
function readChatSession(id) {
  const s = chatDb.prepare(
    'SELECT id, title, created_at, updated_at FROM sessions WHERE id = ? AND deleted_at IS NULL').get(id);
  if (!s) return null;
  const messages = chatDb.prepare(
    `SELECT m.* ${LIVE_MESSAGES} AND m.session_id = ? ORDER BY m.created_at, m.id`)
    .all(id).map(rowToChatMessage);
  return {
    session: { id: s.id, title: s.title, createdAt: s.created_at,
               updatedAt: s.updated_at, messageCount: messages.length },
    messages,
  };
}

/** Rename a chat. An empty title refuses: a blank row in the history panel is
 *  a chat you can see and cannot name. Returns the session, or null. */
function renameChatSession(id, title) {
  const clean = typeof title === 'string' ? title.trim().slice(0, 200) : '';
  if (!clean) return null;
  const { changes } = chatDb.prepare(
    'UPDATE sessions SET title = ? WHERE id = ? AND deleted_at IS NULL').run(clean, id);
  return changes ? readChatSession(id)?.session ?? null : null;
}

/** Soft-delete a chat. The messages are untouched — this stamps the session,
 *  and the LIVE_MESSAGES join does the rest. Chroma still holds chunks derived
 *  from them, which is why the browser fires /api/rag/chat/reindex afterwards:
 *  ChatStore.prune is what makes the delete reach the index. */
function deleteChatSession(id) {
  const { changes } = chatDb.prepare(
    'UPDATE sessions SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL').run(Date.now(), id);
  return changes > 0;
}

// One turn, deleted on its own. The board's two-step, applied to chat: hiding a
// message and destroying it are different calls, so a misclick costs nothing.
// A message id is an INTEGER key — a path param arrives as text, and SQLite
// would compare '5' against 5 and match nothing, silently 404ing every delete.
const chatMessageId = (raw) => (/^\d+$/.test(String(raw)) ? Number(raw) : null);

/** Soft-delete one message. The row survives; it leaves every live read, which
 *  is also what takes it out of the recall index once /rag/chat/reindex runs. */
function deleteChatMessage(id) {
  const key = chatMessageId(id);
  if (key === null) return false;
  const { changes } = chatDb.prepare(
    'UPDATE messages SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL').run(Date.now(), key);
  return changes > 0;
}

/** The assistant's trash: turns deleted one at a time, newest first, each
 *  saying which chat it came from — a sentence out of its conversation is
 *  otherwise unplaceable.
 *
 *  Messages of a soft-deleted CHAT are deliberately absent. There the chat is
 *  the unit: listing its turns loose would bury real deletions under a whole
 *  transcript, and restoring one into a chat that cannot be opened would be a
 *  restore with nothing to show for it. */
function readChatTrash(boardId) {
  return chatDb.prepare(`
    SELECT m.*, s.title AS session_title
      FROM messages m JOIN sessions s ON s.id = m.session_id
     WHERE m.deleted_at IS NOT NULL AND s.deleted_at IS NULL AND s.board_id = ?
     ORDER BY m.deleted_at DESC, m.id DESC`)
    .all(boardId)
    .map((r) => ({ ...rowToChatMessage(r), deletedAt: r.deleted_at,
                   sessionTitle: r.session_title }));
}

/** Put one turn back. Order is by createdAt, so it returns to its own place in
 *  the transcript rather than to the end. */
function restoreChatMessage(id) {
  const key = chatMessageId(id);
  if (key === null) return false;
  const { changes } = chatDb.prepare(
    'UPDATE messages SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL').run(key);
  return changes > 0;
}

/** The one hard delete chat has. `deleted_at IS NOT NULL` is load-bearing: only
 *  what is already in the trash can be erased, so no single call both hides a
 *  turn and destroys it. The chat holding it is left alone — a chat is not its
 *  messages, and one erased turn must not take a conversation with it. */
function purgeChatMessage(id) {
  const key = chatMessageId(id);
  if (key === null) return false;
  return chatDb.prepare(
    'DELETE FROM messages WHERE id = ? AND deleted_at IS NOT NULL').run(key).changes > 0;
}

/** Append a batch of messages to one chat. All-or-nothing: one invalid row
 *  refuses the whole batch, so an import can never half-apply. createdAt is
 *  optional — imports keep their own timestamps, live turns are stamped here.
 *  The session row is upserted from the batch, so there is no "create session"
 *  call any writer could forget to make. Returns the inserted rows, or null
 *  when the batch is invalid. */
function appendChatMessages(list, sessionId, boardId) {
  if (!Array.isArray(list) || list.length === 0) return null;
  // A given session id must be a string: an object here would be stringified
  // into a chat nobody can name again.
  if (sessionId !== undefined && sessionId !== null && typeof sessionId !== 'string') return null;
  for (const m of list) {
    if (!m || typeof m !== 'object') return null;
    if (!CHAT_ROLES.has(m.role)) return null;
    if (typeof m.content !== 'string' || !m.content.trim()) return null;
    if (m.createdAt !== undefined && !Number.isFinite(m.createdAt)) return null;
    if (m.cost !== undefined && m.cost !== null && !Number.isFinite(m.cost)) return null;
  }
  const board = boardId || DEFAULT_BOARD_ID;
  const id = (typeof sessionId === 'string' && sessionId.trim()) || adhocSessionId(board);
  const insert = chatDb.prepare(`
    INSERT INTO messages (session_id, role, content, created_at, steps, usage, cost)
    VALUES (?, ?, ?, ?, ?, ?, ?)`);
  const saved = [];
  chatDb.exec('BEGIN IMMEDIATE');
  try {
    const existing = chatDb.prepare('SELECT id FROM sessions WHERE id = ?').get(id);
    const stamps = list.map((m) => m.createdAt ?? Date.now());
    if (!existing) {
      // Titled from the first USER message of the batch that opened the chat —
      // an assistant's greeting is not what the conversation is about.
      const opener = list.find((m) => m.role === 'user') ?? list[0];
      const title = id === adhocSessionId(board) ? ADHOC_TITLE : chatTitleFrom(opener.content);
      chatDb.prepare(
        'INSERT INTO sessions (id, board_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)')
        .run(id, board, title, Math.min(...stamps), Math.max(...stamps));
    }
    for (const [i, m] of list.entries()) {
      const createdAt = stamps[i];
      const steps = JSON.stringify(Array.isArray(m.steps) ? m.steps : []);
      const usage = m.usage && typeof m.usage === 'object' ? JSON.stringify(m.usage) : null;
      const cost = Number.isFinite(m.cost) ? m.cost : null;
      const { lastInsertRowid } = insert.run(id, m.role, m.content, createdAt, steps, usage, cost);
      saved.push({ id: Number(lastInsertRowid), sessionId: id, role: m.role,
                   content: m.content, createdAt,
                   steps: JSON.parse(steps), usage: safeJson(usage, null), cost });
    }
    // updatedAt follows the newest message, which is what makes the history
    // panel read as "what I am working on" and not "what I started". A later
    // turn never retitles the chat.
    chatDb.prepare(
      'UPDATE sessions SET updated_at = ? WHERE id = ? AND updated_at < ?')
      .run(Math.max(...stamps), id, Math.max(...stamps));
    chatDb.exec('COMMIT');
  } catch (err) {
    chatDb.exec('ROLLBACK');
    throw err;
  }
  return saved;
}

const rowToCard = (r, catIds) => ({
  id: r.id,
  columnId: r.column_id,
  title: r.title,
  notes: r.notes,
  type: typeVal(r.type),
  category: catIds.has(r.category) ? r.category : '',
  importance: r.importance || '',
  urgency: r.urgency || '',
  effort: effortVal(r.effort),
  control: controlVal(r.control),
  effortSrc: srcVal(r.effort_src),
  controlSrc: srcVal(r.control_src),
  deadline: deadlineVal(r.deadline),
  plan: resolvePlan(r.plan, r.plan_src, r.deadline),
  planSrc: planSrcVal(r.plan_src),
  habitFreq: habitFreqVal(r.habit_freq),
  habitCount: habitCountVal(r.habit_count),
  habitTimes: habitTimesVal(safeJson(r.habit_times, []), habitCountVal(r.habit_count)),
  habitHistory: habitHistoryVal(safeJson(r.habit_history, {})),
  num: r.num,
  tags: safeTags(r.tags),
  createdAt: r.created_at,
  updatedAt: r.updated_at,
});

function safeTags(json) {
  try {
    const t = JSON.parse(json);
    return Array.isArray(t) ? t.map(String) : [];
  } catch {
    return [];
  }
}

// The live board is the cards that are neither soft-deleted nor still
// awaiting the user's approval.
function readBoard(boardId) {
  const catIds = categoryIds(boardId);
  const rows = db.prepare(
    'SELECT * FROM cards WHERE board_id = ? AND deleted_at IS NULL AND pending = 0 ORDER BY position ASC')
    .all(boardId);
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)), categories: readCategories(boardId) };
}

/**
 * Name the board a client is looking at, so a later save can say which board it
 * was based on. The hash is taken over the exact bytes that client was sent, so
 * it has no blind spot by construction: any difference the client could see is a
 * different `rev`.
 *
 * Alternatives considered. A SQL aggregate (row count + count of trashed rows +
 * MAX(updated_at) + category count) needs no serialising, but two of its blind
 * spots are this feature's whole reason for existing: `updated_at` comes from
 * the client's own clock, so an edit made by a laptop running a minute behind
 * leaves every term unchanged, and a category *rename* changes no count at all.
 * A monotonic `rev` column on `boards` would be exact, but it has to be bumped
 * by every path that touches a card — the whole-board save, a proposal confirm,
 * a purge, a restore — and the day one of them forgets, deletion silently stops
 * working with nothing to notice it. Hashing what was actually sent cannot be
 * forgotten. sha1 (not sha256) and 16 hex chars because this is a
 * change-detector, never a security claim.
 */
const revOf = (board) => createHash('sha1').update(JSON.stringify(board)).digest('hex').slice(0, 16);

/** The board plus the name of this exact version of it. Every read a client can
 *  adopt from has to go through here: a client that adopts a board without a
 *  rev believes it is up to date while claiming a rev the server has moved past,
 *  and from then on every one of its deletions is refused in silence. */
const boardWithRev = (boardId) => {
  const board = readBoard(boardId);
  return { ...board, rev: revOf(board) };
};

// Cards the Assistant proposed, oldest first, still waiting to be accepted.
function readProposals(boardId) {
  const catIds = categoryIds(boardId);
  const rows = db.prepare(
    'SELECT * FROM cards WHERE board_id = ? AND deleted_at IS NULL AND pending = 1 ORDER BY created_at ASC')
    .all(boardId);
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)) };
}

// The Trash is the soft-deleted cards, newest deletion first. They are still
// in the database and can be restored (re-added by the client) until purged.
function readTrash(boardId) {
  const catIds = categoryIds(boardId);
  const rows = db.prepare(
    'SELECT * FROM cards WHERE board_id = ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC')
    .all(boardId);
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)) };
}

/** Validate and coerce one incoming card; returns null if it has no title. */
function cleanCard(raw, now, catIds) {
  if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) {
    return null;
  }
  const habitCount = habitCountVal(raw.habitCount);
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : cryptoId(),
    columnId: COLUMN_IDS.includes(raw.columnId) ? raw.columnId : 'inbox',
    title: raw.title.trim(),
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    type: typeVal(raw.type),
    category: catIds.has(raw.category) ? raw.category : '',
    importance: iuVal(raw.importance),
    urgency: iuVal(raw.urgency),
    effort: effortVal(raw.effort),
    control: controlVal(raw.control),
    effortSrc: srcVal(raw.effortSrc),
    controlSrc: srcVal(raw.controlSrc),
    deadline: deadlineVal(raw.deadline),
    plan: resolvePlan(raw.plan, raw.planSrc, raw.deadline),
    planSrc: planSrcVal(raw.planSrc),
    habitFreq: habitFreqVal(raw.habitFreq),
    habitCount,
    habitTimes: habitTimesVal(raw.habitTimes, habitCount),
    habitHistory: habitHistoryVal(raw.habitHistory),
    num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
    tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
    createdAt: Number.isFinite(raw.createdAt) ? raw.createdAt : now,
    updatedAt: Number.isFinite(raw.updatedAt) ? raw.updatedAt : now,
  };
}

const cryptoId = () => 'id-' + Math.random().toString(36).slice(2) + Date.now().toString(36);

/**
 * Reconcile the stored board with the `cards` the client currently shows:
 * upsert every card present, and SOFT-delete (archive) any live row that is
 * absent. Nothing is ever hard-deleted here — that is the whole safety net, so
 * a partial or accidental save can never destroy a card; it only moves to
 * the Trash, from where it can be restored. Upserting a card clears its
 * deleted_at, so re-adding or restoring a card brings it back to life.
 *
 * Returns { board, created } — `created` is how many of these cards the database
 * had never seen, which is what triggers a backup.
 *
 * Everything here is scoped to ONE board. The sweep is where that matters most:
 * unscoped, a keystroke on this board would archive every card on every other
 * one, which is the worst thing this file could be made to do.
 */
function writeBoard(cards, boardId) {
  const now = Date.now();
  const catIds = categoryIds(boardId);
  const clean = cards.map((c) => cleanCard(c, now, catIds)).filter(Boolean);
  const keep = new Set(clean.map((c) => c.id));

  // Deliberately every row, not just the live ones: a card restored from the
  // Trash has an id the table already knows, and bringing back an old thought
  // is not the same as capturing a new one.
  const known = new Set(db.prepare('SELECT id FROM cards').all().map((r) => r.id));
  const created = clean.reduce((n, c) => (known.has(c.id) ? n : n + 1), 0);

  const softDelete = db.prepare('UPDATE cards SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL');
  const upsert = db.prepare(`
    INSERT INTO cards (id, board_id, column_id, title, notes, type, category, importance, urgency,
                       effort, control, effort_src, control_src, deadline,
                       habit_freq, habit_count, habit_times, habit_history, plan, plan_src,
                       num, tags, created_at, updated_at, position, deleted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
    ON CONFLICT(id) DO UPDATE SET
      column_id = excluded.column_id, title = excluded.title, notes = excluded.notes,
      type = excluded.type, category = excluded.category,
      importance = excluded.importance, urgency = excluded.urgency,
      effort = excluded.effort, control = excluded.control,
      effort_src = excluded.effort_src, control_src = excluded.control_src,
      deadline = excluded.deadline,
      habit_freq = excluded.habit_freq, habit_count = excluded.habit_count,
      habit_times = excluded.habit_times, habit_history = excluded.habit_history,
      plan = excluded.plan, plan_src = excluded.plan_src,
      num = excluded.num, tags = excluded.tags,
      created_at = excluded.created_at, updated_at = excluded.updated_at, position = excluded.position,
      deleted_at = NULL
  `);
  // NOTE: `pending` is deliberately absent from both the column list and the
  // conflict SET, so a board save can neither create a proposal nor silently
  // accept one. Only /api/proposals/:id/confirm clears that flag. `board_id` is
  // absent from the SET for the same shape of reason: a card that already exists
  // keeps the board it is on, so no whole-board save can move a card between
  // boards. Moving one is a feature with its own questions to answer, not a
  // side effect of a save.

  db.exec('BEGIN IMMEDIATE');
  try {
    // `AND pending = 0` is load-bearing: the browser cannot see proposals, so it
    // never sends them, and without this clause every save would archive them.
    for (const { id } of db.prepare(
      'SELECT id FROM cards WHERE board_id = ? AND deleted_at IS NULL AND pending = 0').all(boardId)) {
      if (!keep.has(id)) softDelete.run(now, id);
    }
    clean.forEach((c, i) =>
      upsert.run(c.id, boardId, c.columnId, c.title, c.notes, c.type, c.category, c.importance, c.urgency,
        c.effort, c.control, c.effortSrc, c.controlSrc, c.deadline,
        c.habitFreq, c.habitCount, JSON.stringify(c.habitTimes), JSON.stringify(c.habitHistory),
        c.plan, c.planSrc,
        c.num, JSON.stringify(c.tags), c.createdAt, c.updatedAt, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  // A save can trash the card a suggestion points at. Cleared here rather than
  // filtered on read, so the row does not linger and reappear if the card is
  // later restored from Trash carrying an edit the user never saw.
  pruneOrphanedEdits();
  return { board: readBoard(boardId), created };
}

/**
 * Apply a save that is based on a board this one has already moved past —
 * `rev` did not match on PUT /api/state — and do it without ever removing
 * anything. The whole-board sweep in `writeBoard` reads an absent card as
 * "the user deleted this"; from a client that has not seen the current board
 * that reading is simply wrong, and on 2026-08-22 it archived 24 cards a second
 * machine had never heard of.
 *
 * The rules, and why each one:
 *   - A card this board does not have is inserted, positioned after the last
 *     one. That is the offline work the client is here to deliver.
 *   - A card both sides have is updated only when the incoming `updatedAt` is
 *     not older than the stored one — otherwise a stale copy quietly reverts a
 *     newer title and there is no Trash entry to notice, which is the same loss
 *     as the sweep wearing a different hat.
 *   - `position` is never touched for a row that already exists, and neither is
 *     the ledger `num`. Order is re-derived from array index on every save
 *     without bumping `updatedAt`, so a stale client's ordering carries no
 *     information; a number is permanent by definition.
 *   - A card whose row is in the Trash stays in the Trash. This is the one that
 *     makes deletion work at all between two machines: resurrecting it would
 *     mean a card deleted here comes back the moment the other laptop saves.
 *   - Proposals (`pending = 1`) are invisible to the browser, so a row that is
 *     one is left entirely alone.
 *
 * Alternatives considered. The natural way to write "only if newer" is a
 * conditional upsert — `ON CONFLICT DO UPDATE … WHERE excluded.updated_at >=
 * cards.updated_at` — one statement, no read. It is wrong here for a reason
 * that is invisible in the SQL: the same statement writes `position`, which is
 * re-derived from array order on every save and never bumps `updatedAt`, so the
 * guard would silently drop legitimate reorders along with stale content.
 * Comparing in JavaScript is what lets content be guarded and order be left
 * alone. A CRDT or a per-field vector clock would remove the last-write-wins
 * guess entirely, and is the honest answer if this board ever gets simultaneous
 * editors; for two laptops that take turns it is a large amount of machinery to
 * decide something a timestamp already decides, and it would have to survive
 * `PUT /api/state` being a whole document rather than a stream of operations.
 *
 * Returns { board, created } like `writeBoard`, so the route treats them alike.
 */
function mergeBoard(cards, boardId) {
  const now = Date.now();
  const catIds = categoryIds(boardId);
  const clean = cards.map((c) => cleanCard(c, now, catIds)).filter(Boolean);

  // Every row of this board, live and trashed alike: the trashed ones are what
  // stop a stale save from undoing a deletion.
  const rows = new Map(db.prepare(
    'SELECT id, updated_at, deleted_at, pending FROM cards WHERE board_id = ?')
    .all(boardId).map((r) => [r.id, r]));
  let nextPos = db.prepare(
    'SELECT COALESCE(MAX(position), -1) AS p FROM cards WHERE board_id = ?').get(boardId).p + 1;

  const insert = db.prepare(`
    INSERT INTO cards (id, board_id, column_id, title, notes, type, category, importance, urgency,
                       effort, control, effort_src, control_src, deadline,
                       habit_freq, habit_count, habit_times, habit_history, plan, plan_src,
                       num, tags, created_at, updated_at, position, deleted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
    ON CONFLICT(id) DO NOTHING
  `);
  const update = db.prepare(`
    UPDATE cards SET
      column_id = ?, title = ?, notes = ?, type = ?, category = ?,
      importance = ?, urgency = ?, effort = ?, control = ?,
      effort_src = ?, control_src = ?, deadline = ?,
      habit_freq = ?, habit_count = ?, habit_times = ?, habit_history = ?,
      plan = ?, plan_src = ?,
      tags = ?, updated_at = ?
    WHERE id = ? AND deleted_at IS NULL AND pending = 0
  `);

  let created = 0;
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const c of clean) {
      const row = rows.get(c.id);
      if (!row) {
        // DO NOTHING, not an upsert: `rows` is scoped to this board, so an id
        // that is already on a *different* one would otherwise raise on the
        // primary key and roll the whole save back. A card keeps the board it
        // is on here for the same reason it does in `writeBoard`.
        const { changes } = insert.run(c.id, boardId, c.columnId, c.title, c.notes, c.type, c.category,
          c.importance, c.urgency, c.effort, c.control, c.effortSrc, c.controlSrc, c.deadline,
          c.habitFreq, c.habitCount, JSON.stringify(c.habitTimes), JSON.stringify(c.habitHistory),
          c.plan, c.planSrc,
          c.num, JSON.stringify(c.tags), c.createdAt, c.updatedAt, nextPos++);
        if (changes) created += 1;
        continue;
      }
      if (row.deleted_at !== null || row.pending) continue;
      if (c.updatedAt < row.updated_at) continue;
      update.run(c.columnId, c.title, c.notes, c.type, c.category, c.importance, c.urgency,
        c.effort, c.control, c.effortSrc, c.controlSrc, c.deadline,
        c.habitFreq, c.habitCount, JSON.stringify(c.habitTimes), JSON.stringify(c.habitHistory),
        c.plan, c.planSrc,
        JSON.stringify(c.tags), c.updatedAt, c.id);
    }
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  pruneOrphanedEdits();
  return { board: readBoard(boardId), created };
}

/**
 * Store a card the Assistant proposed. It is durable immediately — losing a
 * suggestion to a crash would be its own kind of data loss — but `pending = 1`
 * keeps it off the board until the user accepts it. Returns the stored proposal,
 * or null if the card had no usable title.
 */
function writeProposal(raw, boardId) {
  const now = Date.now();
  const catIds = categoryIds(boardId);
  const card = cleanCard(raw, now, catIds);
  if (!card) return null;
  db.prepare(`
    INSERT INTO cards (id, board_id, column_id, title, notes, type, category, importance, urgency,
                       effort, control, effort_src, control_src, deadline,
                       habit_freq, habit_count, habit_times, habit_history,
                       num, tags, created_at, updated_at, position, deleted_at, pending)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
  `).run(card.id, boardId, card.columnId, card.title, card.notes, card.type, card.category,
    card.importance, card.urgency, card.effort, card.control, card.effortSrc,
    card.controlSrc, card.deadline,
    card.habitFreq, card.habitCount,
    JSON.stringify(card.habitTimes), JSON.stringify(card.habitHistory),
    // num stays 0: a ledger number is earned at confirmation, so a rejected
    // proposal never burns one.
    0, JSON.stringify(card.tags), card.createdAt, card.updatedAt, 0);
  return rowToCard(db.prepare('SELECT * FROM cards WHERE id = ?').get(card.id), catIds);
}

/**
 * Accept a proposal: it becomes an ordinary board card. Returns the id of the
 * board it landed on — the route answers with that board, and a proposal knows
 * which one it belongs to, so accepting one never needs to be told. Returns null
 * if there is no such pending card, so confirming twice (or confirming something
 * already live) is a 404 rather than a silent no-op.
 */
function confirmProposal(id) {
  const row = db.prepare(
    'SELECT board_id FROM cards WHERE id = ? AND pending = 1 AND deleted_at IS NULL').get(id);
  if (!row) return null;
  db.prepare('UPDATE cards SET pending = 0, updated_at = ? WHERE id = ?').run(Date.now(), id);
  return row.board_id;
}

// Fields a suggestion may name — the set `update_card` can touch, so a
// suggestion is not a new way into anything: habit history and the ledger
// number are absent here exactly as they are absent there.
//
// `deadline` was missing until 2026-09-02, and the tool has advertised it all
// along: the model named a due date, this filter dropped it, and the Assistant
// then said it had suggested one when nothing had been stored. A suggestion
// naming only a deadline was a 400 the user never saw. `plan` is still absent
// deliberately — a suggested plan needs `planSrc` to say a person chose it, or
// the save resolves the plan back to the deadline and discards it, and whether
// a suggestion may claim that provenance is its own question.
const EDITABLE_FIELDS = ['title', 'notes', 'type', 'category', 'columnId',
  'importance', 'urgency', 'deadline', 'tags'];

/**
 * Store a suggested edit. Returns null when there is nothing to suggest or
 * nothing to suggest it about: a suggestion pointing at a card that is not on the
 * board would surface in the Assistant with nothing to apply to.
 */
function writeEdit(raw) {
  const cardId = String(raw?.cardId ?? '');
  const live = db.prepare(
    'SELECT id FROM cards WHERE id = ? AND deleted_at IS NULL AND pending = 0').get(cardId);
  if (!live) return null;
  const fields = {};
  for (const key of EDITABLE_FIELDS) {
    if (raw?.fields && Object.hasOwn(raw.fields, key)) fields[key] = raw.fields[key];
  }
  if (!Object.keys(fields).length) return null;
  const row = { id: cryptoId(), cardId, fields, createdAt: Date.now() };
  db.prepare('INSERT INTO card_edits (id, card_id, fields, created_at) VALUES (?, ?, ?, ?)')
    .run(row.id, row.cardId, JSON.stringify(row.fields), row.createdAt);
  return row;
}

/** Suggestions still worth showing: oldest first, like the proposal list.
 *  Scoped through the card rather than by a column of its own — a suggestion
 *  points at a card, and that card already knows which board it is on. */
function readEdits(boardId) {
  return db.prepare(`
    SELECT e.* FROM card_edits e JOIN cards c ON c.id = e.card_id
     WHERE c.board_id = ? ORDER BY e.created_at ASC`).all(boardId)
    .map((r) => ({ id: r.id, cardId: r.card_id, fields: JSON.parse(r.fields),
      createdAt: r.created_at }));
}

/**
 * Drop a suggestion — the user applied it (their save already did the writing) or
 * dismissed it. A hard delete, and deliberately not a second exception to the
 * durability promise: that promise is about *cards*, and a suggestion the user
 * has answered is not one. Nothing the user wrote is in here.
 */
function discardEdit(id) {
  return db.prepare('DELETE FROM card_edits WHERE id = ?').run(id).changes > 0;
}

/** Suggestions whose card has left the board have nothing to apply to. */
function pruneOrphanedEdits() {
  db.exec(`
    DELETE FROM card_edits WHERE card_id NOT IN
      (SELECT id FROM cards WHERE deleted_at IS NULL AND pending = 0)
  `);
}

/**
 * Decline a proposal. It goes to the Trash, recoverable, rather than being
 * erased — DELETE /api/cards/:id stays the board's only hard delete.
 *
 * `pending` is cleared as well as `deleted_at` set: leaving it at 1 would mean a
 * restore from Trash brought back a row still invisible to the board, and the
 * restore would look like it had silently failed.
 */
function rejectProposal(id) {
  const now = Date.now();
  return db.prepare(
    'UPDATE cards SET pending = 0, deleted_at = ?, updated_at = ? WHERE id = ? AND pending = 1 AND deleted_at IS NULL',
  ).run(now, now, id).changes > 0;
}

// A backup child is running. Overlapping writes skip the spawn rather than
// stacking up processes; the next new card takes the next snapshot.
let backupInFlight = false;

/**
 * Snapshot the database because a new card was just captured.
 *
 * Runs in a DETACHED CHILD PROCESS, never inline: runBackup shells out to
 * rclone with spawnSync, and this server is single-threaded, so an inline call
 * would freeze every other request for the length of a Google Drive upload.
 * Called after the response is sent and after the transaction commits, so the
 * snapshot contains the card that triggered it.
 */
function backupAfterNewCards() {
  if (!BACKUP_ON_WRITE || backupInFlight) return;
  backupInFlight = true;
  try {
    const child = spawn(process.execPath, [BACKUP_SCRIPT], {
      // BOARD_DB is explicit so the child snapshots the database THIS server
      // opened — otherwise the :3001 test board would back up board.db.
      env: { ...process.env, BOARD_DB: DB_PATH },
      detached: true,
      stdio: 'ignore',
    });
    child.on('exit', () => { backupInFlight = false; });
    child.on('error', () => { backupInFlight = false; });
    child.unref();
  } catch {
    backupInFlight = false; // a failed backup must never break a save
  }
}

/**
 * Permanently remove one card from the database. This is the deliberate
 * second step ("delete from History") and the only operation that truly erases
 * data. Returns true if a row was removed.
 */
// The only statement in this file that truly erases a card, and the predicate
// is half of the promise: `deleted_at IS NOT NULL` means a card can be
// destroyed only after it has been put in the Trash. Without it this route
// deleted by id alone, so a single mistaken call — a stale browser, a typo in
// curl, a future caller that thinks DELETE means "remove from the board" —
// erased a live card with no second step and no way back. The browser has
// always asked twice; that was never a guarantee, because the browser is not a
// security boundary and never was the only thing that can call this.
function purgeCard(id) {
  return db.prepare('DELETE FROM cards WHERE id = ? AND deleted_at IS NOT NULL')
    .run(id).changes > 0;
}

// --------------------------------------------------------------------------
// The developer trace
// --------------------------------------------------------------------------
//
// What the brain files, and the page that reads it back. Everything here is
// inert unless LODESTAR_DEV_KEY is set: the table is not created, the routes
// answer 404, and nothing about the board changes.
//
// The trace is read-only in the strongest sense — no route below writes a card,
// a message, a session or a proposal — and every response carries no-store,
// because the whole point of looking at a trace is to see the state a turn is
// in *now*, and a cached copy of that is a wrong answer wearing a right one's
// clothes.

const TRACE_STATUSES = new Set(['in_flight', 'completed', 'failed', 'interrupted']);
const TRACE_ROLES = new Set(['system', 'human', 'ai', 'tool']);

/** One filed trace, validated. Returns null for anything that is not one —
 *  the brain is the only caller, but a store is not the place to find that
 *  out. */
function cleanTrace(body) {
  if (!body || typeof body !== 'object') return null;
  const id = String(body.trace_id || '').trim();
  const session = String(body.session_id || '').trim();
  if (!id || !session) return null;
  if (!TRACE_STATUSES.has(body.status)) return null;
  if (!Array.isArray(body.entries)) return null;
  const entries = body.entries
    .filter((e) => e && TRACE_ROLES.has(e.role))
    .map((e, i) => ({
      seq: Number.isInteger(e.seq) ? e.seq : i,
      role: e.role,
      content: typeof e.content === 'string' ? e.content : '',
      metadata: e.metadata && typeof e.metadata === 'object' ? e.metadata : {},
    }));
  return {
    trace_id: id,
    session_id: session,
    board_id: String(body.board_id || ''),
    status: body.status,
    model: String(body.model || ''),
    provider: String(body.provider || ''),
    started_at: Number(body.started_at) || Date.now(),
    ended_at: Number.isFinite(Number(body.ended_at)) && body.ended_at !== null
      ? Number(body.ended_at) : null,
    error: String(body.error || ''),
    usage: body.usage ? JSON.stringify(body.usage) : null,
    entries: JSON.stringify(entries),
  };
}

/** File one turn's trace. An upsert on `trace_id`, because the brain files the
 *  same turn twice — once in flight, once settled — and one turn is one
 *  record. Everything but the id is replaced: the settled write is the fuller
 *  one by construction. */
function writeTrace(trace) {
  chatDb.prepare(`
    INSERT INTO traces (trace_id, session_id, board_id, status, model, provider,
                        started_at, ended_at, error, usage, entries, filed_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(trace_id) DO UPDATE SET
      session_id = excluded.session_id, board_id = excluded.board_id,
      status = excluded.status, model = excluded.model,
      provider = excluded.provider, started_at = excluded.started_at,
      ended_at = excluded.ended_at, error = excluded.error,
      usage = excluded.usage, entries = excluded.entries,
      filed_at = excluded.filed_at
  `).run(trace.trace_id, trace.session_id, trace.board_id, trace.status,
    trace.model, trace.provider, trace.started_at, trace.ended_at,
    trace.error, trace.usage, trace.entries, Date.now());
}

/** The sessions that have traces, newest activity first. Joined to `sessions`
 *  only for a title — a trace outlives the chat it came from (a deleted chat
 *  is exactly the one somebody debugs), so the join must not decide whether the
 *  row is listed. */
function readTraceSessions() {
  return chatDb.prepare(`
    SELECT t.session_id AS id,
           COUNT(*)     AS prompts,
           MAX(t.started_at) AS latest,
           (SELECT title FROM sessions s WHERE s.id = t.session_id) AS title
    FROM traces t GROUP BY t.session_id ORDER BY latest DESC LIMIT 200
  `).all();
}

/** Every trace of one session, oldest first — the order the prompts happened
 *  in, which is the order a tape is read in. */
function readTraces(sessionId) {
  return chatDb.prepare(
    'SELECT * FROM traces WHERE session_id = ? ORDER BY started_at ASC LIMIT 200')
    .all(sessionId)
    .map((row) => ({
      ...row,
      entries: JSON.parse(row.entries || '[]'),
      usage: row.usage ? JSON.parse(row.usage) : null,
    }));
}


// --- The page ---------------------------------------------------------------
//
// Server-rendered HTML, deliberately: this is a developer surface that must
// work when the app it debugs does not, so it depends on no module of the
// frontend, no fetch, and no script at all. Everything a browser puts on screen
// here came out of the database on this request.

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

// Every response no-store, and no page here is ever cached by anything.
const sendHtml = (res, status, html) => {
  res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8',
                          'Cache-Control': 'no-store',
                          'X-Robots-Tag': 'noindex, nofollow' });
  res.end(html);
};

const TRACE_CSS = `
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; padding: 24px; background: #f7f6f2; color: #23201c; }
  a { color: #7a2f2f; }
  h1 { font-size: 16px; letter-spacing: .08em; text-transform: uppercase; }
  .bar { display: flex; gap: 12px; align-items: baseline; margin-bottom: 18px; }
  .bar form { margin: 0; }
  table { border-collapse: collapse; width: 100%; max-width: 900px; }
  td, th { text-align: left; padding: 6px 10px; border-bottom: 1px solid #ddd8cc; }
  .prompt { max-width: 900px; margin: 0 0 26px; padding: 10px 0 0;
            border-top: 2px solid #23201c; }
  .prompt h2 { font-size: 13px; margin: 0 0 8px; font-weight: 600; }
  .status { padding: 1px 6px; border: 1px solid currentColor; border-radius: 3px;
            font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  .completed { color: #2f6b3a; } .failed { color: #8c2f2f; }
  .in_flight { color: #8a6d1f; } .interrupted { color: #8a6d1f; }
  .entry { margin: 0 0 8px; padding: 8px 10px; border: 1px solid #ddd8cc;
           background: #fffdf8; }
  .entry[data-role="system"] { background: #efece2; color: #5b564c; }
  .entry[data-role="ai"] { margin-inline-start: 28px; }
  .entry[data-role="tool"] { margin-inline-start: 28px; background: #f2f4ef; }
  .who { font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
         color: #6b665c; }
  pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; }
  details > summary { cursor: pointer; font-size: 12px; color: #6b665c; }
  .meta { font-size: 12px; color: #6b665c; }
  @media (prefers-color-scheme: dark) {
    body { background: #16150f; color: #e8e4d8; }
    a { color: #d99; }
    .entry { background: #1e1d16; border-color: #35332a; }
    .entry[data-role="system"] { background: #24231a; color: #b8b2a2; }
    .entry[data-role="tool"] { background: #1a1f1a; }
    td, th { border-color: #35332a; }
    .prompt { border-color: #e8e4d8; }
  }
`;

const tracePage = (title, body) => `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title><style>${TRACE_CSS}</style></head>
<body>${body}</body></html>`;

/** The door. One field, masked, and it says nothing about what is behind it. */
function unlockPage(next, failed = false) {
  return tracePage('Developer trace', `
    <h1>Developer trace</h1>
    <form method="POST" action="/dev/trace/unlock">
      <input type="hidden" name="next" value="${esc(next)}">
      <label>Key <input type="password" name="key" autocomplete="off" autofocus></label>
      <button type="submit">Unlock</button>
    </form>
    ${failed ? '<p class="meta">Unlock failed.</p>' : ''}`);
}

const when = (ms) => (ms ? new Date(ms).toISOString().replace('T', ' ').slice(0, 19) : '—');
const took = (row) => (row.ended_at && row.started_at
  ? `${((row.ended_at - row.started_at) / 1000).toFixed(1)}s` : '—');

function traceIndexPage(rows) {
  const body = rows.length ? `<table>
    <tr><th>Session</th><th>Prompts</th><th>Latest</th></tr>
    ${rows.map((r) => `<tr>
      <td><a href="/dev/trace?session=${encodeURIComponent(r.id)}">${esc(r.title || r.id)}</a>
          <div class="meta">${esc(r.id)}</div></td>
      <td>${r.prompts}</td><td>${esc(when(r.latest))}</td></tr>`).join('')}
  </table>` : '<p class="meta">No traces recorded yet. Is BRAIN_TRACE=board set on the brain?</p>';
  return tracePage('Developer trace', `
    <div class="bar"><h1>Developer trace</h1>
      <form method="POST" action="/dev/trace/lock"><button type="submit">Lock</button></form>
    </div>${body}`);
}

/** One entry of the tape. A tool call and a tool result are folded — they are
 *  the longest things here and the least often what you came to read. */
function traceEntry(entry) {
  const calls = entry.metadata && entry.metadata.tool_calls;
  const parts = [`<div class="who">${esc(entry.role)} · ${entry.seq}</div>`];
  if (entry.content) {
    // The system prompt is a page of text and it is identical in every prompt
    // group, so it is folded — folded, not trimmed: the whole thing is in the
    // document, one click away, and the character count says how much is
    // behind the fold. A tape you have to scroll past two screens of prompt to
    // read is a tape nobody reads.
    parts.push(entry.role === 'system'
      ? `<details><summary>system prompt · ${entry.content.length} chars</summary>
           <pre>${esc(entry.content)}</pre></details>`
      : `<pre>${esc(entry.content)}</pre>`);
  }
  if (Array.isArray(calls) && calls.length) {
    parts.push(calls.map((c) => `<details><summary>calls ${esc(c.name)}</summary>
      <pre>${esc(JSON.stringify(c.args, null, 2))}</pre></details>`).join(''));
  }
  if (entry.metadata && entry.metadata.tool_call_id) {
    parts.push(`<div class="meta">answering ${esc(entry.metadata.name || '')}
      (${esc(entry.metadata.tool_call_id)})</div>`);
  }
  return `<div class="entry" data-role="${esc(entry.role)}">${parts.join('')}</div>`;
}

function traceDetailPage(sessionId, rows) {
  const groups = rows.map((row, i) => `
    <section class="prompt" data-trace="${esc(row.trace_id)}">
      <h2>Prompt ${i + 1}
        <span class="status ${esc(row.status)}">${esc(row.status)}</span></h2>
      <div class="meta">${esc(when(row.started_at))} · ${esc(took(row))}
        · ${esc(row.provider || '?')}/${esc(row.model || '?')}
        ${row.usage ? `· ${esc(row.usage.total_tokens ?? '?')} tokens` : ''}</div>
      ${row.error ? `<p class="meta">error: ${esc(row.error)}</p>` : ''}
      ${row.entries.map(traceEntry).join('')}
    </section>`).join('');
  return tracePage(`Trace · ${sessionId}`, `
    <div class="bar"><h1><a href="/dev/trace">Developer trace</a> · ${esc(sessionId)}</h1>
      <form method="POST" action="/dev/trace/lock"><button type="submit">Lock</button></form>
    </div>
    <p class="meta">Read-only. This page shows the message envelope the brain
      handed the model, in the order it was handed over.</p>
    ${groups || '<p class="meta">No traces for that session.</p>'}`);
}

/** Is this request carrying a live unlock token? Constant-time, and false the
 *  moment the process restarts — the token only ever existed in memory. */
const devUnlocked = (req) => Boolean(devToken)
  && secretEquals(parseCookies(req.headers.cookie).lodestar_dev || '', devToken);

// --------------------------------------------------------------------------
// HTTP
// --------------------------------------------------------------------------

const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
};

// The frontend is a tree of ES modules under js/, so the whitelist is built
// from the directory at boot rather than listed by hand — a module added
// without a matching entry here would 404 in the browser and take the whole
// import graph down with it. It stays a whitelist: the walk happens once, at
// start-up, and a request still only ever names a key of this object, so no
// request path is ever joined onto the filesystem.
function addModules(dir = 'js') {
  for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
    const rel = `${dir}/${entry.name}`;
    if (entry.isDirectory()) addModules(rel);
    else if (entry.name.endsWith('.js')) STATIC['/' + rel] = [rel, 'text/javascript; charset=utf-8'];
  }
}
try {
  addModules();
} catch (err) {
  // A missing js/ must not take the API down with it — the board's data is
  // reachable over HTTP whether or not this copy of the server has a frontend
  // beside it, and tests/databases.test.js boots server.js alone in a temp
  // directory to prove exactly that. It is still said out loud, because the
  // other way to arrive here is a deployment that shipped without its
  // frontend, and that must not present as a merely blank page.
  console.warn(`No frontend modules found at ${join(ROOT, 'js')} — serving the API only.`, err.message);
}

function sendJson(res, status, body, headers = {}) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
  res.end(text);
}

// --------------------------------------------------------------------------
// Conditional reads
// --------------------------------------------------------------------------

// An ALLOWLIST, named in full here, and deliberately not a middleware that
// stamps every response on its way out. A validator is a promise that a client
// may reuse what it already holds, and this server's payloads are somebody's
// private life: the cost of a wrong entry is not a slow page, it is last week's
// cards on screen with no way to tell. So a route joins the list only when both
// of these hold, checked one route at a time by reading it:
//
//   1. its validator changes whenever ANYTHING the client can see changes —
//      not a row count, not a MAX(updated_at); see revOf's own note for why
//      every aggregate available here has a blind spot that loses a card, and
//      the same reasoning applies to a validator, and
//   2. a match can be answered without building the response body.
//
// ON the list:
//   GET /api/state   — the board already computes `rev`, a sha1 of the exact
//                      bytes the client is sent. That is an ETag in all but
//                      name and it is the one validator in this file that is
//                      already contractually blind-spot-free, so it is reused
//                      rather than a second one invented beside it. What a 304
//                      skips is the response object, its serialisation and the
//                      body on the wire; what it cannot skip is the board read
//                      the validator is derived FROM, and that is the trade
//                      being made on purpose — a validator cheaper than the
//                      payload would have to be an aggregate, and revOf's note
//                      records what that costs. The conditional path therefore
//                      does strictly less work than the unconditional one ever
//                      did; it does not do none.
//   the STATIC files — index.html, styles.css and every js/ module: code
//                      shipped with the server, hashed from the exact bytes
//                      just read off disk. Forty-odd modules revalidate on
//                      every reload and none of them carries user data.
//
// OFF the list, each for a reason:
//   /api/trash, /api/proposals, /api/edits, /api/boards, /api/boards/trash —
//     mutable card and board state with no version of its own. Minting one
//     means hashing the payload, and unlike /api/state there is no existing
//     hash to reuse, so it would be new work on the common path to save bytes
//     on a read nobody makes in a loop.
//   every /api/chat/* read — the same, and worse: the chat record is what the
//     durability promise is about, a soft-delete has to disappear from a read
//     the moment it happens, and this is the surface where being subtly stale
//     would be least visible. It stays uncached, which is the spec's second
//     requirement rather than an omission.
//   /api/trace/status and every other trace response — `no-store` is that
//     surface's rule (a trace is read to see the state a turn is in NOW), and
//     the most a validator could save there is one request per session.
//   /api/health and /login — the public paths, one small fixed body each.
//
// Removing '/api/state' from this set is the whole feature-disable path for
// that route: the header stops being sent and the 304 stops being possible,
// with no other edit.
const VALIDATED_READS = new Set(['/api/state', ...Object.keys(STATIC)]);

/**
 * Does this request already hold the version we were about to send?
 *
 * The allowlist is checked in HERE rather than at each call site, so a route
 * cannot acquire a validator by someone remembering to ask for one.
 *
 * `If-None-Match` may carry a list, and `*` means "any version at all, if you
 * have one". Comparison is weak per RFC 9110 — a `W/` prefix is stripped from
 * both sides — which changes nothing today, since every tag this server mints
 * is strong, and stops a proxy that weakened one from silently missing.
 */
function notModified(req, path, tag) {
  if (!VALIDATED_READS.has(path) || !tag) return false;
  const header = req.headers['if-none-match'];
  if (!header) return false;
  const bare = (t) => t.trim().replace(/^W\//, '');
  const wanted = bare(tag);
  return header.split(',').some((candidate) => {
    const one = bare(candidate);
    return one === '*' || one === wanted;
  });
}

/** A validator over exact bytes. sha1 and 16 hex chars for revOf's reason:
 *  this is a change-detector, never a security claim. */
const etagOf = (bytes) => `"${createHash('sha1').update(bytes).digest('hex').slice(0, 16)}"`;

/** `no-cache` is the other half of the contract and is not optional. It does
 *  not mean "do not store"; it means "never reuse this without asking me
 *  first", which is exactly what makes an ETag safe on a board that changes.
 *  Without it a cache is free to apply its own heuristic freshness to a 200
 *  carrying no directives, and the ETag would then have made staleness more
 *  likely rather than less. */
const REVALIDATE = { 'Cache-Control': 'no-cache' };

/** 304, and nothing whatsoever else: no body and no Content-Type, because a
 *  not-modified response carrying a payload has already spent everything the
 *  conditional request asked to save. The validator is echoed, since a client
 *  that gets a 304 goes on using the copy it holds and the tag it holds with
 *  it. Node knows a 304 has no body, so no length or encoding header is
 *  written either. */
function sendNotModified(res, tag) {
  res.writeHead(304, { ETag: tag, ...REVALIDATE });
  res.end();
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 5_000_000) { // ~5 MB guard
        // Marked, not just thrown: every `Invalid JSON` catch below re-throws
        // anything that is not a body-shape problem so a store error reaches
        // the handler-level catch as a 500 — but this one is the caller's
        // fault, same as a syntax error, and must stay a 400 like it always was.
        const err = new Error('Payload too large');
        err.badRequest = true;
        reject(err);
      }
    });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

// --------------------------------------------------------------------------
// The boundary, as it runs
// --------------------------------------------------------------------------

// The port this process actually bound. PORT is what was asked for; with
// PORT=0 the kernel answers with something else, and it is the answer the Host
// allowlist has to compare against. Set once, in the listen callback, which is
// before any request can arrive.
let activePort = PORT;

// The four public surfaces, and there are deliberately only four: the login
// page, the two calls that start and end a session, and a liveness ping that
// says nothing but "this process is up". Everything else — every board and
// chat route, the assistant proxy, index.html, every ES module under js/ —
// is behind the session.
const LOGIN_PAGE = '/login';
const PUBLIC_PATHS = new Set([LOGIN_PAGE, '/api/login', '/api/logout', '/api/health']);

const boundary = () => ({ port: activePort, extra: ALLOWED_HOSTS });

function sendText(res, status, text, headers = {}) {
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8', ...headers });
  res.end(text);
}

/** Who is asking: 'session' for a logged-in browser, 'service' for the brain,
 *  or null. Deliberately returns one of three values and not a reason — the
 *  caller has nothing useful to do with "the cookie was expired" that it would
 *  not also do with "there was no cookie", and telling them apart in a
 *  response is how a login boundary starts leaking. */
function identify(req) {
  const header = req.headers.authorization || '';
  if (header.startsWith('Bearer ')) {
    // Only ever consulted when a token is configured, so an unset
    // LODESTAR_SERVICE_TOKEN cannot be matched by sending an empty Bearer.
    return SERVICE_TOKEN && secretEquals(header.slice(7).trim(), SERVICE_TOKEN)
      ? 'service' : null;
  }
  const token = parseCookies(req.headers.cookie)[SESSION_COOKIE];
  return token && sessions.verify(token) ? 'session' : null;
}

/** POST /api/login. One body field, one of three outcomes, and the failure
 *  says the same thing whatever went wrong. */
async function handleLogin(req, res) {
  const wait = loginThrottle.retryAfter();
  if (wait > 0) {
    return sendJson(res, 429, { error: 'Too many attempts' },
      { 'Retry-After': String(wait) });
  }
  let password = '';
  try {
    password = String(JSON.parse(await readBody(req))?.password ?? '');
  } catch {
    // A malformed body is a failed attempt like any other: an attacker must
    // not be able to probe the throttle for free by sending junk.
    password = '';
  }
  if (!verifyPassword(password, PASSWORD_HASH)) {
    loginThrottle.recordFailure();
    // Nothing about which part failed, and — obviously, but it has been got
    // wrong in real code — nothing echoing what was typed.
    return sendJson(res, 401, { error: 'Login failed' });
  }
  loginThrottle.recordSuccess();
  const token = sessions.create();
  // The raw token appears here, in this header, and nowhere else in the
  // process: the store keeps only its sha256, and no log line ever sees it.
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Set-Cookie': sessionCookie(token, { maxAgeMs: ABSOLUTE_MS }),
  });
  return res.end(JSON.stringify({ ok: true }));
}

function handleLogout(req, res) {
  sessions.revoke(parseCookies(req.headers.cookie)[SESSION_COOKIE]);
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Set-Cookie': clearedCookie(),
  });
  return res.end(JSON.stringify({ ok: true }));
}

/** Route one request. Every throw in here is caught by the one handler below;
 *  see the note there for why that catch is not optional. */
async function handleRequest(req, res) {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // 1. Is this request even addressed to us? A page on any domain can point
  // that domain at 127.0.0.1 and have the browser connect here — DNS
  // rebinding — but it cannot forge the Host header its browser sends. So the
  // allowlist runs first, before the router, before a row is read and before a
  // body is even accepted: a rejected alias must not be able to observe that a
  // route exists, let alone reach it. 403 rather than 404, because pretending
  // not to exist would be a lie the local browser also has to believe.
  if (!hostAllowed(req.headers.host, boundary())) {
    return sendText(res, 403, 'Forbidden');
  }

  // 2. The four public surfaces.
  if (path === '/api/health') {
    // Liveness and nothing else: no version, no board, no backend, no
    // configuration. A health endpoint is the one thing that answers before
    // login, so it must be worth nothing to whoever asks.
    return sendJson(res, 200, { ok: true });
  }
  if (path === '/api/login' && req.method === 'POST') return handleLogin(req, res);
  if (path === '/api/logout' && req.method === 'POST') return handleLogout(req, res);

  // 3. The session. Everything from here down is private.
  const who = identify(req);
  if (path === LOGIN_PAGE) {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      return sendJson(res, 405, { error: 'Method not allowed' });
    }
    // Already logged in: send them to the board rather than to a form they
    // have no reason to fill in again.
    if (who) return res.writeHead(302, { Location: '/' }).end();
    try {
      const body = await readFile(join(ROOT, 'login.html'));
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(body);
    } catch {
      return sendText(res, 500, 'Login page missing');
    }
  }
  if (!who) {
    // A browser asking for the app gets the door; everything else gets a
    // status it can act on. Neither carries a byte of board, chat or
    // configuration, which is the whole requirement: the login boundary must
    // not leak through the thing that reports it.
    if ((req.method === 'GET' || req.method === 'HEAD')
        && (path === '/' || path === '/index.html')) {
      return res.writeHead(302, { Location: LOGIN_PAGE }).end();
    }
    return sendJson(res, 401, { error: 'Unauthorized' });
  }

  // 4. Where an authenticated mutation came from. SameSite=Strict already
  // means a cross-site page's request carries no cookie, so this is the second
  // of two independent defences rather than the only one — and it is the one
  // that still holds if a browser ever disagrees with us about what "site"
  // means. A missing Origin AND Referer is allowed on purpose: that is a
  // non-browser client, and it has already had to authenticate to get here.
  if (req.method === 'POST' || req.method === 'PUT'
      || req.method === 'PATCH' || req.method === 'DELETE') {
    if (provenanceOf(req.headers, boundary()) === 'blocked') {
      return sendJson(res, 403, { error: 'Forbidden' });
    }
  }

  // The developer trace. Behind the session like everything else — the dev key
  // is a second lock, never the only one — and absent entirely when no key is
  // configured: 404, not 401, because a board running the ordinary way should
  // not advertise that a developer page exists at all.
  //
  // Whether tracing is available is the one thing said before the door: the
  // Assistant needs it to decide whether to draw a control, and "a link exists"
  // is not a secret worth keeping from someone who has already logged in.
  if (path === '/api/trace/status') {
    if (req.method !== 'GET') return sendJson(res, 405, { error: 'Method not allowed' });
    return sendJson(res, 200, { enabled: Boolean(DEV_KEY) });
  }

  if (path === '/api/trace' || path === '/dev/trace'
      || path.startsWith('/dev/trace/')) {
    if (!DEV_KEY) return sendJson(res, 404, { error: 'Not found' });

    // The brain filing a turn. Not behind the unlock token: the token is a
    // *reader's* credential, and the writer is the brain, which arrives with
    // the service token like every other write it makes.
    if (path === '/api/trace') {
      if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
      try {
        const trace = cleanTrace(JSON.parse(await readBody(req)));
        if (!trace) return sendJson(res, 400, { error: 'Not a trace' });
        writeTrace(trace);
        return sendJson(res, 200, { ok: true }, { 'Cache-Control': 'no-store' });
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }

    if (path === '/dev/trace/unlock') {
      if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
      const form = new URLSearchParams(await readBody(req));
      // Constant-time, and never told apart from any other failure. The
      // submitted value is not echoed anywhere — not into the page, not into a
      // log line, and not into the redirect.
      if (!secretEquals(form.get('key') || '', DEV_KEY)) {
        return sendHtml(res, 401, unlockPage('/dev/trace', true));
      }
      // A fresh token per unlock, so an old one stops working. Random, and
      // kept only in this process's memory: nothing durable to steal, and a
      // restart locks the door again.
      devToken = randomBytes(32).toString('base64url');
      const next = String(form.get('next') || '/dev/trace');
      res.writeHead(303, {
        // Path-scoped, so this cookie is never sent to the board's own API,
        // and HttpOnly so no script on any page can read it.
        'Set-Cookie': `lodestar_dev=${devToken}; Path=/dev/trace; HttpOnly; `
          + 'SameSite=Strict',
        'Cache-Control': 'no-store',
        // The path the user asked for, and nothing they typed.
        Location: next.startsWith('/dev/trace') ? next : '/dev/trace',
      });
      return res.end();
    }

    if (path === '/dev/trace/lock') {
      if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
      devToken = '';
      res.writeHead(303, {
        'Set-Cookie': 'lodestar_dev=; Path=/dev/trace; HttpOnly; SameSite=Strict; Max-Age=0',
        'Cache-Control': 'no-store', Location: '/dev/trace',
      });
      return res.end();
    }

    if (path === '/dev/trace') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        return sendJson(res, 405, { error: 'Method not allowed' });
      }
      const session = url.searchParams.get('session') || '';
      if (!devUnlocked(req)) {
        return sendHtml(res, 200, unlockPage(session
          ? `/dev/trace?session=${encodeURIComponent(session)}` : '/dev/trace'));
      }
      if (!session) return sendHtml(res, 200, traceIndexPage(readTraceSessions()));
      return sendHtml(res, 200, traceDetailPage(session, readTraces(session)));
    }

    return sendJson(res, 404, { error: 'Not found' });
  }

  // Which board this request is about, for every route that reads or writes
  // board data. Absent means the default board, so every caller written before
  // boards existed still addresses a real one; a named board that is gone or
  // never existed is refused below rather than quietly swapped for another.
  const boardId = resolveBoard(url);
  const noSuchBoard = () => sendJson(res, 400, { error: 'No such board' });

  // The boards themselves. Checked before the board-data routes only for
  // reading order; the paths do not overlap.
  if (path === '/api/boards') {
    if (req.method === 'GET') {
      return sendJson(res, 200, { boards: readBoards(), defaultId: defaultBoardId() });
    }
    if (req.method === 'POST') {
      try {
        const board = createBoard(JSON.parse(await readBody(req))?.name);
        if (!board) return sendJson(res, 400, { error: 'A board needs a non-empty name' });
        return sendJson(res, 200, { board });
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Deleted boards, and the two ways out of the trash. Matched before
  // /api/boards/:id, or 'trash' would be read as a board id.
  if (path === '/api/boards/trash') {
    if (req.method === 'GET') return sendJson(res, 200, { boards: readBoardsTrash() });
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  if (path.startsWith('/api/boards/trash/')) {
    const rest = decodeURIComponent(path.slice('/api/boards/trash/'.length));
    if (rest.endsWith('/restore')) {
      if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
      const board = restoreBoard(rest.slice(0, -'/restore'.length));
      return board
        ? sendJson(res, 200, { board })
        : sendJson(res, 404, { error: 'No such deleted board' });
    }
    if (req.method === 'DELETE') {
      const purged = purgeBoard(rest);
      // 404 for a board that is live as well as for one that never existed: the
      // only thing that can be erased is something already in the trash.
      return purged
        ? sendJson(res, 200, { ok: true, ...purged })
        : sendJson(res, 404, { error: 'No such deleted board' });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  if (path.startsWith('/api/boards/')) {
    const id = decodeURIComponent(path.slice('/api/boards/'.length));
    if (req.method === 'PATCH') {
      try {
        const board = renameBoard(id, JSON.parse(await readBody(req))?.name);
        // One status for both "no such board" and "blank name": the second is
        // the interesting one, so the message names it.
        return board
          ? sendJson(res, 200, { board })
          : sendJson(res, 400, { error: 'A board needs a non-empty name' });
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    if (req.method === 'DELETE') {
      const outcome = deleteBoard(id);
      if (outcome === 'missing') return sendJson(res, 404, { error: 'No such board' });
      if (outcome === 'last') {
        return sendJson(res, 409, { error: 'The last board cannot be deleted' });
      }
      return sendJson(res, 200, { ok: true });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // API
  if (path === '/api/state') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') {
      // The rev is the validator — see VALIDATED_READS for why this route is
      // the one board read that may carry one, and what a 304 here does and
      // does not save.
      const board = boardWithRev(boardId);
      // The tag IS the rev, quoted — not a second hash of it. One value, so a
      // client can compare what it sent on a PUT with what it holds as a
      // validator and see the same string.
      const tag = `"${board.rev}"`;
      if (notModified(req, path, tag)) return sendNotModified(res, tag);
      return sendJson(res, 200, board, { ETag: tag, ...REVALIDATE });
    }
    if (req.method === 'PUT') {
      try {
        const parsed = JSON.parse(await readBody(req));
        if (!parsed || !Array.isArray(parsed.cards)) {
          return sendJson(res, 400, { error: 'Body must be { version, cards: [...] }' });
        }
        // Which board this save was written against. Three states, one field,
        // and the difference between them is only ever whether DELETING is
        // authorised:
        //   absent            — say nothing, get the old contract. Every curl,
        //                       eval and pre-rev test lives here, and so does
        //                       the whole-board sweep they rely on.
        //   the current rev   — this client is looking at what the database
        //                       holds, so an omitted card really was deleted.
        //   anything else     — including '': the client is describing a board
        //                       that has since moved. Its save is applied
        //                       additively and deletes nothing.
        // Sending '' rather than omitting the field is what a client does when
        // it has no rev yet, so that no client code path can be granted the
        // right to delete by forgetting to say anything.
        const claimed = Object.hasOwn(parsed, 'rev') ? String(parsed.rev) : null;
        // Read-compare-write with no lock, and it needs none: node:sqlite is
        // synchronous and the one await in this handler (readBody) is already
        // done. Nothing may put an await between this line and the write.
        const stale = claimed !== null && claimed !== revOf(readBoard(boardId));

        // Registry first, then cards — so cards referencing a just-added
        // category validate against the fresh registry.
        const cats = sanitizeCategories(parsed.categories);
        if (cats) (stale ? mergeCategories : writeCategories)(cats, boardId);
        const { board, created } = (stale ? mergeBoard : writeBoard)(parsed.cards, boardId);
        // The rev of the board as it now stands, so the client that just wrote
        // is the one client guaranteed not to be stale on its next save.
        sendJson(res, 200, { ...board, rev: revOf(board), ...(stale ? { stale: true } : {}) });
        // After the response: one snapshot per save that brought new cards,
        // however many they were. Never before, or the backup would miss them.
        // A merge counts: it is exactly when never-seen cards arrive.
        if (created > 0) backupAfterNewCards();
        return;
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The Trash — soft-deleted cards, recoverable until purged.
  if (path === '/api/trash') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, readTrash(boardId));
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The chat record. Deleting is soft everywhere here: a chat's DELETE stamps
  // its session, a message's DELETE stamps the row, and both stay in the file.
  // The single exception is DELETE /api/chat/trash/:id below — chat's own
  // "Delete permanently", reachable only for something already in the trash.
  if (path === '/api/chat/messages') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, { messages: readChatMessages(boardId) });
    if (req.method === 'POST') {
      try {
        const body = JSON.parse(await readBody(req));
        // The board is named in the body here, beside the session, because this
        // is the one chat route the brain posts to and both travel together.
        const target = resolveBoardId(body.boardId);
        if (target === null) return noSuchBoard();
        const saved = appendChatMessages(body.messages, body.sessionId, target);
        if (!saved) {
          return sendJson(res, 400, { error:
            'Body must be { messages: [{role, content, createdAt?, steps?, usage?, cost?}], sessionId?, boardId? }'
            + ' — role user|assistant, content non-empty, sessionId a string' });
        }
        return sendJson(res, 200, { messages: saved });
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Every board's live messages, in one read. The brain's chat index is one
  // Chroma collection over the whole record, and its maintenance needs the
  // whole record: `prune` drops chunks whose message is no longer live, so
  // handing it one board's messages would erase every other board from the
  // index. Nothing in the browser calls this — recall is board-scoped, and this
  // is the one read that deliberately is not.
  if (path === '/api/chat/messages/all') {
    if (req.method === 'GET') return sendJson(res, 200, { messages: readChatMessages() });
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // One turn, hidden on its own.
  if (path.startsWith('/api/chat/messages/')) {
    const id = decodeURIComponent(path.slice('/api/chat/messages/'.length));
    if (req.method === 'DELETE') {
      return deleteChatMessage(id)
        ? sendJson(res, 200, { ok: true })
        : sendJson(res, 404, { error: 'No such message' });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The assistant's trash — turns deleted one at a time, recoverable until the
  // deliberate second step.
  if (path === '/api/chat/trash') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, { messages: readChatTrash(boardId) });
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  if (path.startsWith('/api/chat/trash/')) {
    const rest = decodeURIComponent(path.slice('/api/chat/trash/'.length));
    if (rest.endsWith('/restore')) {
      const id = rest.slice(0, -'/restore'.length);
      if (req.method === 'POST') {
        return restoreChatMessage(id)
          ? sendJson(res, 200, { ok: true })
          : sendJson(res, 404, { error: 'No such deleted message' });
      }
      return sendJson(res, 405, { error: 'Method not allowed' });
    }
    if (req.method === 'DELETE') {
      return purgeChatMessage(rest)
        ? sendJson(res, 200, { ok: true })
        : sendJson(res, 404, { error: 'No such deleted message' });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The chats themselves — the history panel's list.
  if (path === '/api/chat/sessions') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, { sessions: readChatSessions(boardId) });
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  if (path.startsWith('/api/chat/sessions/')) {
    const id = decodeURIComponent(path.slice('/api/chat/sessions/'.length));
    if (!id) return sendJson(res, 404, { error: 'No such chat' });
    if (req.method === 'GET') {
      const found = readChatSession(id);
      return found ? sendJson(res, 200, found) : sendJson(res, 404, { error: 'No such chat' });
    }
    if (req.method === 'PATCH') {
      try {
        const { title } = JSON.parse(await readBody(req));
        const session = renameChatSession(id, title);
        if (!session) {
          // One status for both "no such chat" and "blank title": the second is
          // the interesting one, so the message names it.
          return sendJson(res, 400, { error: 'A chat needs a non-empty title' });
        }
        return sendJson(res, 200, { session });
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    if (req.method === 'DELETE') {
      return deleteChatSession(id)
        ? sendJson(res, 200, { ok: true })
        : sendJson(res, 404, { error: 'No such chat' });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Proposals — cards the Assistant suggested, awaiting the user's approval.
  // Deliberately NOT part of /api/state: they never travel through a whole-board
  // PUT, so the "never send a partial card list" contract is untouched.
  if (path === '/api/proposals') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, readProposals(boardId));
    if (req.method === 'POST') {
      try {
        const proposal = writeProposal(JSON.parse(await readBody(req)), boardId);
        if (!proposal) return sendJson(res, 400, { error: 'A proposal needs a title' });
        return sendJson(res, 200, proposal);
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Accept or decline one proposal.
  // Suggested edits. No confirm route on purpose: accepting one is the user
  // saving the board, which is PUT /api/state like any other edit they make.
  // All this surface can do is hold a suggestion and let go of it.
  if (path === '/api/edits') {
    if (boardId === null) return noSuchBoard();
    if (req.method === 'GET') return sendJson(res, 200, { edits: readEdits(boardId) });
    if (req.method === 'POST') {
      try {
        const stored = writeEdit(JSON.parse(await readBody(req)));
        if (!stored) {
          return sendJson(res, 400, {
            error: 'A suggestion needs a live card id and at least one editable field',
          });
        }
        // No backup: nothing changed, so there is nothing to snapshot.
        return sendJson(res, 200, stored);
      } catch (err) {
        if (!(err instanceof SyntaxError) && !err.badRequest) throw err;
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }
  if (path.startsWith('/api/edits/')) {
    const id = decodeURIComponent(path.slice('/api/edits/'.length));
    if (req.method !== 'DELETE') return sendJson(res, 405, { error: 'Method not allowed' });
    if (!discardEdit(id)) return sendJson(res, 404, { error: 'No such suggestion' });
    return sendJson(res, 200, { ok: true });
  }

  if (path.startsWith('/api/proposals/')) {
    const rest = path.slice('/api/proposals/'.length);
    const slash = rest.lastIndexOf('/');
    const id = decodeURIComponent(rest.slice(0, slash));
    const action = rest.slice(slash + 1);
    if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
    if (action === 'confirm') {
      const landedOn = confirmProposal(id);
      if (!landedOn) return sendJson(res, 404, { error: 'No such proposal' });
      // With the rev: the browser adopts this board, and a client that adopts a
      // board without knowing its rev is stale from then on without being told.
      sendJson(res, 200, boardWithRev(landedOn));
      // The card is the user's now, which is the moment worth a snapshot.
      backupAfterNewCards();
      return;
    }
    if (action === 'reject') {
      if (!rejectProposal(id)) return sendJson(res, 404, { error: 'No such proposal' });
      // No backup: declining a suggestion is not a new entry.
      return sendJson(res, 200, { ok: true });
    }
    return sendJson(res, 404, { error: 'Not found' });
  }

  // Permanent delete of a single card (the deliberate second step).
  if (path.startsWith('/api/cards/')) {
    const id = decodeURIComponent(path.slice('/api/cards/'.length));
    if (req.method === 'DELETE') {
      if (!id) return sendJson(res, 400, { error: 'Missing card id' });
      return sendJson(res, 200, { ok: purgeCard(id) });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The one upstream: the brain holds the LLM key, so the browser never talks to
  // it directly.
  const upstream = path.startsWith('/api/agent/') || path.startsWith('/api/rag/')
    ? { url: AGENT_URL + path.slice('/api'.length), down: 'assistant unavailable',
        limited: true }
    : null;
  if (upstream) {
    // Metered before the body is read, so a flood costs nothing to refuse. The
    // flag stays although there is only one upstream to carry it: it says that
    // metering is a property of *this* upstream and not of proxying.
    if (upstream.limited) {
      const after = agentRetryAfter();
      if (after !== null) {
        return sendJson(res, 429, { error: 'Too many assistant requests' },
          { 'Retry-After': String(after) });
      }
    }
    const target = upstream.url + url.search;
    let body;
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      try {
        body = await readBody(req);
      } catch {
        // Almost always an over-long voice recording. Reporting this as
        // "assistant unavailable" would send the user debugging a brain that
        // is running perfectly well, so name the real problem.
        return sendJson(res, 413, { error: 'Payload too large' });
      }
    }
    try {
      // Named `relayed` rather than shadowing `upstream`: the catch below reads
      // the outer one for its `down` message, which only worked by block scope.
      const relayed = await fetch(target, {
        method: req.method,
        headers: { 'content-type': req.headers['content-type'] || 'application/json' },
        body,
        signal: AbortSignal.timeout(120000),
      });
      const headers = {
        'Content-Type': relayed.headers.get('content-type') || 'application/json',
      };
      // Forwarded because an event stream must not be cached: without it the
      // same question asked twice can replay the first answer's frames.
      const cache = relayed.headers.get('cache-control');
      if (cache) headers['Cache-Control'] = cache;
      res.writeHead(relayed.status, headers);
      // Piped, never buffered. `await upstream.text()` waits for the last byte,
      // so the assistant's own progress — the whole point of the SSE route —
      // would arrive in one lump at the end, byte-identical and useless.
      if (relayed.body) await pipeline(Readable.fromWeb(relayed.body), res);
      else res.end();
    } catch {
      // Once the headers are out there is no status left to fail with, and
      // sendJson would throw on top of the original error. Dropping the socket
      // is what tells the browser the stream ended early.
      if (res.headersSent) res.destroy();
      else sendJson(res, 503, { error: upstream.down });
    }
    return;
  }

  // Static (whitelisted — no arbitrary filesystem access)
  const entry = STATIC[path];
  if (entry && req.method === 'GET') {
    const [file, type] = entry;
    try {
      const body = await readFile(join(ROOT, normalize(file)));
      // Hashed from the bytes just read, never from mtime and size: an editor
      // that rewrites a module to the same length inside one filesystem tick
      // would leave that pair unchanged and pin a stale module in the browser
      // of whoever is developing the app.
      const tag = etagOf(body);
      if (notModified(req, path, tag)) return sendNotModified(res, tag);
      res.writeHead(200, { 'Content-Type': type, ETag: tag, ...REVALIDATE });
      return res.end(body);
    } catch {
      res.writeHead(404).end('Not found');
      return;
    }
  }

  res.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found');
}

// One catch over the whole router, and it is load-bearing rather than tidy.
// The router is an async function, so anything it throws becomes a rejected
// promise; since Node 15 an unhandled rejection TERMINATES THE PROCESS. So a
// single bad request — `GET /api/boards/%E0%A4%A`, where decodeURIComponent
// throws URIError, or any statement that meets a busy database — used to take
// the server down and every other in-flight request with it. Catching here
// turns that into one failed request, which is the property worth having.
//
// The client is told nothing but "Server error": an exception's message can
// carry a filesystem path or a connection string, and the browser is not where
// that belongs. The detail is logged instead — a silent catch would trade a
// loud crash for an invisible bug. `headersSent` is checked because a route
// may already have streamed (the SSE proxy does), and writing a second time
// would throw inside the catch itself.
const server = createServer((req, res) => {
  handleRequest(req, res).catch((err) => {
    try {
      console.error(`Unhandled error for ${req.method} ${req.url}:`, err);
      if (res.headersSent) res.destroy();
      else sendJson(res, 500, { error: 'Server error' });
    } catch (e) {
      // The catch of last resort must not itself throw: that would be a fresh
      // unhandled rejection, the exact crash this handler exists to prevent.
      console.error(e);
    }
  });
});

server.listen(PORT, BIND, () => {
  // The port the OS actually gave us, not the one we asked for. They differ for
  // exactly one caller and it matters there: PORT=0 lets the kernel hand out a
  // free port, which is how the test harness starts a dozen servers at once
  // without them fighting over a number somebody guessed.
  const { port } = server.address();
  // Which is also the only port the Host allowlist may accept, so it is
  // learned here rather than read from PORT: with PORT=0 the two differ, and
  // an allowlist built on the *requested* port would reject every request the
  // test harness makes.
  activePort = port;
  // The backend is named in the log because "which store am I actually
  // writing to" must be answerable without reading the environment back.
  console.log(
    `Lodestar running at http://localhost:${port}  (backend: ${DB_BACKEND}, db: ${DB_PATH})`);
  // Said out loud because it is the security property of this process, and the
  // one thing an operator must be able to confirm without reading the source.
  console.log(BIND === '127.0.0.1'
    ? '  bound to 127.0.0.1 — reachable from this machine only; login required'
    : `  bound to ${BIND} — NOT loopback-only; every interface can reach this port`);
  // Which of the two forms supplied the password, said once at boot. The
  // plaintext form is the weaker one and it is chosen by editing a file, so it
  // should not be possible to be running it and not know.
  if (PASSWORD_SOURCE === 'password') {
    console.log('  password read from LODESTAR_AUTH_PASSWORD — it is stored in '
      + 'plaintext in .env; `npm run auth:setup` mints a hash instead');
  }
});
