// Lodestar server — serves the static app and persists the board to a
// local SQLite file so questions survive restarts. Zero npm dependencies:
// Node's built-in http server and node:sqlite do all the work.
//
//   node server.js                 # http://localhost:3000, board.db beside this file
//   PORT=4000 BOARD_DB=/tmp/x.db node server.js
//
// The board is stored one row per question. The client sends its whole state
// on every change (PUT /api/state); the server upserts the rows it sees and
// SOFT-deletes the rows it doesn't (marks them, never removes them) — so a
// partial or buggy save can never lose a question. Soft-deleted rows live on in
// the Trash (GET /api/trash) and are recoverable until an explicit, deliberate
// purge (DELETE /api/cards/:id). That purge is the ONLY thing that truly erases
// a question from the database; otherwise the only way to lose data is to delete
// the database file itself.

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { mkdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 3000;
const DB_PATH = process.env.BOARD_DB || join(ROOT, 'board.db');
const AGENT_URL = process.env.AGENT_URL || 'http://127.0.0.1:9000';
// The RAG lab (brain/tests/raglab) — developer tooling, in the 9000 block like
// the brains but a separate service, and usually not running at all. The
// Assistant's lab page reaches it through this proxy so the browser talks to one
// origin, exactly as it does for the brain.
const RAGLAB_URL = process.env.RAGLAB_URL || 'http://127.0.0.1:9002';

// A new question on the board is worth a snapshot of the database. Off only
// when explicitly disabled — the test suites set this to '0' so they never add
// throwaway boards to the user's real backup history.
const BACKUP_ON_WRITE = process.env.LODESTAR_BACKUP_ON_WRITE !== '0';
const BACKUP_SCRIPT = join(ROOT, 'scripts', 'backup-db.mjs');

const COLUMN_IDS = ['inbox', 'in-progress', 'answered'];
const TYPES = ['question', 'problem', 'task', 'idea', 'plan'];

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

/** Same shape/rules as the client: [{id, label, h}] or null when unusable. */
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
  return out.length ? out : null;
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

// --------------------------------------------------------------------------
// Database
// --------------------------------------------------------------------------

// Make sure the DB's directory exists — on Azure App Service we point
// BOARD_DB at /home/data (persistent storage) which may not exist on first boot.
mkdirSync(dirname(DB_PATH), { recursive: true });

const db = new DatabaseSync(DB_PATH);
db.exec(`
  CREATE TABLE IF NOT EXISTS cards (
    id         TEXT PRIMARY KEY,
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
    pending     INTEGER NOT NULL DEFAULT 0
  );
`);

// Migrate databases created before newer columns existed: add any that are
// missing so older board.db files keep working untouched. deleted_at is NULL
// for a live question and a timestamp once it has been soft-deleted (trashed).
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

// The user's category registry. Seeded with the default life areas the first
// time; from then on the client's edits (add/remove/import) are the truth.
db.exec(`
  CREATE TABLE IF NOT EXISTS categories (
    id       TEXT    PRIMARY KEY,
    label    TEXT    NOT NULL,
    h        INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
  );
`);
if (db.prepare('SELECT COUNT(*) AS n FROM categories').get().n === 0) {
  const seed = db.prepare('INSERT INTO categories (id, label, h, position) VALUES (?, ?, ?, ?)');
  DEFAULT_CATEGORIES.forEach((c, i) => seed.run(c.id, c.label, c.h, i));
}

function readCategories() {
  return db.prepare('SELECT id, label, h FROM categories ORDER BY position ASC').all()
    .map((r) => ({ id: r.id, label: r.label, h: r.h }));
}

/** Replace the whole registry — it's config, not card data, so unlike cards
 *  it has no soft-delete: removing a category never touches any card row. */
function writeCategories(cats) {
  db.exec('BEGIN');
  try {
    db.exec('DELETE FROM categories');
    const insert = db.prepare('INSERT INTO categories (id, label, h, position) VALUES (?, ?, ?, ?)');
    cats.forEach((c, i) => insert.run(c.id, c.label, c.h, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

const categoryIds = () => new Set(db.prepare('SELECT id FROM categories').all().map((r) => r.id));

const rowToCard = (r, catIds) => ({
  id: r.id,
  columnId: r.column_id,
  title: r.title,
  notes: r.notes,
  type: TYPES.includes(r.type) ? r.type : 'question',
  category: catIds.has(r.category) ? r.category : '',
  importance: r.importance || '',
  urgency: r.urgency || '',
  effort: effortVal(r.effort),
  control: controlVal(r.control),
  effortSrc: srcVal(r.effort_src),
  controlSrc: srcVal(r.control_src),
  deadline: deadlineVal(r.deadline),
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

// The live board is the questions that are neither soft-deleted nor still
// awaiting the user's approval.
function readBoard() {
  const catIds = categoryIds();
  const rows = db.prepare(
    'SELECT * FROM cards WHERE deleted_at IS NULL AND pending = 0 ORDER BY position ASC').all();
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)), categories: readCategories() };
}

// Cards the Assistant proposed, oldest first, still waiting to be accepted.
function readProposals() {
  const catIds = categoryIds();
  const rows = db.prepare(
    'SELECT * FROM cards WHERE deleted_at IS NULL AND pending = 1 ORDER BY created_at ASC').all();
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)) };
}

// The Trash is the soft-deleted questions, newest deletion first. They are still
// in the database and can be restored (re-added by the client) until purged.
function readTrash() {
  const catIds = categoryIds();
  const rows = db.prepare('SELECT * FROM cards WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC').all();
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)) };
}

