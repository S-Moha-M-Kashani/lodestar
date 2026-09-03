// tests/caching.test.js
//
// The conservative read-caching slice: the ETag allowlist, and the SQLite index
// the query-plan evidence earned. Two properties are worth a test each here and
// they pull in opposite directions — a match must cost less than the payload,
// and nothing mutable may ever answer a conditional read with a 304.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer } from './helpers/server-harness.mjs';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const json = { 'content-type': 'application/json' };

const put = (base, cards, extra = {}) => fetch(base + '/api/state', {
  method: 'PUT', headers: json,
  body: JSON.stringify({ version: 1, cards, ...extra }),
});

/** A conditional GET: what a browser sends on its second visit. */
const revalidate = (base, path, tag) => fetch(base + path, {
  headers: { 'if-none-match': tag },
});

// This is an integration test. The first requirement: an eligible read that has
// not changed answers with a validator match and no second copy of the payload.
// Both allowlist entries are exercised — the board, whose validator is the rev
// it already computes, and a static module, whose validator is a hash of its
// bytes — because they are the same requirement reached by two code paths.
test('an unchanged eligible read answers 304 with no payload', async () => {
  const s = await startServer();
  try {
    await put(s.base, [{ id: 'a1', columnId: 'inbox', title: 'A card' }]);

    const first = await fetch(s.base + '/api/state');
    const tag = first.headers.get('etag');
    const body = await first.json();
    assert.equal(first.status, 200);
    assert.ok(tag, 'GET /api/state must offer a validator');
    // The tag is the rev itself, quoted — one value, not two names for the
    // board's version that could drift apart.
    assert.equal(tag, `"${body.rev}"`);
    // Without this the ETag makes staleness MORE likely, not less: a cache may
    // apply its own heuristic freshness to a 200 carrying no directives.
    assert.equal(first.headers.get('cache-control'), 'no-cache');

    const again = await revalidate(s.base, '/api/state', tag);
    assert.equal(again.status, 304);
    assert.equal(await again.text(), '', 'a 304 must not carry the payload');
    assert.equal(again.headers.get('content-type'), null);
    assert.equal(again.headers.get('etag'), tag, 'the client goes on using the tag');
    assert.equal(again.headers.get('cache-control'), 'no-cache');

    // `*` means "any version, if you hold one" (RFC 9110), and a tag from
    // another board's version must not match.
    assert.equal((await revalidate(s.base, '/api/state', '*')).status, 304);
    assert.equal((await revalidate(s.base, '/api/state', '"0000000000000000"')).status, 200);

    // The second allowlist entry: code shipped with the server.
    const mod = await fetch(s.base + '/js/main.js');
    const modTag = mod.headers.get('etag');
    assert.equal(mod.status, 200);
    assert.ok(modTag, 'a static module must offer a validator');
    assert.ok((await mod.text()).length > 0);
    const modAgain = await revalidate(s.base, '/js/main.js', modTag);
    assert.equal(modAgain.status, 304);
    assert.equal(await modAgain.text(), '');
  } finally {
    await s.stop();
  }
});

// This is an integration test. The one that matters. A validator that survives
// a change to the thing it names is how a cache serves somebody last week's
// cards, so every kind of change the client can see is checked against it —
// including the two that a MAX(updated_at) or a row count would miss, which is
// exactly why the validator is a hash of the bytes.
test('any change a client can see makes the board validator stop matching', async () => {
  const s = await startServer();
  try {
    await put(s.base, [{ id: 'a1', columnId: 'inbox', title: 'A card' }]);
    const before = await fetch(s.base + '/api/state');
    const stale = before.headers.get('etag');

    // An edit.
    await put(s.base, [{ id: 'a1', columnId: 'inbox', title: 'A card, edited' }]);
    const after = await revalidate(s.base, '/api/state', stale);
    assert.equal(after.status, 200, 'an edited board must not answer 304');
    assert.notEqual(after.headers.get('etag'), stale);
    const fresh = await after.json();
    assert.equal(fresh.cards[0].title, 'A card, edited');

    // A CATEGORY RENAME. No card row changes and no count changes, so a SQL
    // aggregate would call this board unchanged; the label is on screen.
    const renamed = await put(
      s.base,
      [{ id: 'a1', columnId: 'inbox', title: 'A card, edited' }],
      { categories: [{ id: 'work', label: 'Werk', h: 250 }] });
    assert.equal(renamed.status, 200);
    const afterRename = await revalidate(
      s.base, '/api/state', after.headers.get('etag'));
    assert.equal(afterRename.status, 200, 'a renamed category must not answer 304');

    // A DELETE. The card leaves the board and the validator has to follow it,
    // or the one client that cached it keeps seeing a card that is in the Trash.
    await put(s.base, []);
    const afterDelete = await revalidate(
      s.base, '/api/state', afterRename.headers.get('etag'));
    assert.equal(afterDelete.status, 200);
    assert.deepEqual((await afterDelete.json()).cards, []);
  } finally {
    await s.stop();
  }
});

