// tests/server.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';
import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, readFileSync, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// This is an integration test.
test('GET /api/state returns version, cards, seeded categories', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state');
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.version, 1);
    assert.ok(Array.isArray(body.cards));
    assert.equal(body.categories.length, 10); // seeded defaults, Dream included
    assert.ok(body.categories.some((c) => c.id === 'work'));
  } finally { await s.stop(); }
});

// This is an integration test. The plan travels with the card, and the two
// rules that make it more than another text column are enforced here too: a
// plan mirrors the deadline until somebody sets one, and the type 'plan' —
// retired on 2026-08-28 — is stored as a task rather than coerced to a
// question, which would have re-filed years of work as unanswered.
test('PUT /api/state keeps a card plan, and retires the plan type', async () => {
  const s = await startServer();
  try {
    const put = await fetch(s.base + '/api/state', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [
        { id: 'own', columnId: 'inbox', title: 'Sail an ocean', plan: '2031-06', planSrc: 'user' },
        { id: 'auto', columnId: 'inbox', title: 'Tax forms', deadline: '2026-09-01' },
        { id: 'junk', columnId: 'inbox', title: 'Half a plan', plan: '2027-02-30', planSrc: 'user' },
        { id: 'old', columnId: 'inbox', title: 'A card that says it is a plan', type: 'plan' },
      ] }),
    });
    assert.equal(put.status, 200);
    const byId = Object.fromEntries((await put.json()).cards.map((c) => [c.id, c]));

    assert.equal(byId.own.plan, '2031-06');
    assert.equal(byId.own.planSrc, 'user');
    // Nobody set this one, so the deadline is the plan.
    assert.equal(byId.auto.plan, '2026-09-01');
    assert.equal(byId.auto.planSrc, 'auto');
    // The impossible day goes; the month someone did mean stays.
    assert.equal(byId.junk.plan, '2027-02');
    assert.equal(byId.old.type, 'task');

    // And it survives a re-read from the database rather than only the echo.
    const again = await (await fetch(s.base + '/api/state')).json();
    assert.equal(again.cards.find((c) => c.id === 'own').plan, '2031-06');
  } finally { await s.stop(); }
});

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
test('card with no id gets one auto-generated', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [{ columnId: 'inbox', title: 'No id here' }] }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.cards.length, 1);
    assert.ok(typeof body.cards[0].id === 'string' && body.cards[0].id.length > 0);
  } finally { await s.stop(); }
});

// This is an integration test.
test('DELETE /api/cards/ with empty id → 400 Missing card id', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/cards/', { method: 'DELETE' });
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { error: 'Missing card id' });
  } finally { await s.stop(); }
});

// This is an integration test.
test('DELETE unknown id → 200 { ok: false }', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/cards/does-not-exist', { method: 'DELETE' });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: false });
  } finally { await s.stop(); }
});

// This is an integration test.
test('unknown path → 404 text/plain Not found', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/nope');
    assert.equal(res.status, 404);
    assert.match(res.headers.get('content-type') || '', /text\/plain/);
    assert.equal(await res.text(), 'Not found');
  } finally { await s.stop(); }
});

// This is an integration test.
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

// This is an integration test.
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

// This is an integration test.
test('/api/rag/* is also proxied (503 when brain down)', async () => {
  const s = await startServer({ env: { AGENT_URL: 'http://127.0.0.1:59999' } });
  try {
    const res = await fetch(s.base + '/api/rag/recall');
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: 'assistant unavailable' });
  } finally { await s.stop(); }
});

// ---- Rate limit on the assistant proxy -----------------------------------
// A token bucket in front of the brain. One runaway client loop must not be
// able to spend the user's API credit or pin a local model for minutes, and the
// board server is the only place that can say no — the brain answers whatever
// reaches it. The two knobs exist so this test can trip the limit in three
// requests; the defaults are far above anything a person does by hand.

// This is an integration test — a real server process in front of a real stub upstream.
test('the assistant proxy answers 429 with Retry-After once the bucket is empty', async () => {
  const seen = [];
  const brain = await startStubBrain((req) => seen.push(req),
    { status: 200, body: { reply: 'ok' } });
  const s = await startServer({ env: {
    AGENT_URL: brain.url, LODESTAR_AGENT_BURST: '2', LODESTAR_AGENT_PER_MIN: '1' } });
  try {
    const ask = () => fetch(s.base + '/api/agent/chat', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [] }),
    });
    assert.equal((await ask()).status, 200);
    assert.equal((await ask()).status, 200);
    const limited = await ask();
    assert.equal(limited.status, 429);
    assert.deepEqual(await limited.json(), { error: 'Too many assistant requests' });
    // Whole seconds, and never 0 — a client that reads it and retries at once
    // is straight back where it started.
    const after = limited.headers.get('retry-after');
    assert.ok(Number.isInteger(Number(after)) && Number(after) >= 1,
      `Retry-After was ${after}`);
    assert.equal(seen.length, 2, 'the rejected request never reached the brain');
    // The board is local data, not a brain call: being over the assistant's
    // limit must never make the user's own cards unreachable.
    assert.equal((await fetch(s.base + '/api/state')).status, 200);
  } finally { await s.stop(); await brain.stop(); }
});

// How long the stub upstream sits between its two SSE frames. Long enough that
// the gap is unmistakable, short enough that the test costs half a second.
const STREAM_HOLD_MS = 500;

