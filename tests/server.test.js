// tests/server.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('GET /api/state returns version, cards, seeded categories', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state');
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.version, 1);
    assert.ok(Array.isArray(body.cards));
    assert.equal(body.categories.length, 9); // seeded defaults
    assert.ok(body.categories.some((c) => c.id === 'work'));
  } finally { await s.stop(); }
});

test('PUT /api/state persists a card and echoes full board', async () => {
  const s = await startServer();
  try {
    const put = await fetch(s.base + '/api/state', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [{ id: 'c1', columnId: 'inbox', title: 'Hello' }] }),
    });
    assert.equal(put.status, 200);
    const body = await put.json();
    assert.equal(body.cards.length, 1);
    assert.equal(body.cards[0].title, 'Hello');
    assert.equal(body.cards[0].columnId, 'inbox');
  } finally { await s.stop(); }
});

test('omitting a live card from PUT soft-deletes it into trash', async () => {
  const s = await startServer();
  try {
    await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [
        { id: 'a', columnId: 'inbox', title: 'Keep me' },
        { id: 'b', columnId: 'inbox', title: 'Drop me' },
      ] }),
    });
    // Second PUT omits 'b'
    await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [{ id: 'a', columnId: 'inbox', title: 'Keep me' }] }),
    });
    const state = await (await fetch(s.base + '/api/state')).json();
    assert.deepEqual(state.cards.map((c) => c.id), ['a']);
    const trash = await (await fetch(s.base + '/api/trash')).json();
    assert.ok(trash.cards.some((c) => c.id === 'b'));
    assert.equal(trash.categories, undefined); // trash has no categories key
  } finally { await s.stop(); }
});

test('re-including a trashed card restores it', async () => {
  const s = await startServer();
  try {
    const put = (cards) => fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards }),
    });
    await put([{ id: 'x', columnId: 'inbox', title: 'Card X' }]);
    await put([]); // soft-delete x
    await put([{ id: 'x', columnId: 'inbox', title: 'Card X' }]); // restore
    const state = await (await fetch(s.base + '/api/state')).json();
    assert.ok(state.cards.some((c) => c.id === 'x'));
  } finally { await s.stop(); }
});

test('wrong methods return 405 with the shared error body', async () => {
  const s = await startServer();
  try {
    for (const [path, method] of [
      ['/api/state', 'DELETE'],
      ['/api/trash', 'POST'],
      ['/api/cards/anything', 'GET'],
    ]) {
      const res = await fetch(s.base + path, { method });
      assert.equal(res.status, 405, `${method} ${path}`);
      assert.deepEqual(await res.json(), { error: 'Method not allowed' });
    }
  } finally { await s.stop(); }
});

test('malformed JSON body → 400 Invalid JSON', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' }, body: '{not json',
    });
    assert.equal(res.status, 400);
    const body = await res.json();
    assert.match(body.error, /^Invalid JSON: /);
  } finally { await s.stop(); }
});

test('non-array cards → 400 shape error', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: 'nope' }),
    });
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: 'Body must be { version, cards: [...] }' });
  } finally { await s.stop(); }
});

test('card with blank title is silently dropped (not an error)', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [
        { id: 'ok', columnId: 'inbox', title: 'Real' },
        { id: 'blank', columnId: 'inbox', title: '   ' },
      ] }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.deepEqual(body.cards.map((c) => c.id), ['ok']);
  } finally { await s.stop(); }
});

test('card with no id gets one auto-generated', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [{ columnId: 'inbox', title: 'No id here' }] }),
    });
    const body = await res.json();
    assert.equal(body.cards.length, 1);
    assert.ok(typeof body.cards[0].id === 'string' && body.cards[0].id.length > 0);
  } finally { await s.stop(); }
});

test('DELETE /api/cards/ with empty id → 400 Missing card id', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/cards/', { method: 'DELETE' });
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: 'Missing card id' });
  } finally { await s.stop(); }
});

test('DELETE unknown id → 200 { ok: false }', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/cards/does-not-exist', { method: 'DELETE' });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: false });
  } finally { await s.stop(); }
});

test('unknown path → 404 text/plain Not found', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/nope');
    assert.equal(res.status, 404);
    assert.match(res.headers.get('content-type') || '', /text\/plain/);
    assert.equal(await res.text(), 'Not found');
  } finally { await s.stop(); }
});

test('~5MB payload cap → 400 Invalid JSON: Payload too large', async () => {
  const s = await startServer();
  try {
    const big = 'x'.repeat(5_000_001);
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [], pad: big }),
    });
    assert.equal(res.status, 400);
    const body = await res.json();
    assert.equal(body.error, 'Invalid JSON: Payload too large');
  } finally { await s.stop(); }
});

test('proxy returns 503 assistant unavailable when brain is down', async () => {
  // Point AGENT_URL at a port with nothing listening.
  const s = await startServer({ env: { AGENT_URL: 'http://127.0.0.1:59999' } });
  try {
    const res = await fetch(s.base + '/api/agent/chat', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [] }),
    });
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: 'assistant unavailable' });
  } finally { await s.stop(); }
});

test('/api/rag/* is also proxied (503 when brain down)', async () => {
  const s = await startServer({ env: { AGENT_URL: 'http://127.0.0.1:59999' } });
  try {
    const res = await fetch(s.base + '/api/rag/communities');
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: 'assistant unavailable' });
  } finally { await s.stop(); }
});

test('static index.html served at / with html content-type', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/');
    assert.equal(res.status, 200);
    assert.match(res.headers.get('content-type') || '', /text\/html/);
    const text = await res.text();
    assert.match(text, /id="board"/); // mount point is <main id="board">, not <div>
  } finally { await s.stop(); }
});

test('whitelisted path with wrong method falls through to 404', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/app.js', { method: 'POST' });
    assert.equal(res.status, 404);
  } finally { await s.stop(); }
});

test('boot migrates a legacy cards table missing newer columns', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-legacy-'));
  const dbPath = join(dir, 'board.db');
  // Create an old-schema DB with only the original columns.
  const seed = new DatabaseSync(dbPath);
  seed.exec(`CREATE TABLE cards (
    id TEXT PRIMARY KEY, column_id TEXT NOT NULL, title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'medium',
    num INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
  )`);
  seed.prepare(`INSERT INTO cards (id, column_id, title, created_at, updated_at)
                VALUES ('legacy1', 'inbox', 'Old card', 1, 1)`).run();
  seed.close();

  const s = await startServer({ env: { BOARD_DB: dbPath } });
  try {
    const state = await (await fetch(s.base + '/api/state')).json();
    const card = state.cards.find((c) => c.id === 'legacy1');
    assert.ok(card, 'legacy card survived migration');
    assert.equal(card.effort, 'medium');   // added column default
    assert.equal(card.control, 'influence'); // added column default
    assert.equal(card.type, 'question');
  } finally { await s.stop(); }
});
