// tests/server.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';
import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, readFileSync, existsSync, readdirSync } from 'node:fs';
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
    assert.equal(res.status, 200);
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

// ---- RAG lab proxy -------------------------------------------------------
// The lab is a second upstream, separate from the brain: it runs only when a
// developer starts it, and it must never be reached at AGENT_URL — a request
// for lab options landing on the brain would 404 in a way that reads like the
// lab is broken.

test('/api/raglab/* is proxied to the lab, not to the brain', async () => {
  const seen = [];
  const lab = await startStubBrain((req) => seen.push(req), {
    status: 200, body: { chunkers: ['fixed'] },
  });
  const brain = await startStubBrain((req) => seen.push({ ...req, wrong: true }));
  const s = await startServer({ env: { AGENT_URL: brain.url, RAGLAB_URL: lab.url } });
  try {
    const res = await fetch(s.base + '/api/raglab/options');
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { chunkers: ['fixed'] });
    assert.equal(seen.length, 1, 'exactly one upstream was called');
    assert.equal(seen[0].wrong, undefined, 'the brain must not receive lab traffic');
    // The lab serves /api/options; the browser asks for /api/raglab/options.
    assert.equal(seen[0].path, '/api/options');
  } finally { await s.stop(); await lab.stop(); await brain.stop(); }
});

test('/api/raglab/* forwards POST bodies and query strings', async () => {
  const seen = [];
  const lab = await startStubBrain((req) => seen.push(req), {
    status: 200, body: { job_id: 'abc' },
  });
  const s = await startServer({ env: { RAGLAB_URL: lab.url } });
  try {
    const res = await fetch(s.base + '/api/raglab/run?limit=5', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ index: { chunker: 'session' } }),
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { job_id: 'abc' });
    assert.equal(seen[0].path, '/api/run?limit=5');
    assert.deepEqual(JSON.parse(seen[0].body), { index: { chunker: 'session' } });
  } finally { await s.stop(); await lab.stop(); }
});

test('the lab being down is reported as the lab, not as the assistant', async () => {
  // Distinct wording is the whole point: "assistant unavailable" would send the
  // user restarting a brain that is running perfectly well.
  const s = await startServer({ env: { RAGLAB_URL: 'http://127.0.0.1:59998' } });
  try {
    const res = await fetch(s.base + '/api/raglab/options');
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: 'RAG lab unavailable' });
  } finally { await s.stop(); }
});

test('a lab error status passes through instead of becoming a 503', async () => {
  const lab = await startStubBrain(() => {}, {
    status: 400, body: { detail: 'unknown chunker: nope' },
  });
  const s = await startServer({ env: { RAGLAB_URL: lab.url } });
  try {
    const res = await fetch(s.base + '/api/raglab/query', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ question: 'x' }),
    });
    assert.equal(res.status, 400);
    assert.deepEqual(await res.json(), { detail: 'unknown chunker: nope' });
  } finally { await s.stop(); await lab.stop(); }
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
    assert.equal(card.deadline, '');       // added column default
  } finally { await s.stop(); }
});

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

const snapshots = (dir) =>
  (existsSync(dir) ? readdirSync(dir) : []).filter((f) => f.startsWith('board-') && f.endsWith('.db'));

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

const putCards = (base, cards) =>
  fetch(base + '/api/state', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ version: 1, cards }),
  });

test('PUT with a never-before-seen card triggers one backup', async () => {
  const bk = backupSandbox();
  const s = await startServer({ env: bk.env });
  try {
    await putCards(s.base, [{ id: 'n1', columnId: 'inbox', title: 'A new thought' }]);
    const files = await waitForSnapshots(bk.dir, 1);
    assert.equal(files.length, 1, 'a new card must produce exactly one snapshot');
    // The snapshot is taken after the commit, so it contains the new card.
    const snap = new DatabaseSync(join(bk.dir, files[0]), { readOnly: true });
    const row = snap.prepare('SELECT title FROM cards WHERE id = ?').get('n1');
    assert.equal(row.title, 'A new thought');
  } finally { await s.stop(); }
});

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
// stays the only hard delete in the system.

const postProposal = (base, card) =>
  fetch(base + '/api/proposals', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(card),
  });

const getJson = async (base, path) => (await fetch(base + path)).json();
const act = (base, id, what) =>
  fetch(`${base}/api/proposals/${encodeURIComponent(id)}/${what}`, { method: 'POST' });

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

test('a proposal with a blank title is rejected as a bad request', async () => {
  const s = await startServer();
  try {
    const res = await postProposal(s.base, { title: '   ' });
    assert.equal(res.status, 400);
    const pending = await getJson(s.base, '/api/proposals');
    assert.equal(pending.cards.length, 0);
  } finally { await s.stop(); }
});

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

test('package.json pairs the test board with its own brain', async () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  const board = pkg.scripts['test-board'];
  assert.match(board, /PORT=3001/);
  assert.match(board, /BOARD_DB=board-3001\.db/);
  // Without this, the :3001 board talks to the default brain, whose writes
  // land in board.db — the bug this pairing exists to prevent.
  assert.match(board, /AGENT_URL=http:\/\/127\.0\.0\.1:9001/);

  const brain = pkg.scripts['test-brain'];
  assert.ok(brain, 'a test-brain script must exist to pair with test-board');
  assert.match(brain, /BOARD_API_URL=http:\/\/127\.0\.0\.1:3001/);
  assert.match(brain, /--port 9001/);
});