// A stub upstream that emits one SSE frame, waits, then emits a second and
// ends. It holds on its own timer rather than on a signal from the test: a
// buffering proxy never delivers the first frame, so a test that waited for it
// before releasing the upstream would deadlock instead of failing.
function startStubStream() {
  return new Promise((resolve) => {
    const srv = createServer(async (req, res) => {
      for await (const _ of req) { /* drain the request body */ }
      res.writeHead(200, { 'Content-Type': 'text/event-stream',
                           'Cache-Control': 'no-cache' });
      res.write('event: calling\ndata: {"tool":"web_search"}\n\n');
      setTimeout(() => {
        res.write('event: done\ndata: {"reply":"ok"}\n\n');
        res.end();
      }, STREAM_HOLD_MS);
    });
    srv.listen(0, '127.0.0.1', () => resolve({
      url: `http://127.0.0.1:${srv.address().port}`,
      stop: () => new Promise((done) => srv.close(done)),
    }));
  });
}

// This is an integration test — a real server process proxying a real upstream.
test('an SSE upstream reaches the browser frame by frame, not all at the end', async () => {
  // `await upstream.text()` returns a byte-identical response with none of the
  // progress, and neither the status nor the body says so. The only observable
  // difference is *when* the first frame lands, so that is what is asserted:
  // piped, it arrives a hold ahead of the end; buffered, the two coincide.
  const brain = await startStubStream();
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const res = await fetch(s.base + '/api/agent/chat/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [] }),
    });
    assert.equal(res.status, 200);
    assert.match(res.headers.get('content-type') || '', /text\/event-stream/);
    // Without this an intermediary may cache an event stream, and asking the
    // same question twice would replay the first answer's frames.
    assert.equal(res.headers.get('cache-control'), 'no-cache');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let first = '', rest = '', firstAt = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!firstAt) { firstAt = Date.now(); first = decoder.decode(value); }
      else rest += decoder.decode(value);
    }
    const gap = Date.now() - firstAt;

    assert.match(first, /event: calling/);
    assert.match(first + rest, /event: done/);
    assert.ok(gap > STREAM_HOLD_MS / 2,
      `the first frame landed ${gap}ms before the end; the proxy buffered`);
  } finally { await s.stop(); await brain.stop(); }
});

// ---- What the proxy does with an upstream's answer -----------------------
// Neither of these two is about how many upstreams there are — they are about
// not mangling the one there is — so both are aimed at the brain.

// This is an integration test.
test('the proxy forwards POST bodies and query strings unchanged', async () => {
  const seen = [];
  const brain = await startStubBrain((req) => seen.push(req), {
    status: 200, body: { ok: true },
  });
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const res = await fetch(s.base + '/api/agent/chat?stream=0', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true });
    // The '/api' prefix is the board's, not the brain's, and the query string
    // rides along: a proxy that dropped it would silently change the request.
    assert.equal(seen[0].path, '/agent/chat?stream=0');
    assert.deepEqual(JSON.parse(seen[0].body),
      { messages: [{ role: 'user', content: 'hi' }] });
  } finally { await s.stop(); await brain.stop(); }
});

// This is an integration test.
test('an upstream error status passes through instead of becoming a 503', async () => {
  // 503 means "could not reach it". A 400 that arrived as an answer has to keep
  // its own status, or a bad request is indistinguishable from a dead service and
  // the message telling you what was wrong with it never reaches the page.
  const brain = await startStubBrain(() => {}, {
    status: 400, body: { detail: 'unknown model: nope' },
  });
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const res = await fetch(s.base + '/api/agent/chat', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: [] }),
    });
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { detail: 'unknown model: nope' });
  } finally { await s.stop(); await brain.stop(); }
});

// This is an integration test.
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

// This is an integration test.
test('whitelisted path with wrong method falls through to 404', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/js/main.js', { method: 'POST' });
    assert.equal(res.status, 404);
  } finally { await s.stop(); }
});

// This is an integration test.
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
    assert.equal(card.deadline, '');       // added column default
  } finally { await s.stop(); }
});

// This is an integration test.
test('PUT /api/state round-trips a card deadline', async () => {
  const s = await startServer();
  try {
    const put = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [
        { id: 'd1', columnId: 'inbox', title: 'With deadline', deadline: '2026-08-01' },
        { id: 'd2', columnId: 'inbox', title: 'Without deadline' },
      ] }),
    });
    assert.equal(put.status, 200);
    const body = await put.json();
    assert.equal(body.cards.find((c) => c.id === 'd1').deadline, '2026-08-01');
    assert.equal(body.cards.find((c) => c.id === 'd2').deadline, '');
    // Survives a fresh read, not just the PUT echo.
    const state = await (await fetch(s.base + '/api/state')).json();
    assert.equal(state.cards.find((c) => c.id === 'd1').deadline, '2026-08-01');
  } finally { await s.stop(); }
});

// This is an integration test.
test('malformed deadlines are sanitized to empty string', async () => {
  const s = await startServer();
  try {
    const bads = ['not-a-date', '2026-13-45', '01-08-2026', 12345, null, {}];
    const cards = bads.map((deadline, i) =>
      ({ id: `bad${i}`, columnId: 'inbox', title: `Bad ${i}`, deadline }));
    const put = await fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards }),
    });
    assert.equal(put.status, 200);
    const body = await put.json();
    for (const c of body.cards) assert.equal(c.deadline, '', `deadline of ${c.id} not scrubbed`);
  } finally { await s.stop(); }
});

