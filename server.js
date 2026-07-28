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
    deadline    TEXT NOT NULL DEFAULT ''
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

// The live board is only the questions that have not been soft-deleted.
function readBoard() {
  const catIds = categoryIds();
  const rows = db.prepare('SELECT * FROM cards WHERE deleted_at IS NULL ORDER BY position ASC').all();
  return { version: 1, cards: rows.map((r) => rowToCard(r, catIds)), categories: readCategories() };
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

  db.exec('BEGIN');
  try {
    for (const { id } of db.prepare('SELECT id FROM cards WHERE deleted_at IS NULL').all()) {
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

  // Permanent delete of a single question (the deliberate second step).
  if (path.startsWith('/api/cards/')) {
    const id = decodeURIComponent(path.slice('/api/cards/'.length));
    if (req.method === 'DELETE') {
      if (!id) return sendJson(res, 400, { error: 'Missing card id' });
      return sendJson(res, 200, { ok: purgeCard(id) });
    }
    return sendJson(res, 405, { error: 'Method not allowed' });
  }

  // Assistant/RAG proxy — the brain service holds the LLM key; the browser never sees it.
  if (path.startsWith('/api/agent/') || path.startsWith('/api/rag/')) {
    const target = AGENT_URL + path.slice('/api'.length) + url.search;
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
      sendJson(res, 503, { error: 'assistant unavailable' });
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
