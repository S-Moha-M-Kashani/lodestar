// Question Board server — serves the static app and persists the board to a
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
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 3000;
const DB_PATH = process.env.BOARD_DB || join(ROOT, 'board.db');

const COLUMN_IDS = ['inbox', 'to-research', 'in-progress', 'answered'];
const PRIORITIES = ['high', 'medium', 'low'];
const iuVal = (v) => (v === 'high' || v === 'low' ? v : '');

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
    importance TEXT    NOT NULL DEFAULT '',
    urgency    TEXT    NOT NULL DEFAULT '',
    num        INTEGER NOT NULL DEFAULT 0,
    tags       TEXT    NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER
  );
`);

// Migrate databases created before newer columns existed: add any that are
// missing so older board.db files keep working untouched. deleted_at is NULL
// for a live question and a timestamp once it has been soft-deleted (trashed).
const columnNames = new Set(db.prepare('PRAGMA table_info(cards)').all().map((c) => c.name));
if (!columnNames.has('importance')) db.exec("ALTER TABLE cards ADD COLUMN importance TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('urgency')) db.exec("ALTER TABLE cards ADD COLUMN urgency TEXT NOT NULL DEFAULT ''");
if (!columnNames.has('deleted_at')) db.exec('ALTER TABLE cards ADD COLUMN deleted_at INTEGER');

const rowToCard = (r) => ({
  id: r.id,
  columnId: r.column_id,
  title: r.title,
  notes: r.notes,
  priority: r.priority,
  importance: r.importance || '',
  urgency: r.urgency || '',
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
  const rows = db.prepare('SELECT * FROM cards WHERE deleted_at IS NULL ORDER BY position ASC').all();
  return { version: 1, cards: rows.map(rowToCard) };
}

// The Trash is the soft-deleted questions, newest deletion first. They are still
// in the database and can be restored (re-added by the client) until purged.
function readTrash() {
  const rows = db.prepare('SELECT * FROM cards WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC').all();
  return { version: 1, cards: rows.map(rowToCard) };
}

/** Validate and coerce one incoming card; returns null if it has no title. */
function cleanCard(raw, now) {
  if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) {
    return null;
  }
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : cryptoId(),
    columnId: COLUMN_IDS.includes(raw.columnId) ? raw.columnId : 'inbox',
    title: raw.title.trim(),
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    priority: PRIORITIES.includes(raw.priority) ? raw.priority : 'medium',
    importance: iuVal(raw.importance),
    urgency: iuVal(raw.urgency),
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
 */
function writeBoard(cards) {
  const now = Date.now();
  const clean = cards.map((c) => cleanCard(c, now)).filter(Boolean);
  const keep = new Set(clean.map((c) => c.id));

  const softDelete = db.prepare('UPDATE cards SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL');
  const upsert = db.prepare(`
    INSERT INTO cards (id, column_id, title, notes, priority, importance, urgency, num, tags, created_at, updated_at, position, deleted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
    ON CONFLICT(id) DO UPDATE SET
      column_id = excluded.column_id, title = excluded.title, notes = excluded.notes,
      priority = excluded.priority, importance = excluded.importance, urgency = excluded.urgency,
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
      upsert.run(c.id, c.columnId, c.title, c.notes, c.priority, c.importance, c.urgency, c.num, JSON.stringify(c.tags), c.createdAt, c.updatedAt, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return readBoard();
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
  '/sample-import.json': ['sample-import.json', 'application/json; charset=utf-8'],
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
        return sendJson(res, 200, writeBoard(parsed.cards));
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
  console.log(`Question Board running at http://localhost:${PORT}  (db: ${DB_PATH})`);
});