// This is an integration test.
test('deadline survives soft-delete and restore', async () => {
  const s = await startServer();
  try {
    const put = (cards) => fetch(s.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards }),
    });
    await put([{ id: 'dl', columnId: 'inbox', title: 'Dated', deadline: '2026-09-15' }]);
    await put([]); // soft-delete
    const trash = await (await fetch(s.base + '/api/trash')).json();
    assert.equal(trash.cards.find((c) => c.id === 'dl').deadline, '2026-09-15');
  } finally { await s.stop(); }
});

// ---- Habits --------------------------------------------------------------
// A habit is a card you repeat: a target per calendar period, optional reminder
// slots, and the completions themselves. The completions are the only card field
// the user cannot retype from memory, so the server's job is to store them
// exactly and never scrub them by accident.

// Shared with the backup tests further down — a whole-board save is the only
// way a card reaches the server, habit or not.
const putCards = (base, cards) => fetch(base + '/api/state', {
  method: 'PUT', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ version: 1, cards }),
});

// This is an integration test.
test('PUT /api/state round-trips the habit fields', async () => {
  const s = await startServer();
  try {
    const put = await putCards(s.base, [{
      id: 'h1', columnId: 'inbox', title: 'Meditate', type: 'habit',
      habitFreq: 'daily', habitCount: 2, habitTimes: ['07:00', '21:00'],
      habitHistory: { '2026-07-30': [1785431613000] },
    }]);
    assert.equal(put.status, 200);
    const echoed = (await put.json()).cards.find((c) => c.id === 'h1');
    assert.equal(echoed.type, 'habit');
    assert.equal(echoed.habitFreq, 'daily');
    assert.equal(echoed.habitCount, 2);
    assert.deepEqual(echoed.habitTimes, ['07:00', '21:00']);
    assert.deepEqual(echoed.habitHistory, { '2026-07-30': [1785431613000] });
    // Survives a fresh read, not just the PUT echo.
    const state = await (await fetch(s.base + '/api/state')).json();
    const stored = state.cards.find((c) => c.id === 'h1');
    assert.deepEqual(stored.habitHistory, { '2026-07-30': [1785431613000] });
    assert.deepEqual(stored.habitTimes, ['07:00', '21:00']);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a card that says nothing about habits gets the habit defaults', async () => {
  const s = await startServer();
  try {
    const body = await (await putCards(s.base, [
      { id: 'plain', columnId: 'inbox', title: 'Just a question' },
    ])).json();
    const card = body.cards[0];
    assert.equal(card.habitFreq, '');
    assert.equal(card.habitCount, 1);
    assert.deepEqual(card.habitTimes, []);
    assert.deepEqual(card.habitHistory, {});
  } finally { await s.stop(); }
});

// This is an integration test.
test('habit is an accepted card type', async () => {
  const s = await startServer();
  try {
    const body = await (await putCards(s.base, [
      { id: 'h', columnId: 'inbox', title: 'Push-ups', type: 'habit' },
      { id: 'x', columnId: 'inbox', title: 'Nonsense type', type: 'sandwich' },
    ])).json();
    assert.equal(body.cards.find((c) => c.id === 'h').type, 'habit');
    assert.equal(body.cards.find((c) => c.id === 'x').type, 'question');
  } finally { await s.stop(); }
});

// This is an integration test.
test('an unrecognized frequency is scrubbed to empty', async () => {
  const s = await startServer();
  try {
    const bads = ['fortnightly', 'DAILY', 'hourly', 7, null, {}, []];
    const cards = bads.map((habitFreq, i) =>
      ({ id: `f${i}`, columnId: 'inbox', title: `Freq ${i}`, type: 'habit', habitFreq }));
    const body = await (await putCards(s.base, cards)).json();
    for (const c of body.cards) assert.equal(c.habitFreq, '', `habitFreq of ${c.id} not scrubbed`);
  } finally { await s.stop(); }
});

// This is an integration test.
test('every real frequency is kept', async () => {
  const s = await startServer();
  try {
    const freqs = ['daily', 'weekly', 'monthly', 'yearly'];
    const cards = freqs.map((habitFreq, i) =>
      ({ id: `ok${i}`, columnId: 'inbox', title: habitFreq, type: 'habit', habitFreq }));
    const body = await (await putCards(s.base, cards)).json();
    assert.deepEqual(body.cards.map((c) => c.habitFreq).sort(), [...freqs].sort());
  } finally { await s.stop(); }
});

// This is an integration test.
test('habitCount is clamped to 1..99 and always a whole number', async () => {
  const s = await startServer();
  try {
    const cases = [[0, 1], [-4, 1], [500, 99], [2.7, 2], ['3', 3], [NaN, 1], [null, 1], ['x', 1]];
    const cards = cases.map(([habitCount], i) =>
      ({ id: `n${i}`, columnId: 'inbox', title: `Count ${i}`, type: 'habit', habitCount }));
    const body = await (await putCards(s.base, cards)).json();
    cases.forEach(([input, want], i) => {
      const got = body.cards.find((c) => c.id === `n${i}`).habitCount;
      assert.equal(got, want, `habitCount ${JSON.stringify(input)} became ${got}, wanted ${want}`);
    });
  } finally { await s.stop(); }
});

// This is an integration test.
test('habitTimes are normalized: valid, sorted, deduped, and never more than the target', async () => {
  const s = await startServer();
  try {
    const body = await (await putCards(s.base, [{
      id: 't1', columnId: 'inbox', title: 'Times', type: 'habit',
      habitFreq: 'daily', habitCount: 2,
      // out of order, a duplicate, an unpadded hour, an impossible clock time,
      // a non-string — and one more than the target allows.
      habitTimes: ['21:00', '07:00', '07:00', '7:30', '25:99', 12, '13:15'],
    }])).json();
    assert.deepEqual(body.cards[0].habitTimes, ['07:00', '13:15']);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a malformed habit history is replaced with an empty one', async () => {
  const s = await startServer();
  try {
    const bads = ['nope', 42, null, [], [1, 2, 3]];
    const cards = bads.map((habitHistory, i) =>
      ({ id: `bh${i}`, columnId: 'inbox', title: `Bad history ${i}`, type: 'habit', habitHistory }));
    const body = await (await putCards(s.base, cards)).json();
    for (const c of body.cards) {
      assert.deepEqual(c.habitHistory, {}, `habitHistory of ${c.id} not scrubbed`);
    }
  } finally { await s.stop(); }
});

// This is an integration test.
test('history entries that are not period→timestamps are dropped, the good ones kept', async () => {
  const s = await startServer();
  try {
    const body = await (await putCards(s.base, [{
      id: 'mix', columnId: 'inbox', title: 'Mixed history', type: 'habit', habitFreq: 'daily',
      habitHistory: {
        '2026-07-30': [1785431613000, 1785461613000],
        '2026-W31': [1785431613000],       // a real weekly period id
        '2026-07': [1785431613000],        // a real monthly period id
        '2026': [1785431613000],           // a real yearly period id
        'yesterday': [1785431613000],      // not a period id
        '2026-07-29': 'twice',             // not a list
        '2026-07-28': ['noon', null, 1785345213000], // one usable stamp among junk
      },
    }])).json();
    const history = body.cards[0].habitHistory;
    assert.deepEqual(history['2026-07-30'], [1785431613000, 1785461613000]);
    assert.deepEqual(history['2026-W31'], [1785431613000]);
    assert.deepEqual(history['2026-07'], [1785431613000]);
    assert.deepEqual(history['2026'], [1785431613000]);
    assert.equal('yesterday' in history, false);
    assert.equal('2026-07-29' in history, false);
    assert.deepEqual(history['2026-07-28'], [1785345213000]);
  } finally { await s.stop(); }
});

// This is an integration test.
test('history keeps the newest 400 periods so one habit cannot bloat every save', async () => {
  const s = await startServer();
  try {
    // 405 consecutive days. Period ids sort lexicographically, so "newest" is
    // simply the largest keys.
    const days = [];
    for (let i = 0; i < 405; i++) {
      days.push(new Date(Date.UTC(2025, 0, 1) + i * 86400000).toISOString().slice(0, 10));
    }
    const habitHistory = Object.fromEntries(days.map((d, i) => [d, [1_700_000_000_000 + i]]));
    const body = await (await putCards(s.base, [{
      id: 'long', columnId: 'inbox', title: 'Long runner', type: 'habit',
      habitFreq: 'daily', habitHistory,
    }])).json();
    const kept = Object.keys(body.cards[0].habitHistory).sort();
    assert.equal(kept.length, 400);
    assert.deepEqual(kept, days.slice(-400), 'the oldest 5 periods should be the ones dropped');
  } finally { await s.stop(); }
});

// This is an integration test.
test('habit history is not tied to the card type — retyping a card never erases it', async () => {
  const s = await startServer();
  try {
    // Someone stamps a habit as a task by mistake. A year of completions must
    // still be there when they stamp it back.
    const history = { '2026-07-30': [1785431613000] };
    await putCards(s.base, [{
      id: 'flip', columnId: 'inbox', title: 'Meditate', type: 'habit',
      habitFreq: 'daily', habitCount: 2, habitHistory: history,
    }]);
    const asTask = await (await putCards(s.base, [{
      id: 'flip', columnId: 'inbox', title: 'Meditate', type: 'task',
      habitFreq: 'daily', habitCount: 2, habitHistory: history,
    }])).json();
    assert.equal(asTask.cards[0].type, 'task');
    assert.deepEqual(asTask.cards[0].habitHistory, history);
    assert.equal(asTask.cards[0].habitFreq, 'daily');
    assert.equal(asTask.cards[0].habitCount, 2);
  } finally { await s.stop(); }
});

// This is an integration test.
test('habit history survives soft-delete and restore', async () => {
  const s = await startServer();
  try {
    const history = { '2026-07-30': [1785431613000, 1785461613000] };
    await putCards(s.base, [{
      id: 'hd', columnId: 'inbox', title: 'Push-ups', type: 'habit',
      habitFreq: 'daily', habitCount: 3, habitHistory: history,
    }]);
    await putCards(s.base, []); // soft-delete into the Trash
    const trash = await (await fetch(s.base + '/api/trash')).json();
    const trashed = trash.cards.find((c) => c.id === 'hd');
    assert.deepEqual(trashed.habitHistory, history);
    assert.equal(trashed.habitCount, 3);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a legacy database gains the habit columns with their defaults', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'qboard-habit-migrate-'));
  const dbPath = join(dir, 'board.db');
  const seed = new DatabaseSync(dbPath);
  seed.exec(`CREATE TABLE cards (
    id TEXT PRIMARY KEY, column_id TEXT NOT NULL, title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'medium',
    num INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
  )`);
  seed.prepare(`INSERT INTO cards (id, column_id, title, created_at, updated_at)
                VALUES ('before-habits', 'inbox', 'Older than habits', 1, 1)`).run();
  seed.close();

  const s = await startServer({ env: { BOARD_DB: dbPath } });
  try {
    const state = await (await fetch(s.base + '/api/state')).json();
    const card = state.cards.find((c) => c.id === 'before-habits');
    assert.ok(card, 'the pre-habit card survived migration');
    assert.equal(card.habitFreq, '');
    assert.equal(card.habitCount, 1);
    assert.deepEqual(card.habitTimes, []);
    assert.deepEqual(card.habitHistory, {});
  } finally { await s.stop(); }
});

// This is an integration test.
test('a proposal can carry habit fields through the confirmation gate', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/proposals', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        title: 'Stretch every morning', type: 'habit',
        habitFreq: 'daily', habitCount: 1, habitTimes: ['08:00'],
      }),
    });
    assert.equal(res.status, 200);
    const proposal = await res.json();
    assert.equal(proposal.type, 'habit');
    assert.equal(proposal.habitFreq, 'daily');
    assert.deepEqual(proposal.habitTimes, ['08:00']);

    const confirm = await fetch(`${s.base}/api/proposals/${proposal.id}/confirm`, { method: 'POST' });
    assert.equal(confirm.status, 200);
    const state = await (await fetch(s.base + '/api/state')).json();
    const live = state.cards.find((c) => c.id === proposal.id);
    assert.equal(live.habitFreq, 'daily');
    assert.deepEqual(live.habitTimes, ['08:00']);
  } finally { await s.stop(); }
});