// This is an integration test. The spec's second requirement from the other
// side: the reads that are NOT on the allowlist must stay uncacheable, and a
// conditional request against one must be answered with the current record.
// The chat record is where being subtly stale would be least visible, so a
// soft-delete is put through the same conditional read.
test('mutable chat and board reads offer no validator and stay fresh', async () => {
  const s = await startServer();
  try {
    await fetch(s.base + '/api/chat/messages', {
      method: 'POST', headers: json,
      body: JSON.stringify({
        sessionId: 'sess-1',
        messages: [{ role: 'user', content: 'first' },
                   { role: 'assistant', content: 'second' }],
      }),
    });

    const uncached = ['/api/chat/messages', '/api/chat/sessions', '/api/chat/trash',
                      '/api/trash', '/api/proposals', '/api/edits', '/api/boards',
                      '/api/boards/trash', '/api/trace/status'];
    for (const path of uncached) {
      const res = await fetch(s.base + path);
      assert.equal(res.status, 200, path);
      assert.equal(res.headers.get('etag'), null, `${path} must offer no validator`);
    }

    // A tag it never issued, and `*`, which would match any validator at all:
    // neither may talk this route into a 304.
    const read = await fetch(s.base + '/api/chat/messages');
    const messages = (await read.json()).messages;
    assert.deepEqual(messages.map((m) => m.content), ['first', 'second']);
    assert.equal((await revalidate(s.base, '/api/chat/messages', '*')).status, 200);

    // Soft-delete one turn and read again with the widest possible validator.
    const del = await fetch(`${s.base}/api/chat/messages/${messages[0].id}`,
                            { method: 'DELETE' });
    assert.equal(del.status, 200);
    const afterDelete = await revalidate(s.base, '/api/chat/messages', '*');
    assert.equal(afterDelete.status, 200);
    assert.deepEqual((await afterDelete.json()).messages.map((m) => m.content),
                     ['second'], 'a deleted turn must be gone from the very next read');
  } finally {
    await s.stop();
  }
});

// This is an integration test. Task 5.1: the index the evidence identified
// exists after boot, and the two queries it was measured against actually use
// it. The plan is asserted rather than a timing, because a timing on an empty
// temp database measures nothing — the numbers are in the comment beside the
// CREATE INDEX, taken on 2000 cards and 3000 messages.
test('the messages_session index exists and the chat reads use it', async () => {
  const s = await startServer();
  try {
    // Boot has to have happened before the file is inspected, and it has:
    // startServer waits for the listening line.
    const chat = new DatabaseSync(s.assistantDbPath, { readOnly: true });
    try {
      const indexes = chat.prepare(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'messages'")
        .all();
      const found = indexes.find((i) => i.name === 'messages_session');
      assert.ok(found, 'messages_session must exist after boot');
      // The column order is the whole point: it answers the ORDER BY as well as
      // the predicate, which is what removes the temp B-tree.
      assert.match(found.sql, /\(\s*session_id\s*,\s*created_at\s*,\s*id\s*\)/);

      const plan = (sql) => chat.prepare('EXPLAIN QUERY PLAN ' + sql)
        .all().map((r) => r.detail).join(' | ');

      // One chat's transcript — readChatSession.
      const transcript = plan(`
        SELECT m.* FROM messages m LEFT JOIN sessions s ON s.id = m.session_id
         WHERE m.deleted_at IS NULL AND s.deleted_at IS NULL AND m.session_id = ?
         ORDER BY m.created_at, m.id`);
      assert.match(transcript, /messages_session/);
      assert.doesNotMatch(transcript, /TEMP B-TREE/,
                          'the index must answer the ORDER BY, not just the predicate');

      // The history panel's list — readChatSessions. Its per-chat COUNT(*) was
      // a full scan of messages once per session; this is the 4.9x.
      const sessions = plan(`
        SELECT s.id, (SELECT COUNT(*) FROM messages m
                       WHERE m.session_id = s.id AND m.deleted_at IS NULL) AS n
          FROM sessions s WHERE s.deleted_at IS NULL AND s.board_id = ?
          ORDER BY s.updated_at DESC, s.created_at DESC`);
      assert.match(sessions, /messages_session/);
    } finally {
      chat.close();
    }
  } finally {
    await s.stop();
  }
});

// This is an integration test. An index is not a migration, but it is DDL run
// at boot against a file this server did not create, and it names a column that
// three boot-time ALTERs above it add. So: an assistant.db from before sessions
// existed must still open, keep its rows, and come out of boot indexed.
test('an assistant.db written before sessions existed still opens', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-oldchat-'));
  const oldDb = join(dir, 'assistant.db');
  const seed = new DatabaseSync(oldDb);
  // The original 2026-07 shape: no session_id, no steps, no usage, no cost, and
  // no sessions table at all.
  seed.exec(`
    CREATE TABLE messages (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      role       TEXT    NOT NULL,
      content    TEXT    NOT NULL,
      created_at INTEGER NOT NULL,
      deleted_at INTEGER
    );
    INSERT INTO messages (role, content, created_at) VALUES ('user', 'an old turn', 1);
  `);
  seed.close();

  const s = await startServer({ env: { ASSISTANT_DB: oldDb } });
  try {
    // Booting at all is most of the claim; startServer throws if it did not.
    assert.equal((await fetch(s.base + '/api/health')).status, 200);
    const chat = new DatabaseSync(oldDb, { readOnly: true });
    try {
      assert.equal(chat.prepare('SELECT content FROM messages WHERE id = 1').get().content,
                   'an old turn', 'the pre-existing row must survive boot untouched');
      assert.ok(chat.prepare(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'messages_session'")
        .get(), 'the index must be created on an upgraded database too');
    } finally {
      chat.close();
    }
  } finally {
    await s.stop();
  }
});
