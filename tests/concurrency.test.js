// tests/concurrency.test.js — one database file, several server processes.
//
// Until 2026-08-31 the composed container wrote its own private copy of
// board.db, so two servers could never meet over one file. They share it now,
// and a third server runs on this machine besides, which turned two latent
// defects into live ones: node:sqlite opens with a busy timeout of 0 and a
// rollback journal (so a reader arriving mid-commit fails at once), and the
// router is an async function with no catch (so any throw became an unhandled
// rejection and killed the process). Both are fixed in server.js; these are
// the two tests that hold them fixed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { existsSync } from 'node:fs';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startServer } from './helpers/server-harness.mjs';

const json = { 'content-type': 'application/json' };
const put = (base, cards, board) => fetch(
  base + `/api/state?board=${board}`,
  { method: 'PUT', headers: json, body: JSON.stringify({ version: 1, cards }) });
const newBoard = async (base, name) => (await (await fetch(base + '/api/boards', {
  method: 'POST', headers: json, body: JSON.stringify({ name }),
})).json()).board;

// This is an integration test: it spawns two real servers over one temp database.
test('two servers writing one board file lose neither save nor process',
  async () => {
    const dir = mkdtempSync(join(tmpdir(), 'lodestar-2w-'));
    const shared = { BOARD_DB: join(dir, 'board.db'),
      ASSISTANT_DB: join(dir, 'assistant.db') };
    const a = await startServer({ env: shared });
    const b = await startServer({ env: shared });
    try {
      // WAL is the half of the fix that lets a reader and a writer coexist, and
      // it is a property of the FILE — so both processes inherit it and the log
      // sidecar appears beside the database.
      const mode = new DatabaseSync(shared.BOARD_DB, { readOnly: true })
        .prepare('PRAGMA journal_mode').get().journal_mode;
      assert.equal(mode, 'wal', 'the board file is not in WAL mode');
      assert.ok(existsSync(shared.BOARD_DB + '-wal'), 'no -wal sidecar was created');

      // A board each, because the whole-board sweep is board-scoped: this way
      // every PUT still carries the complete card list for the board it names,
      // and neither server's writes are a partial save of the other's.
      const boardA = await newBoard(a.base, 'A');
      const boardB = await newBoard(b.base, 'B');

      // Interleaved on purpose: whichever process is committing, the other is
      // reading or writing. Before the fix this is where SQLITE_BUSY surfaced
      // as `400 "Invalid JSON: database is locked"` and the save vanished.
      const rounds = [];
      for (let i = 0; i < 12; i += 1) {
        rounds.push(put(a.base, [{ id: `a${i}`, columnId: 'inbox', title: `A ${i}` }], boardA.id));
        rounds.push(put(b.base, [{ id: `b${i}`, columnId: 'inbox', title: `B ${i}` }], boardB.id));
        rounds.push(fetch(`${a.base}/api/trash?board=${boardB.id}`));
        rounds.push(fetch(`${b.base}/api/state?board=${boardA.id}`));
      }
      const results = await Promise.all(rounds);
      for (const r of results) {
        assert.equal(r.status, 200,
          `a request failed with ${r.status}: ${JSON.stringify(await r.json())}`);
      }

      // Neither process died, and each board's last save is the one that stands.
      assert.equal(a.proc.exitCode, null, 'server A exited');
      assert.equal(b.proc.exitCode, null, 'server B exited');
      const seen = await (await fetch(`${b.base}/api/state?board=${boardA.id}`)).json();
      assert.deepEqual(seen.cards.map((c) => c.id), ['a11'],
        "server A's last save is not what server B reads");
      const other = await (await fetch(`${a.base}/api/state?board=${boardB.id}`)).json();
      assert.deepEqual(other.cards.map((c) => c.id), ['b11']);
    } finally {
      await a.stop();
      await b.stop();
    }
  });

// This is an integration test.
test('a throwing route answers 500 and the server keeps serving', async () => {
  const s = await startServer();
  try {
    // A real crash vector, not a stub: this path reaches decodeURIComponent on
    // a malformed escape, which raises URIError. As an unhandled rejection in
    // the async router that used to end the process.
    const bad = await fetch(s.base + '/api/boards/%E0%A4%A');
    assert.equal(bad.status, 500);
    // The message says nothing: an exception can carry a filesystem path or a
    // connection string, and the browser is not where either belongs.
    assert.deepEqual(await bad.json(), { error: 'Server error' });

    // The assertion that actually gates the bug — the process survived it.
    const after = await fetch(s.base + '/api/state');
    assert.equal(after.status, 200);
    assert.equal(s.proc.exitCode, null, 'the server died on one bad request');
  } finally {
    await s.stop();
  }
});
