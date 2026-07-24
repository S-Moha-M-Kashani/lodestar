// tests/server.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';

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