// ---- Voice transcription proxy ------------------------------------------
// The browser records audio and posts base64 WAV; the Node proxy must hand it
// to the brain untouched (the OpenRouter key lives only there).

// Minimal stand-in for the brain: echoes back what it was handed. `reply` lets a
// test choose the upstream status/body so error passthrough can be asserted.
function startStubBrain(handler, reply = { status: 200, body: { text: 'transcribed words' } }) {
  return new Promise((resolve) => {
    const srv = createServer(async (req, res) => {
      let body = '';
      for await (const chunk of req) body += chunk;
      handler({ method: req.method, path: req.url, contentType: req.headers['content-type'], body });
      res.writeHead(reply.status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(reply.body));
    });
    srv.listen(0, '127.0.0.1', () => resolve({
      url: `http://127.0.0.1:${srv.address().port}`,
      stop: () => new Promise((done) => srv.close(done)),
    }));
  });
}

// This is an integration test.
test('POST /api/agent/transcribe reaches the brain with the body intact', async () => {
  const seen = [];
  const brain = await startStubBrain((req) => seen.push(req));
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const payload = { audio: Buffer.from('fake-wav-bytes').toString('base64'), format: 'wav', model: 'my/omni' };
    const res = await fetch(s.base + '/api/agent/transcribe', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { text: 'transcribed words' });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].method, 'POST');
    assert.equal(seen[0].path, '/agent/transcribe'); // /api stripped, nothing else
    assert.match(seen[0].contentType, /application\/json/);
    assert.deepEqual(JSON.parse(seen[0].body), payload); // byte-for-byte passthrough
  } finally { await s.stop(); await brain.stop(); }
});