/** Validate and coerce one incoming card; returns null if it has no title. */
function cleanCard(raw, now, catIds) {
  if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) {
    return null;
  }
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : cryptoId(),
    columnId: COLUMN_IDS.includes(raw.columnId) ? raw.columnId : 'inbox',
    title: raw.title.trim(),
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    type: TYPES.includes(raw.type) ? raw.type : 'question',
    category: catIds.has(raw.category) ? raw.category : '',
    importance: iuVal(raw.importance),
    urgency: iuVal(raw.urgency),
    effort: effortVal(raw.effort),
    control: controlVal(raw.control),
    effortSrc: srcVal(raw.effortSrc),
    controlSrc: srcVal(raw.controlSrc),
    deadline: deadlineVal(raw.deadline),
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
 * a partial or accidental save can never destroy a question; it only moves to
 * the Trash, from where it can be restored. Upserting a card clears its
 * deleted_at, so re-adding or restoring a question brings it back to life.
 *
 * Returns { board, created } — `created` is how many of these cards the database
 * had never seen, which is what triggers a backup.
 */
function writeBoard(cards) {
  const now = Date.now();
  const catIds = categoryIds();
  const clean = cards.map((c) => cleanCard(c, now, catIds)).filter(Boolean);
  const keep = new Set(clean.map((c) => c.id));

  // Deliberately every row, not just the live ones: a card restored from the
  // Trash has an id the table already knows, and bringing back an old thought
  // is not the same as capturing a new one.
  const known = new Set(db.prepare('SELECT id FROM cards').all().map((r) => r.id));
  const created = clean.reduce((n, c) => (known.has(c.id) ? n : n + 1), 0);

  const softDelete = db.prepare('UPDATE cards SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL');
  const upsert = db.prepare(`
    INSERT INTO cards (id, column_id, title, notes, type, category, importance, urgency,
                       effort, control, effort_src, control_src, deadline,
                       num, tags, created_at, updated_at, position, deleted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
    ON CONFLICT(id) DO UPDATE SET
      column_id = excluded.column_id, title = excluded.title, notes = excluded.notes,
      type = excluded.type, category = excluded.category,
      importance = excluded.importance, urgency = excluded.urgency,
      effort = excluded.effort, control = excluded.control,
      effort_src = excluded.effort_src, control_src = excluded.control_src,
      deadline = excluded.deadline,
      num = excluded.num, tags = excluded.tags,
      created_at = excluded.created_at, updated_at = excluded.updated_at, position = excluded.position,
      deleted_at = NULL
  `);
  // NOTE: `pending` is deliberately absent from both the column list and the
  // conflict SET, so a board save can neither create a proposal nor silently
  // accept one. Only /api/proposals/:id/confirm clears that flag.

  db.exec('BEGIN');
  try {
    // `AND pending = 0` is load-bearing: the browser cannot see proposals, so it
    // never sends them, and without this clause every save would archive them.
    for (const { id } of db.prepare(
      'SELECT id FROM cards WHERE deleted_at IS NULL AND pending = 0').all()) {
      if (!keep.has(id)) softDelete.run(now, id);
    }
    clean.forEach((c, i) =>
      upsert.run(c.id, c.columnId, c.title, c.notes, c.type, c.category, c.importance, c.urgency,
        c.effort, c.control, c.effortSrc, c.controlSrc, c.deadline,
        c.num, JSON.stringify(c.tags), c.createdAt, c.updatedAt, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return { board: readBoard(), created };
}

/**
 * Store a card the Assistant proposed. It is durable immediately — losing a
 * suggestion to a crash would be its own kind of data loss — but `pending = 1`
 * keeps it off the board until the user accepts it. Returns the stored proposal,
 * or null if the card had no usable title.
 */
function writeProposal(raw) {
  const now = Date.now();
  const catIds = categoryIds();
  const card = cleanCard(raw, now, catIds);
  if (!card) return null;
  db.prepare(`
    INSERT INTO cards (id, column_id, title, notes, type, category, importance, urgency,
                       effort, control, effort_src, control_src, deadline,
                       num, tags, created_at, updated_at, position, deleted_at, pending)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
  `).run(card.id, card.columnId, card.title, card.notes, card.type, card.category,
    card.importance, card.urgency, card.effort, card.control, card.effortSrc,
    card.controlSrc, card.deadline,
    // num stays 0: a ledger number is earned at confirmation, so a rejected
    // proposal never burns one.
    0, JSON.stringify(card.tags), card.createdAt, card.updatedAt, 0);
  return rowToCard(db.prepare('SELECT * FROM cards WHERE id = ?').get(card.id), catIds);
}

/**
 * Accept a proposal: it becomes an ordinary board card. Returns false if there
 * is no such pending card, so confirming twice (or confirming something already
 * live) is a 404 rather than a silent no-op.
 */
function confirmProposal(id) {
  return db.prepare(
    'UPDATE cards SET pending = 0, updated_at = ? WHERE id = ? AND pending = 1 AND deleted_at IS NULL',
  ).run(Date.now(), id).changes > 0;
}

/**
 * Decline a proposal. It goes to the Trash, recoverable, rather than being
 * erased — DELETE /api/cards/:id stays the only hard delete in the system.
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
 * Snapshot the database because a new question was just captured.
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
 * Permanently remove one question from the database. This is the deliberate
 * second step ("delete from History") and the only operation that truly erases
 * data. Returns true if a row was removed.
 */
function purgeCard(id) {
  return db.prepare('DELETE FROM cards WHERE id = ?').run(id).changes > 0;
}

// --------------------------------------------------------------------------
// HTTP
// --------------------------------------------------------------------------

const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/styles.css': ['styles.css', 'text/css; charset=utf-8'],
};

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(text);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 5_000_000) reject(new Error('Payload too large')); // ~5 MB guard
    });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // API
  if (path === '/api/state') {
    if (req.method === 'GET') {
      return sendJson(res, 200, readBoard());
    }
    if (req.method === 'PUT') {
      try {
        const parsed = JSON.parse(await readBody(req));
        if (!parsed || !Array.isArray(parsed.cards)) {
          return sendJson(res, 400, { error: 'Body must be { version, cards: [...] }' });
        }
        // Registry first, then cards — so cards referencing a just-added
        // category validate against the fresh registry.
        const cats = sanitizeCategories(parsed.categories);
        if (cats) writeCategories(cats);
        const { board, created } = writeBoard(parsed.cards);
        sendJson(res, 200, board);
        // After the response: one snapshot per save that brought new questions,
        // however many they were. Never before, or the backup would miss them.
        if (created > 0) backupAfterNewCards();
        return;
      } catch (err) {
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // The Trash — soft-deleted questions, recoverable until purged.
  if (path === '/api/trash') {
    if (req.method === 'GET') return sendJson(res, 200, readTrash());
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Proposals — cards the Assistant suggested, awaiting the user's approval.
  // Deliberately NOT part of /api/state: they never travel through a whole-board
  // PUT, so the "never send a partial card list" contract is untouched.
  if (path === '/api/proposals') {
    if (req.method === 'GET') return sendJson(res, 200, readProposals());
    if (req.method === 'POST') {
      try {
        const proposal = writeProposal(JSON.parse(await readBody(req)));
        if (!proposal) return sendJson(res, 400, { error: 'A proposal needs a title' });
        return sendJson(res, 200, proposal);
      } catch (err) {
        return sendJson(res, 400, { error: 'Invalid JSON: ' + err.message });
      }
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Accept or decline one proposal.
  if (path.startsWith('/api/proposals/')) {
    const rest = path.slice('/api/proposals/'.length);
    const slash = rest.lastIndexOf('/');
    const id = decodeURIComponent(rest.slice(0, slash));
    const action = rest.slice(slash + 1);
    if (req.method !== 'POST') return sendJson(res, 405, { error: 'Method not allowed' });
    if (action === 'confirm') {
      if (!confirmProposal(id)) return sendJson(res, 404, { error: 'No such proposal' });
      sendJson(res, 200, readBoard());
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

  // Permanent delete of a single question (the deliberate second step).
  if (path.startsWith('/api/cards/')) {
    const id = decodeURIComponent(path.slice('/api/cards/'.length));
    if (req.method === 'DELETE') {
      if (!id) return sendJson(res, 400, { error: 'Missing card id' });
      return sendJson(res, 200, { ok: purgeCard(id) });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Upstream proxies. Two services, deliberately reported apart: the brain holds
  // the LLM key so the browser never sees it, and the RAG lab is developer
  // tooling that is usually not running. Telling the user "assistant
  // unavailable" because the lab is down would send them restarting a brain
  // that works fine.
  const upstream = path.startsWith('/api/raglab/')
    ? { url: RAGLAB_URL + '/api' + path.slice('/api/raglab'.length), down: 'RAG lab unavailable' }
    : path.startsWith('/api/agent/') || path.startsWith('/api/rag/')
      ? { url: AGENT_URL + path.slice('/api'.length), down: 'assistant unavailable' }
      : null;
  if (upstream) {
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
      const upstream = await fetch(target, {
        method: req.method,
        headers: { 'content-type': req.headers['content-type'] || 'application/json' },
        body,
        signal: AbortSignal.timeout(120000),
      });
      const text = await upstream.text();
      res.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') || 'application/json',
      });
      res.end(text);
    } catch {
      sendJson(res, 503, { error: upstream.down });
    }
    return;
  }

  // Static (whitelisted — no arbitrary filesystem access)
  const entry = STATIC[path];
  if (entry && req.method === 'GET') {
    const [file, type] = entry;
    try {
      const body = await readFile(join(ROOT, normalize(file)));
      res.writeHead(200, { 'Content-Type': type });
      return res.end(body);
    } catch {
      res.writeHead(404).end('Not found');
      return;
    }
  }

  res.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found');
});

server.listen(PORT, () => {
  console.log(`Lodestar running at http://localhost:${PORT}  (db: ${DB_PATH})`);
});
