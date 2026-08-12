// tests/boards.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const json = { 'content-type': 'application/json' };
const put = (base, cards, board) => fetch(
  base + '/api/state' + (board ? `?board=${board}` : ''),
  { method: 'PUT', headers: json, body: JSON.stringify({ version: 1, cards }) });
const get = async (base, path) => (await fetch(base + path)).json();
const newBoard = async (base, name) => (await (await fetch(base + '/api/boards', {
  method: 'POST', headers: json, body: JSON.stringify({ name }),
})).json()).board;

// This is an integration test. The one that matters most: writeBoard soft-deletes
// every live card it was not sent, so an unscoped sweep would let a keystroke on
// one board archive the whole of another.
test('a whole-board PUT touches only the board it names', async () => {
  const s = await startServer();
  try {
    const home = await newBoard(s.base, 'Home');
    await put(s.base, [{ id: 'w1', columnId: 'inbox', title: 'Work card' }]);
    await put(s.base, [{ id: 'h1', columnId: 'inbox', title: 'Home card' }], home.id);

    // Saving the default board again, without Home's card, must not archive it.
    await put(s.base, [{ id: 'w1', columnId: 'inbox', title: 'Work card, edited' }]);

    const first = await get(s.base, '/api/state');
    const second = await get(s.base, `/api/state?board=${home.id}`);
    assert.deepEqual(first.cards.map((c) => c.id), ['w1']);
    assert.equal(first.cards[0].title, 'Work card, edited');
    assert.deepEqual(second.cards.map((c) => c.id), ['h1']);

    // The trash is scoped the same way, or a restore would cross boards.
    const trash = await get(s.base, `/api/trash?board=${home.id}`);
    assert.deepEqual(trash.cards, []);
    // An unknown board is refused rather than silently answered with another's cards.
    const bogus = await fetch(s.base + '/api/state?board=nope');
    assert.equal(bogus.status, 400);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a board is created, renamed, soft-deleted and restored whole', async () => {
  const s = await startServer();
  try {
    const board = await newBoard(s.base, 'Music');
    await put(s.base, [{ id: 'm1', columnId: 'inbox', title: 'Learn a scale' }], board.id);

    const renamed = await fetch(s.base + `/api/boards/${board.id}`, {
      method: 'PATCH', headers: json, body: JSON.stringify({ name: 'Practice' }),
    });
    assert.equal(renamed.status, 200);
    assert.equal((await renamed.json()).board.name, 'Practice');

    const listed = await get(s.base, '/api/boards');
    assert.deepEqual(listed.boards.map((b) => b.name), ['Lodestar', 'Practice']);
    assert.equal(listed.boards.find((b) => b.id === board.id).cardCount, 1);
    assert.equal(listed.defaultId, 'main');

    const gone = await fetch(s.base + `/api/boards/${board.id}`, { method: 'DELETE' });
    assert.equal(gone.status, 200);
    assert.deepEqual((await get(s.base, '/api/boards')).boards.map((b) => b.id), ['main']);
    assert.deepEqual((await get(s.base, '/api/boards/trash')).boards.map((b) => b.id), [board.id]);
    // Its cards are still there — a deleted board is hidden, not emptied.
    assert.equal((await fetch(s.base + `/api/state?board=${board.id}`)).status, 400);

    const back = await fetch(s.base + `/api/boards/trash/${board.id}/restore`, { method: 'POST' });
    assert.equal(back.status, 200);
    const state = await get(s.base, `/api/state?board=${board.id}`);
    assert.deepEqual(state.cards.map((c) => c.title), ['Learn a scale']);

    // The last live board has nowhere to send you, so it refuses to go.
    assert.equal((await fetch(s.base + `/api/boards/${board.id}`, { method: 'DELETE' })).status, 200);
    assert.equal((await fetch(s.base + '/api/boards/main', { method: 'DELETE' })).status, 409);
  } finally { await s.stop(); }
});

// This is an integration test. Purging is the board's one hard delete, and it is
// reachable only for a board already in the trash — no single call both hides a
// board and destroys it.
test('purging a deleted board erases its cards and chats, and only its own', async () => {
  const s = await startServer();
  try {
    const board = await newBoard(s.base, 'Trip');
    await put(s.base, [{ id: 'keep', columnId: 'inbox', title: 'Stays' }]);
    await put(s.base, [{ id: 't1', columnId: 'inbox', title: 'Goes' }], board.id);
    const chat = (sessionId, boardId) => fetch(s.base + '/api/chat/messages', {
      method: 'POST', headers: json,
      body: JSON.stringify({ sessionId, boardId, messages: [{ role: 'user', content: 'hi' }] }),
    });
    await chat('s-keep', 'main');
    await chat('s-goes', board.id);

    // A live board cannot be purged.
    assert.equal((await fetch(s.base + `/api/boards/trash/${board.id}`, { method: 'DELETE' })).status, 404);

    await fetch(s.base + `/api/boards/${board.id}`, { method: 'DELETE' });
    const purged = await fetch(s.base + `/api/boards/trash/${board.id}`, { method: 'DELETE' });
    assert.equal(purged.status, 200);
    assert.deepEqual(await purged.json(), { ok: true, cards: 1, sessions: 1 });

    const cards = new DatabaseSync(s.dbPath).prepare('SELECT id FROM cards').all();
    assert.deepEqual(cards.map((c) => c.id), ['keep']);
    const sessions = new DatabaseSync(s.assistantDbPath).prepare('SELECT id FROM sessions').all();
    assert.deepEqual(sessions.map((r) => r.id), ['s-keep']);
    assert.deepEqual((await get(s.base, '/api/boards/trash')).boards, []);
  } finally { await s.stop(); }
});

// This is an integration test.
test('chats belong to a board and a board only ever sees its own', async () => {
  const s = await startServer();
  try {
    const other = await newBoard(s.base, 'Other');
    const send = (sessionId, boardId) => fetch(s.base + '/api/chat/messages', {
      method: 'POST', headers: json,
      body: JSON.stringify({ sessionId, boardId, messages: [{ role: 'user', content: sessionId }] }),
    });
    await send('here', 'main');
    await send('there', other.id);

    assert.deepEqual((await get(s.base, '/api/chat/sessions')).sessions.map((x) => x.id), ['here']);
    assert.deepEqual(
      (await get(s.base, `/api/chat/sessions?board=${other.id}`)).sessions.map((x) => x.id), ['there']);
    assert.deepEqual(
      (await get(s.base, '/api/chat/messages')).messages.map((m) => m.content), ['here']);

    // A batch naming no board lands on the default one, so every caller written
    // before boards existed — a curl, an eval, the brain's own tests — still records.
    await fetch(s.base + '/api/chat/messages', {
      method: 'POST', headers: json,
      body: JSON.stringify({ messages: [{ role: 'user', content: 'unsessioned' }] }),
    });
    assert.deepEqual((await get(s.base, '/api/chat/sessions')).sessions.map((x) => x.id).sort(),
      ['adhoc', 'here']);
  } finally { await s.stop(); }
});

// This is an integration test. A board.db written before this feature must open
// on a board that looks exactly like the one it had.
test('a database with no boards table migrates onto the default board', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-legacy-'));
  const dbPath = join(dir, 'board.db');
  const legacy = new DatabaseSync(dbPath);
  legacy.exec(`
    CREATE TABLE cards (
      id TEXT PRIMARY KEY, column_id TEXT NOT NULL, title TEXT NOT NULL,
      notes TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'medium',
      num INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      position INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO cards (id, column_id, title, created_at, updated_at)
    VALUES ('old', 'inbox', 'Written before boards', 1, 1);
  `);
  legacy.close();

  const s = await startServer({ env: { BOARD_DB: dbPath } });
  try {
    const state = await get(s.base, '/api/state');
    assert.deepEqual(state.cards.map((c) => c.id), ['old']);
    const boards = await get(s.base, '/api/boards');
    assert.deepEqual(boards.boards.map((b) => b.id), ['main']);
    assert.equal(boards.boards[0].cardCount, 1);
  } finally { await s.stop(); }
});