// This is an integration test.
test('transcribe returns 503 when the brain is down', async () => {
  const s = await startServer({ env: { AGENT_URL: 'http://127.0.0.1:59999' } });
  try {
    const res = await fetch(s.base + '/api/agent/transcribe', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ audio: 'AAAA', format: 'wav' }),
    });
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: 'assistant unavailable' });
  } finally { await s.stop(); }
});

// This is an integration test.
test('a transcription failure keeps the brain\'s reason instead of flattening it', async () => {
  // When a model silently drops the audio the brain answers 502 with a detail
  // naming the model. The proxy must pass that through verbatim so the browser
  // can show the real cause rather than "check that the brain is running".
  const detail = "the model 'nvidia/nemotron-...:free' did not receive the audio";
  const brain = await startStubBrain(() => {}, { status: 502, body: { detail } });
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const res = await fetch(s.base + '/api/agent/transcribe', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ audio: 'AAAA', format: 'wav' }),
    });
    assert.equal(res.status, 502);
    assert.deepEqual(await res.json(), { detail });
  } finally { await s.stop(); await brain.stop(); }
});

// This is an integration test.
test('oversized audio is rejected as too large, not blamed on the brain', async () => {
  // Audio is the only payload that realistically hits the ~5MB body cap. The
  // caller must be told the recording was too long — reporting "assistant
  // unavailable" would send them hunting a service that is running fine.
  const seen = [];
  const brain = await startStubBrain((req) => seen.push(req));
  const s = await startServer({ env: { AGENT_URL: brain.url } });
  try {
    const res = await fetch(s.base + '/api/agent/transcribe', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ audio: 'A'.repeat(5_000_001), format: 'wav' }),
    });
    assert.equal(res.status, 413);
    assert.equal((await res.json()).error, 'Payload too large');
    assert.equal(seen.length, 0); // never forwarded upstream
  } finally { await s.stop(); await brain.stop(); }
});

// ---- Backup on a new card -------------------------------------------------
// A snapshot is taken when a card the database has never seen arrives, and at
// no other time. The backup runs in a detached child process so the request is
// never blocked, which is why these tests poll instead of asserting inline.
//
// Every test here points LODESTAR_BACKUP_DIR at a temp directory and
// LODESTAR_RCLONE_BIN at a path that does not exist: the suite must never write
// into the real backups/ history, and must never push to Google Drive.

