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

// This is an integration test. The 2026-08-22 incident: a second machine loaded
// this board with a days-old copy, saved it, and the sweep archived the 24 cards
// that copy had never heard of. A save now says which version of the board it was
// written against, and one that names a version the server has moved past is
// applied additively — it can add, it can update what it is not behind on, and it
// cannot delete.
test('a save that names a rev it has not seen adds and never deletes', async () => {
  const s = await startServer();
  try {
    const put2 = (cards, body) => fetch(s.base + '/api/state', {
      method: 'PUT', headers: json, body: JSON.stringify({ version: 1, cards, ...body }),
    });
    const older = { id: 'a', columnId: 'inbox', title: 'A', updatedAt: 1000 };
    const newer = { id: 'b', columnId: 'inbox', title: 'B', updatedAt: 2000 };
    await put2([older, newer], {});

    // The rev names this exact board, and it changes when the board does.
    const seen = await get(s.base, '/api/state');
    assert.equal(typeof seen.rev, 'string');
    assert.ok(seen.rev.length > 0);

    // A save carrying the current rev is the ordinary case and still sweeps.
    const fresh = await (await put2([older], { rev: seen.rev })).json();
    assert.deepEqual(fresh.cards.map((c) => c.id), ['a']);
    assert.notEqual(fresh.rev, seen.rev);
    // Restore it, so what follows is about staleness and not about the trash.
    const restored = await (await put2([older, newer], { rev: fresh.rev })).json();

    // Now the stale save: it is based on `seen`, so it has never heard of card
    // 'c', still carries an old copy of 'b', and omits 'a' — which is the thing
    // it must not be believed about.
    await put2([older, newer, { id: 'c', columnId: 'inbox', title: 'C' }], { rev: restored.rev });
    const stale = await (await put2(
      [{ ...newer, title: 'B, reverted', updatedAt: 1500 },
       { id: 'd', columnId: 'inbox', title: 'D from the other machine' }],
      { rev: seen.rev, categories: [{ id: 'boats', label: 'Boats', h: 200 }] },
    )).json();

    assert.equal(stale.stale, true);
    assert.notEqual(stale.rev, seen.rev);
    const ids = stale.cards.map((c) => c.id);
    assert.deepEqual(ids.sort(), ['a', 'b', 'c', 'd']); // nothing archived, the new card landed
    assert.equal((await get(s.base, '/api/trash')).cards.length, 0);
    // Not behind on 'b' is not the same as authoritative about it: the stored
    // copy is newer, so the revert is refused rather than applied.
    assert.equal(stale.cards.find((c) => c.id === 'b').title, 'B');
    // The registry is additive too. Replacing it would drop every category the
    // stale client never saw, and cleanCard would then blank that field on
    // every card holding one.
    const cats = stale.categories.map((c) => c.id);
    assert.ok(cats.includes('boats'), 'a category the board lacked is added');
    assert.ok(cats.includes('work'), 'the categories it never saw survive');

    // And the contract for everything that predates rev — curl, the evals, the
    // brain, every test above this one: say nothing, get the old behaviour.
    await put2([older], {});
    assert.deepEqual((await get(s.base, '/api/state')).cards.map((c) => c.id), ['a']);
    assert.equal((await get(s.base, '/api/trash')).cards.length, 3);
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

// This is an integration test. It encodes the reported bug: categories from one
// board appeared on another, and a category deleted on the second board came
// back. Desired behaviour — each board owns its registry; a new board is seeded
// with the default life areas; no save on one board can touch another's registry.
test('the category registry is per board', async () => {
  const s = await startServer();
  try {
    const DEFAULT_IDS = ['work', 'love', 'family', 'health', 'mind', 'music', 'travel', 'home', 'money', 'dream'];
    const putState = (body, board) => fetch(
      s.base + '/api/state' + (board ? `?board=${board}` : ''),
      { method: 'PUT', headers: json, body: JSON.stringify({ version: 1, ...body }) });

    // Give the default board a custom category beside the defaults.
    await putState({ cards: [], categories: [
      ...DEFAULT_IDS.map((id, i) => ({ id, label: id, h: i * 10 })),
      { id: 'garden', label: 'Garden', h: 120 },
    ] });

    // A new board starts with the default life areas — not the first board's
    // registry, so 'garden' must not leak onto it.
    const second = await newBoard(s.base, 'Second');
    const fresh = await get(s.base, `/api/state?board=${second.id}`);
    assert.deepEqual(fresh.categories.map((c) => c.id), DEFAULT_IDS);

    // Deleting categories on the second board touches only the second board.
    await putState({ cards: [], categories: [{ id: 'work', label: 'Work', h: 255 }] }, second.id);
    const firstAfter = await get(s.base, '/api/state');
    assert.ok(firstAfter.categories.some((c) => c.id === 'garden'),
      'the first board keeps its own registry');
    assert.equal(firstAfter.categories.length, DEFAULT_IDS.length + 1);

    // The reported resurrection: a later save on the first board (its registry
    // in full, as the browser always sends it) must not bring the second
    // board's deleted categories back.
    await putState({ cards: [], categories: firstAfter.categories });
    const secondAfter = await get(s.base, `/api/state?board=${second.id}`);
    assert.deepEqual(secondAfter.categories.map((c) => c.id), ['work']);

    // Deleting the last category persists too: an empty registry is a real
    // state of one board, not a malformed payload to be skipped.
    await putState({ cards: [], categories: [] }, second.id);
    assert.deepEqual((await get(s.base, `/api/state?board=${second.id}`)).categories, []);
    assert.equal((await get(s.base, '/api/state')).categories.length, DEFAULT_IDS.length + 1);
  } finally { await s.stop(); }
});

// This is an integration test. A board.db whose categories table predates
// per-board registries must hand its rows — custom categories included — to the
// default board, exactly as the migration at the top of this file does for cards.
test('a categories table with no board column migrates onto the default board', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-legacy-cats-'));
  const dbPath = join(dir, 'board.db');
  const legacy = new DatabaseSync(dbPath);
  legacy.exec(`
    CREATE TABLE categories (
      id TEXT PRIMARY KEY, label TEXT NOT NULL, h INTEGER NOT NULL,
      position INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO categories (id, label, h, position) VALUES
      ('work', 'Work', 255, 0), ('garden', 'Garden', 120, 1);
  `);
  legacy.close();

  const s = await startServer({ env: { BOARD_DB: dbPath } });
  try {
    const state = await get(s.base, '/api/state');
    assert.deepEqual(state.cards, []);
    assert.deepEqual(state.categories.map((c) => c.id), ['work', 'garden']);
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
