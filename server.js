// Question Board server — serves the static app and persists the board to a
// local SQLite file so questions survive restarts. Zero npm dependencies:
// Node's built-in http server and node:sqlite do all the work.
//
//   node server.js                 # http://localhost:3000, board.db beside this file
//   PORT=4000 BOARD_DB=/tmp/x.db node server.js
//
// The board is stored one row per question. The client sends its whole state
// on every change (PUT /api/state); the server upserts the rows it sees and
// deletes the rows it doesn't — so a deleted question is the only thing that
// ever leaves the database.

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 3000;
const DB_PATH = process.env.BOARD_DB || join(ROOT, 'board.db');

const COLUMN_IDS = ['inbox', 'to-research', 'in-progress', 'answered'];
const PRIORITIES = ['high', 'medium', 'low'];

// --------------------------------------------------------------------------
// Database
// --------------------------------------------------------------------------

const db = new DatabaseSync(DB_PATH);
db.exec(`
  CREATE TABLE IF NOT EXISTS cards (
    id         TEXT PRIMARY KEY,
    column_id  TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    notes      TEXT    NOT NULL DEFAULT '',
    priority   TEXT    NOT NULL DEFAULT 'medium',
    num        INTEGER NOT NULL DEFAULT 0,
    tags       TEXT    NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
  );
`);

const rowToCard = (r) => ({
  id: r.id,
  columnId: r.column_id,
  title: r.title,
  notes: r.notes,
  priority: r.priority,
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

function readBoard() {
  const rows = db.prepare('SELECT * FROM cards ORDER BY position ASC').all();
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
    num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
    tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
    createdAt: Number.isFinite(raw.createdAt) ? raw.createdAt : now,
    updatedAt: Number.isFinite(raw.updatedAt) ? raw.updatedAt : now,
  };
}

const cryptoId = () => 'id-' + Math.random().toString(36).slice(2) + Date.now().toString(36);

/** Replace the stored board with `cards`: upsert what's present, delete the rest. */
function writeBoard(cards) {
  const now = Date.now();
  const clean = cards.map((c) => cleanCard(c, now)).filter(Boolean);
  const keep = new Set(clean.map((c) => c.id));

  const del = db.prepare('DELETE FROM cards WHERE id = ?');
  const upsert = db.prepare(`
    INSERT INTO cards (id, column_id, title, notes, priority, num, tags, created_at, updated_at, position)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      column_id = excluded.column_id, title = excluded.title, notes = excluded.notes,
      priority = excluded.priority, num = excluded.num, tags = excluded.tags,
      created_at = excluded.created_at, updated_at = excluded.updated_at, position = excluded.position
  `);

  db.exec('BEGIN');
  try {
    for (const { id } of db.prepare('SELECT id FROM cards').all()) {
      if (!keep.has(id)) del.run(id);
    }
    clean.forEach((c, i) =>
      upsert.run(c.id, c.columnId, c.title, c.notes, c.priority, c.num, JSON.stringify(c.tags), c.createdAt, c.updatedAt, i));
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
  return readBoard();
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