function backupSandbox() {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-bk-'));
  return {
    dir,
    env: {
      LODESTAR_BACKUP_ON_WRITE: '1',
      LODESTAR_BACKUP_DIR: dir,
      LODESTAR_RCLONE_BIN: join(dir, 'no-such-rclone'),
    },
  };
}

// Snapshots land in the db/ subfolder of the backup dir (json/ holds the
// importable exports beside it).
const snapshots = (dir) => {
  const dbDir = join(dir, 'db');
  return (existsSync(dbDir) ? readdirSync(dbDir) : []).filter((f) => f.startsWith('board-') && f.endsWith('.db'));
};

// Wait until at least `n` snapshots exist, or the timeout expires.
async function waitForSnapshots(dir, n, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (snapshots(dir).length >= n) break;
    await new Promise((r) => setTimeout(r, 50));
  }
  return snapshots(dir);
}

// Give a backup that should NOT happen enough time to prove it didn't.
const settle = (ms = 1200) => new Promise((r) => setTimeout(r, ms));

// This is an integration test.
test('PUT with a never-before-seen card triggers one backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await putCards(s.base, [{ id: 'n1', columnId: 'inbox', title: 'A new thought' }]);
    const files = await waitForSnapshots(bk.dir, 1);
    assert.equal(files.length, 1, 'a new card must produce exactly one snapshot');
    // The snapshot is taken after the commit, so it contains the new card.
    const snap = new DatabaseSync(join(bk.dir, 'db', files[0]), { readOnly: true });
    const row = snap.prepare('SELECT title FROM cards WHERE id = ?').get('n1');
    assert.equal(row.title, 'A new thought');
  } finally { await s.stop(); }
});

// This is an integration test.
test('PUT that only edits an existing card triggers no further backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await putCards(s.base, [{ id: 'e1', columnId: 'inbox', title: 'Original' }]);
    await waitForSnapshots(bk.dir, 1);
    // Edit the title, move it, and reorder — none of these is a new entry.
    await putCards(s.base, [{ id: 'e1', columnId: 'in-progress', title: 'Edited', notes: 'more' }]);
    await settle();
    assert.equal(snapshots(bk.dir).length, 1, 'editing an existing card must not back up');
  } finally { await s.stop(); }
});

// This is an integration test.
test('PUT that restores a soft-deleted card triggers no further backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await putCards(s.base, [{ id: 'r1', columnId: 'inbox', title: 'Comes and goes' }]);
    await waitForSnapshots(bk.dir, 1);
    await putCards(s.base, []);            // soft-delete r1 into the trash
    await settle();
    assert.equal(snapshots(bk.dir).length, 1, 'a delete is not a new entry');
    await putCards(s.base, [{ id: 'r1', columnId: 'inbox', title: 'Comes and goes' }]);
    await settle();
    // The id is already known, so restoring an old thought is not capturing a new one.
    assert.equal(snapshots(bk.dir).length, 1, 'restoring a trashed card must not back up');
  } finally { await s.stop(); }
});

// This is an integration test.
test('one PUT carrying five new cards triggers exactly one backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await putCards(s.base, [1, 2, 3, 4, 5].map((n) => (
      { id: `bulk${n}`, columnId: 'inbox', title: `Bulk ${n}` })));
    await waitForSnapshots(bk.dir, 1);
    await settle();
    assert.equal(snapshots(bk.dir).length, 1, 'one payload means one snapshot, not one per card');
  } finally { await s.stop(); }
});

// This is an integration test.
test('LODESTAR_BACKUP_ON_WRITE=0 disables write-triggered backups', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: { ...bk.env, LODESTAR_BACKUP_ON_WRITE: '0' } });
  try {
    await putCards(s.base, [{ id: 'off1', columnId: 'inbox', title: 'No backup please' }]);
    await settle();
    assert.equal(snapshots(bk.dir).length, 0, 'the kill switch must suppress the backup');
  } finally { await s.stop(); }
});

// ---- Agent card confirmation gate -----------------------------------------
// A card the Assistant invents is a PROPOSAL: it lives in the cards table with
// pending = 1, is invisible to the board, and becomes real only when the user
// confirms it. Rejecting it sends it to the Trash, so DELETE /api/cards/:id
// stays the board's only hard delete.

const postProposal = (base, card) =>
  fetch(base + '/api/proposals', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(card),
  });

const getJson = async (base, path) => (await fetch(base + path)).json();
const act = (base, id, what) =>
  fetch(`${base}/api/proposals/${encodeURIComponent(id)}/${what}`, { method: 'POST' });

// This is an integration test.
test('POST /api/proposals creates a card the board does not show', async () => {
  const s = await startServer();
  try {
    const res = await postProposal(s.base, { title: 'Agent idea', type: 'idea' });
    assert.equal(res.status, 200);
    const proposal = await res.json();
    assert.ok(proposal.id, 'the server assigns an id');
    assert.equal(proposal.title, 'Agent idea');

    const board = await getJson(s.base, '/api/state');
    assert.equal(board.cards.filter((c) => c.id === proposal.id).length, 0,
      'a proposal must not appear on the board');

    const pending = await getJson(s.base, '/api/proposals');
    assert.deepEqual(pending.cards.map((c) => c.title), ['Agent idea']);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a proposal with a blank title is rejected as a bad request', async () => {
  const s = await startServer();
  try {
    const res = await postProposal(s.base, { title: '   ' });
    assert.equal(res.status, 400);
    const pending = await getJson(s.base, '/api/proposals');
    assert.equal(pending.cards.length, 0);
  } finally { await s.stop(); }
});

// This is an integration test.
test('creating a proposal triggers no backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await postProposal(s.base, { title: 'Not yours yet' });
    await settle();
    assert.equal(snapshots(bk.dir).length, 0,
      'the backup belongs at confirmation, not when the agent writes');
  } finally { await s.stop(); }
});

