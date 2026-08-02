// tests/chat.test.js — Stage 2 of Session 7: assistant.db, the chat record.
//
// Contract under test: a `messages` table (id, role, content, created_at,
// deleted_at) in databases/assistant.db — ASSISTANT_DB overrides, exactly as
// BOARD_DB does for the board — exposed as:
//
//   GET  /api/chat/messages  -> { messages: [{id, role, content, createdAt}] }
//        live rows only, ordered by createdAt then id, so an imported older
//        transcript reads in chronological order.
//   POST /api/chat/messages  <- { messages: [{role, content, createdAt?}] }
//        role is user|assistant, content a non-empty string; createdAt is
//        optional (imports keep their own timestamps, live turns are stamped
//        by the server). An invalid row refuses the whole batch — the record
//        never takes half an import.
//
// There are deliberately NO delete routes: the record cannot be destroyed
// through the API at all in this stage — stronger than the board's promise,
// until a UI needs a trash for chat.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startServer } from './helpers/server-harness.mjs';

async function post(base, body) {
  return fetch(base + '/api/chat/messages', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

const list = async (base) =>
  (await (await fetch(base + '/api/chat/messages')).json()).messages;

// This is an integration test.
test('POST appends to the record; GET returns it in chronological order', async () => {
  const s = await startServer();
  try {
    // A live turn: both sides recorded, server stamps createdAt.
    const res = await post(s.base, { messages: [
      { role: 'user', content: 'remember the wifi password is hunter2' },
      { role: 'assistant', content: 'Noted — the wifi password is hunter2.' },
    ] });
    assert.equal(res.status, 200);
    const { messages: saved } = await res.json();
    assert.equal(saved.length, 2);
    assert.ok(saved[0].id && saved[1].id && saved[0].id !== saved[1].id);
    assert.ok(saved[0].createdAt > 0, 'the server stamps createdAt when absent');

    // An import: an older message keeps its own timestamp and sorts first.
    const old = await post(s.base, { messages: [
      { role: 'user', content: 'from the old transcript', createdAt: 1000 },
    ] });
    assert.equal(old.status, 200);
    const all = await list(s.base);
    assert.deepEqual(all.map((m) => m.content), [
      'from the old transcript',
      'remember the wifi password is hunter2',
      'Noted — the wifi password is hunter2.',
    ]);
    assert.equal(all[0].createdAt, 1000, 'an imported createdAt is preserved');
  } finally { await s.stop(); }
});

// This is an integration test.
test('the record is separate from the board and survives a restart', async () => {
  // Fixed paths so a second server can reopen the same databases.
  const dir = mkdtempSync(join(tmpdir(), 'chat-'));
  const env = { BOARD_DB: join(dir, 'board.db'), ASSISTANT_DB: join(dir, 'assistant.db') };

  const s1 = await startServer({ env });
  try {
    await post(s1.base, { messages: [
      { role: 'user', content: 'does this survive?' },
      { role: 'assistant', content: 'It must.' },
    ] });
    assert.ok(existsSync(env.ASSISTANT_DB), 'chat lives in its own file');

    // The whole-board save — the API that soft-deletes omitted cards — must
    // have no reach into the chat record. This is why there are two files.
    const put = await fetch(s1.base + '/api/state', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ version: 1, cards: [] }),
    });
    assert.equal(put.status, 200);
    assert.equal((await list(s1.base)).length, 2, 'a board save cannot touch chat');
  } finally { await s1.stop(); }

  const s2 = await startServer({ env });
  try {
    const all = await list(s2.base);
    assert.deepEqual(all.map((m) => m.content), ['does this survive?', 'It must.']);
  } finally { await s2.stop(); }
});

// This is an integration test.
test('an invalid message refuses the whole batch and inserts nothing', async () => {
  const s = await startServer();
  try {
    for (const bad of [
      { messages: [{ role: 'wizard', content: 'no such role' }] },
      { messages: [{ role: 'user', content: '' }] },
      { messages: [{ role: 'user', content: 'good' }, { role: 'user', content: '' }] },
      { messages: 'not-an-array' },
      {},
    ]) {
      const res = await post(s.base, bad);
      assert.equal(res.status, 400, `must refuse: ${JSON.stringify(bad)}`);
    }
    assert.deepEqual(await list(s.base), [],
      'a refused batch inserts nothing — not even its valid rows');
  } finally { await s.stop(); }
});