// This is an integration test.
test('a browser PUT that omits proposals does not trash them', async () => {
  // The load-bearing case: writeBoard soft-deletes any live card missing from a
  // save, and the browser never sends proposals because it cannot see them.
  const s = await startServer();
  try {
    const proposal = await (await postProposal(s.base, { title: 'Survive the sweep' })).json();
    await putCards(s.base, [{ id: 'mine', columnId: 'inbox', title: 'My own card' }]);

    const pending = await getJson(s.base, '/api/proposals');
    assert.deepEqual(pending.cards.map((c) => c.id), [proposal.id],
      'the proposal must survive a whole-board save that omits it');
    const trash = await getJson(s.base, '/api/trash');
    assert.equal(trash.cards.filter((c) => c.id === proposal.id).length, 0,
      'and it must not have been archived');
  } finally { await s.stop(); }
});

// This is an integration test.
test('confirming a proposal puts it on the board and takes one backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    const proposal = await (await postProposal(s.base, { title: 'Now it is mine' })).json();
    const res = await act(s.base, proposal.id, 'confirm');
    assert.equal(res.status, 200);

    const board = await getJson(s.base, '/api/state');
    assert.ok(board.cards.some((c) => c.id === proposal.id), 'confirmed cards join the board');
    const pending = await getJson(s.base, '/api/proposals');
    assert.equal(pending.cards.length, 0, 'and leave the pending list');

    const files = await waitForSnapshots(bk.dir, 1);
    assert.equal(files.length, 1, 'confirmation is what earns the snapshot');
  } finally { await s.stop(); }
});

// This is an integration test.
test('rejecting a proposal trashes it and takes no backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    const proposal = await (await postProposal(s.base, { title: 'No thanks' })).json();
    const res = await act(s.base, proposal.id, 'reject');
    assert.equal(res.status, 200);

    const board = await getJson(s.base, '/api/state');
    assert.equal(board.cards.filter((c) => c.id === proposal.id).length, 0);
    const pending = await getJson(s.base, '/api/proposals');
    assert.equal(pending.cards.length, 0);
    const trash = await getJson(s.base, '/api/trash');
    assert.ok(trash.cards.some((c) => c.id === proposal.id),
      'a rejected proposal is recoverable, not erased');

    await settle();
    assert.equal(snapshots(bk.dir).length, 0, 'rejection is not a new entry');
  } finally { await s.stop(); }
});

// This is an integration test.
test('a rejected proposal restores from Trash as an ordinary card', async () => {
  // Reject clears `pending` as well as setting deleted_at. If it did not, the
  // restored row would still be invisible and the restore would look broken.
  const s = await startServer();
  try {
    const proposal = await (await postProposal(s.base, { title: 'Second thoughts' })).json();
    await act(s.base, proposal.id, 'reject');
    // Restoring is what the app does: re-include the card in a whole-board save.
    await putCards(s.base, [{ id: proposal.id, columnId: 'inbox', title: 'Second thoughts' }]);

    const board = await getJson(s.base, '/api/state');
    assert.ok(board.cards.some((c) => c.id === proposal.id),
      'a restored proposal comes back as a normal board card');
    const pending = await getJson(s.base, '/api/proposals');
    assert.equal(pending.cards.length, 0, 'and is not pending again');
  } finally { await s.stop(); }
});

// This is an integration test.
test('confirm or reject on an unknown or already-live card → 404', async () => {
  const s = await startServer();
  try {
    for (const what of ['confirm', 'reject']) {
      const res = await act(s.base, 'no-such-proposal', what);
      assert.equal(res.status, 404, `${what} on an unknown id`);
    }
    // A live card is not a proposal; acting on one is a bug, not a no-op.
    await putCards(s.base, [{ id: 'live', columnId: 'inbox', title: 'Already mine' }]);
    for (const what of ['confirm', 'reject']) {
      const res = await act(s.base, 'live', what);
      assert.equal(res.status, 404, `${what} on a live card`);
    }
    const board = await getJson(s.base, '/api/state');
    assert.ok(board.cards.some((c) => c.id === 'live'), 'and the live card is untouched');
  } finally { await s.stop(); }
});

// This is an integration test.
test('/api/proposals rejects methods it does not implement', async () => {
  const s = await startServer();
  try {
    const res = await fetch(s.base + '/api/proposals', { method: 'DELETE' });
    assert.equal(res.status, 405);
    assert.deepEqual(await res.json(), { error: 'Method not allowed' });
  } finally { await s.stop(); }
});

// ---- Paired backends: every board gets its own brain ----------------------
// The test board on :3001 (board-3001.db) must proxy the assistant to its own
// brain on :9001, which in turn writes back to :3001 — never to board.db.
// Brains live in the 9000 block; 8001/8002 belong to the Chroma stack that chat
// memory now runs on. Port collisions are covered in tests/ports.test.js.

// This is a configuration invariant.
test('package.json pairs the test board with its own brain', async () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  const board = pkg.scripts['test-board'];
  assert.match(board, /PORT=3001/);
  assert.match(board, /BOARD_DB=databases\/test\/board-3001\.db/);
  // The chat record needs the same pairing: without it the test board's
  // chats would be recorded into the real databases/real/assistant.db.
  assert.match(board, /ASSISTANT_DB=databases\/test\/assistant-3001\.db/);
  // Without this, the :3001 board talks to the default brain, whose writes
  // land in board.db — the bug this pairing exists to prevent.
  assert.match(board, /AGENT_URL=http:\/\/127\.0\.0\.1:9001/);

  const brain = pkg.scripts['test-brain'];
  assert.ok(brain, 'a test-brain script must exist to pair with test-board');
  assert.match(brain, /BOARD_API_URL=http:\/\/127\.0\.0\.1:3001/);
  assert.match(brain, /--port 9001/);
});

// ---- Suggested edits -------------------------------------------------------
// An edit the Assistant wants is a SUGGESTION, not a write. It lives in its own
// table, never touches the cards table, and is shown to the user in the ordinary
// card editor. The user's own save is what applies it — the same whole-board PUT
// a hand edit goes through — so there is no apply path the agent can reach, and
// nothing here needs a confirmation route or a backup of its own.

const postEdit = (base, body) =>
  fetch(base + '/api/edits', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

// This is an integration test.
test('POST /api/edits stores a suggestion and changes no card', async () => {
  const s = await startServer();
  try {
    await putCards(s.base, [{ id: 'a', title: 'Renew the passport', columnId: 'inbox' }]);

    const res = await postEdit(s.base, {
      cardId: 'a', fields: { title: 'Renew it before June', columnId: 'in-progress' },
    });
    assert.equal(res.status, 200);
    const stored = await res.json();
    assert.ok(stored.id, 'the server assigns the suggestion an id');

    // The card is exactly as the user left it. This is the whole point.
    const board = await getJson(s.base, '/api/state');
    const card = board.cards.find((c) => c.id === 'a');
    assert.equal(card.title, 'Renew the passport');
    assert.equal(card.columnId, 'inbox');

    const pending = await getJson(s.base, '/api/edits');
    assert.equal(pending.edits.length, 1);
    assert.equal(pending.edits[0].cardId, 'a');
    assert.deepEqual(pending.edits[0].fields,
      { title: 'Renew it before June', columnId: 'in-progress' });
  } finally { await s.stop(); }
});

// This is an integration test.
test('a suggestion for a card that does not exist is a bad request', async () => {
  const s = await startServer();
  try {
    const res = await postEdit(s.base, { cardId: 'ghost', fields: { title: 'x' } });
    assert.equal(res.status, 400);
    assert.equal((await getJson(s.base, '/api/edits')).edits.length, 0);

    // And a suggestion that changes nothing is equally useless.
    const empty = await postEdit(s.base, { cardId: 'ghost', fields: {} });
    assert.equal(empty.status, 400);
  } finally { await s.stop(); }
});

// This is an integration test.
test('DELETE /api/edits/:id discards a suggestion and leaves the card alone', async () => {
  const s = await startServer();
  try {
    await putCards(s.base, [{ id: 'a', title: 'Renew the passport' }]);
    const stored = await (await postEdit(s.base, {
      cardId: 'a', fields: { title: 'Renew it' } })).json();

    const gone = await fetch(`${s.base}/api/edits/${encodeURIComponent(stored.id)}`,
      { method: 'DELETE' });
    assert.equal(gone.status, 200);
    assert.equal((await getJson(s.base, '/api/edits')).edits.length, 0);

    // Discarding a suggestion is not a card operation: DELETE /api/cards/:id
    // stays the board's only hard delete, and this card is untouched.
    const board = await getJson(s.base, '/api/state');
    assert.equal(board.cards.find((c) => c.id === 'a').title, 'Renew the passport');

    assert.equal((await fetch(`${s.base}/api/edits/nope`, { method: 'DELETE' })).status,
      404, 'discarding twice is a 404, not a silent success');
  } finally { await s.stop(); }
});

// This is an integration test.
test('a board save neither applies nor clears a suggestion', async () => {
  const s = await startServer();
  try {
    await putCards(s.base, [{ id: 'a', title: 'Renew the passport' }]);
    await postEdit(s.base, { cardId: 'a', fields: { title: 'Renew it' } });

    // The browser saves the board as it always does, knowing nothing about
    // suggestions. Neither side may leak into the other: a save that silently
    // applied one would be the agent writing after all, and a save that dropped
    // one would lose the suggestion the moment the user typed anywhere.
    await putCards(s.base, [{ id: 'a', title: 'Renew the passport', notes: 'April' }]);

    const board = await getJson(s.base, '/api/state');
    assert.equal(board.cards.find((c) => c.id === 'a').title, 'Renew the passport');
    assert.equal((await getJson(s.base, '/api/edits')).edits.length, 1);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a suggestion is discarded with the card it points at', async () => {
  const s = await startServer();
  try {
    await putCards(s.base, [{ id: 'a', title: 'Renew the passport' }]);
    await postEdit(s.base, { cardId: 'a', fields: { title: 'Renew it' } });

    // Omitting the card soft-deletes it into Trash. A suggestion pointing at a
    // trashed card would surface in the Assistant with nothing to apply to.
    await putCards(s.base, []);
    assert.equal((await getJson(s.base, '/api/edits')).edits.length, 0);
  } finally { await s.stop(); }
});
