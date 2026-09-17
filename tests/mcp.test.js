// tests/mcp.test.js
//
// The MCP surface: the board, reachable from a local MCP client (Claude Code,
// Claude Desktop) over stdio. It is a *client* of the board's HTTP API and
// never of the database — so the running server stays the single writer, the
// rev handshake still decides who may delete, and every write is the ordinary
// whole-board PUT a browser makes.
//
// Four things here can go quietly wrong, and each one of them loses work:
//
//   1. **A partial card list.** Invariant 1: `PUT /api/state` soft-deletes
//      every card the save omits. A tool that sent only the card it changed
//      would archive the whole board on one edit. Every write is therefore
//      read-full-state → modify → PUT-everything, and the test that matters is
//      not "the edit landed" but "the neighbours are still alive".
//   2. **A delete that erases.** The board's durability promise is that a card
//      is destroyed only by Trash → Delete permanently, and the owner asked
//      for that to stay on the board itself. So `DELETE /api/cards/:id` has no
//      tool at all — absent rather than gated, the rule `BoardClient` already
//      follows by carrying no whole-board write. A deleted card is in the
//      Trash, by name, and restorable from here.
//   3. **A filter that is nearly the board's.** The keyword search is one
//      haystack of `title + ' ' + notes + ' ' + tags.join(' ')`, which is
//      `matchesFilters` in js/core/cards.js. Three separate substring tests
//      would answer differently for a query spanning the end of the title and
//      the start of the notes, and a near-miss is the one nobody can explain.
//   4. **A cap reported as a total.** "All my travel cards" answered with
//      twenty of forty is a sentence the model goes on to reason from, so the
//      answer states how many matched before the cap.
//
// mcp/filter.mjs imports only import-free leaves (js/core/constants.js,
// js/core/timespan.js) — the js/core/plan.js, nesting.js, lenses.js rule. The
// moment it reaches ui/dom.js the first test here stops loading under node and
// the whole filter table becomes an integration-only surface.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { TOOL_BY_NAME } from '../mcp/tools.mjs';
import { selectCards } from '../mcp/filter.mjs';
import { startServer, TEST_SERVICE_TOKEN } from './helpers/server-harness.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// --------------------------------------------------------------------------
// A card, with every field the board stores, so a filter cannot pass here by
// reading a field the real board would not have handed it.
// --------------------------------------------------------------------------
const card = (over = {}) => ({
  id: 'id-' + (over.num ?? 1), num: over.num ?? 1, columnId: 'inbox',
  title: 'a card', notes: '', type: 'task', category: '', importance: '',
  urgency: '', effort: 'medium', control: 'influence', effortSrc: 'default',
  controlSrc: 'default', deadline: '', plan: '', planSrc: 'auto',
  habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
  parentId: '', tags: [], createdAt: 1_700_000_000_000,
  updatedAt: 1_700_000_000_000, doneAt: 0, ...over,
});

// This is a unit test.
test('the filters are the board\'s own, they AND together, and the total survives the cap', () => {
  const cards = [
    card({ num: 1, title: 'book the', notes: 'mountain hut', tags: ['travel'] }),
    card({ num: 2, title: 'call the bank', category: 'money', importance: 'high',
           deadline: '2026-09-20', tags: ['admin', 'urgent'] }),
    card({ num: 3, title: 'renew passport', category: 'travel',
           importance: 'high', deadline: '2026-12-01' }),
    card({ num: 4, title: 'learn the scales', notes: '', parentId: 'id-1' }),
  ];

  // Rule 3: one haystack, so a query running from the last word of the title
  // into the first word of the notes matches — as it does in the search box.
  assert.deepEqual(selectCards(cards, { keyword: 'the mountain' }).cards.map((c) => c.num), [1]);
  // ...and the tags are in it, lower-cased on both sides.
  assert.deepEqual(selectCards(cards, { keyword: 'ADMIN' }).cards.map((c) => c.num), [2]);

  // Every filter that is given narrows; an absent one narrows nothing. AND.
  assert.deepEqual(
    selectCards(cards, { importance: 'high', category: 'travel' }).cards.map((c) => c.num), [3]);
  // '' is a real question — the cards nobody has judged — and not "unfiltered".
  assert.deepEqual(
    selectCards(cards, { importance: '' }).cards.map((c) => c.num).sort(), [1, 4]);

  // The scope filter, and the lineage it reads.
  assert.deepEqual(selectCards(cards, { scope: 'children' }).cards.map((c) => c.num), [4]);
  assert.deepEqual(selectCards(cards, { childOf: 'id-1' }).cards.map((c) => c.num), [4]);

  assert.deepEqual(
    selectCards(cards, { hasDeadline: true }).cards.map((c) => c.num).sort(), [2, 3]);

  // A date span, on the field named. Fixed dates on both sides, so this test
  // cannot change its answer overnight — the tests/timespan.test.js rule.
  assert.deepEqual(
    selectCards(cards, { deadline: { start: '2026-09-01', end: '2026-09-30' } })
      .cards.map((c) => c.num), [2]);

  // A card with no value for the sort field goes last in BOTH directions: a
  // list whose head is two undated cards is a list nobody reads.
  const byDue = (order) =>
    selectCards(cards, { sortBy: 'deadline', order }).cards.map((c) => c.num);
  assert.deepEqual(byDue('asc').slice(0, 2), [2, 3]);
  assert.deepEqual(byDue('desc').slice(0, 2), [3, 2]);

  // Rule 4: the cap is visible, never silent.
  const capped = selectCards(cards, { limit: 2 });
  assert.equal(capped.total, 4);
  assert.equal(capped.cards.length, 2);
});

// --------------------------------------------------------------------------
// One MCP client, speaking newline-delimited JSON-RPC over stdio — which is
// what the transport is. Written here rather than mocked: the protocol handler
// is half the thing under test, and a fake one would pass against a server no
// client can talk to.
// --------------------------------------------------------------------------
function startMcp(boardUrl) {
  const proc = spawn('node', ['mcp/server.mjs'], {
    cwd: ROOT,
    env: { ...process.env, NODE_NO_WARNINGS: '1', LODESTAR_BOARD_URL: boardUrl,
           LODESTAR_SERVICE_TOKEN: TEST_SERVICE_TOKEN },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stderr = '';
  proc.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  const waiting = new Map();
  let buf = '';
  proc.stdout.on('data', (chunk) => {
    buf += chunk.toString();
    let cut;
    while ((cut = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, cut).trim();
      buf = buf.slice(cut + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      const pending = waiting.get(msg.id);
      if (pending) { waiting.delete(msg.id); pending(msg); }
    }
  });
  let seq = 0;
  const send = (method, params) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(
      () => reject(new Error(`${method} timed out; stderr:\n${stderr}`)), 10000);
    waiting.set(id, (msg) => { clearTimeout(timer); resolve(msg); });
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
  const notify = (method, params) =>
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
  // The text of a tool's answer — what a client actually shows.
  const call = async (name, args = {}) => {
    const msg = await send('tools/call', { name, arguments: args });
    assert.ok(!msg.error, `${name} errored: ${JSON.stringify(msg.error)}`);
    return { text: (msg.result.content || []).map((c) => c.text || '').join('\n'),
             isError: Boolean(msg.result.isError) };
  };
  return { send, notify, call, stop: () => proc.kill('SIGKILL') };
}

async function handshake(boardUrl) {
  const mcp = startMcp(boardUrl);
  const hello = await mcp.send('initialize', {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'tests/mcp.test.js', version: '0' },
  });
  assert.ok(!hello.error, `initialize errored: ${JSON.stringify(hello.error)}`);
  assert.ok(hello.result.protocolVersion, 'no protocol version in the handshake');
  assert.match(hello.result.serverInfo.name, /lodestar/i);
  mcp.notify('notifications/initialized');
  return mcp;
}

// A card's id, read off the line the tool actually prints. Every listing ends
// a card with `[<id>]` precisely so that the next call can name it, so this is
// the contract being read rather than a guess at an id's shape.
const idOf = (text) => {
  const found = text.match(/\[([^\]\s]+)\]\s*$/m);
  assert.ok(found, `no card id in the answer:\n${text}`);
  return found[1];
};

// This is an integration test: a real board server on a temp database, and the
// MCP server as a separate process speaking the real transport.
test('a card can be written down, found, changed, deleted and restored', async (t) => {
  const s = await startServer({ env: { LODESTAR_SERVICE_TOKEN: TEST_SERVICE_TOKEN } });
  const mcp = await handshake(s.base);
  t.after(async () => { mcp.stop(); await s.stop(); });

  // The tools the client is offered. Named here so a tool cannot be renamed
  // out from under a saved client config without this failing.
  const listed = await mcp.send('tools/list');
  const names = listed.result.tools.map((tool) => tool.name).sort();
  assert.deepEqual(names, ['create_card', 'delete_card', 'describe_board',
                           'get_card', 'list_cards', 'list_trash',
                           'restore_card', 'update_card']);
  // Every tool says what it takes, or a client cannot call it at all.
  for (const tool of listed.result.tools) {
    assert.equal(tool.inputSchema.type, 'object', `${tool.name} has no input schema`);
    assert.ok(tool.description, `${tool.name} has no description`);
  }

  const made = await mcp.call('create_card', {
    title: 'renew the passport', notes: 'the swiss one', type: 'task',
    deadline: '2026-12-01', tags: ['travel', 'admin'],
  });
  const id = idOf(made.text);
  // A ledger number is earned at creation, not left at 0 for a browser to fill
  // in — a card reading C-000 until somebody reloads is a card with no name.
  assert.match(made.text, /\bC-0*[1-9]\d*\b/);

  // It is on the board, through the board's own read.
  const board = await (await fetch(s.base + '/api/state')).json();
  const stored = board.cards.find((c) => c.id === id);
  assert.equal(stored.title, 'renew the passport');
  assert.deepEqual(stored.tags, ['travel', 'admin']);
  assert.equal(stored.deadline, '2026-12-01');

  // Found by the search the board's own box would have run.
  const found = await mcp.call('list_cards', { keyword: 'passport' });
  assert.match(found.text, /renew the passport/);
  const byTag = await mcp.call('list_cards', { tagsAll: ['travel'], columnId: 'inbox' });
  assert.match(byTag.text, /renew the passport/);

  // One card, in full — every field, or it cannot be edited from here.
  const full = await mcp.call('get_card', { card: id });
  assert.match(full.text, /the swiss one/);
  assert.match(full.text, /2026-12-01/);

  // An edit names one field and leaves the rest exactly as they were. Filing
  // it in Done is how a card is finished, and the board stamps the date.
  await mcp.call('update_card', { card: id, columnId: 'answered' });
  const after = await (await fetch(s.base + '/api/state')).json();
  const done = after.cards.find((c) => c.id === id);
  assert.equal(done.columnId, 'answered');
  assert.ok(done.doneAt > 0, 'a finished card carries no finished date');
  assert.equal(done.title, 'renew the passport');
  assert.equal(done.notes, 'the swiss one');
  assert.deepEqual(done.tags, ['travel', 'admin']);

  // Rule 2: deleting moves the card to the Trash and destroys nothing.
  await mcp.call('delete_card', { card: id });
  const live = await (await fetch(s.base + '/api/state')).json();
  assert.equal(live.cards.find((c) => c.id === id), undefined);
  const trash = await (await fetch(s.base + '/api/trash')).json();
  assert.ok(trash.cards.some((c) => c.id === id), 'the card was erased, not trashed');
  const listedTrash = await mcp.call('list_trash');
  assert.match(listedTrash.text, /renew the passport/);

  // A ledger number belongs to a card for life, so a card in the Trash still
  // holds its own. `ensureNums` counts the live cards alone — it is the
  // browser's, and the browser holds nothing else — so the next card written
  // after the highest one is deleted would claim that number a second time,
  // and restoring the first would leave the board with two C-004s. Found by
  // running this against the real board rather than by a test asking for it.
  const next = await mcp.call('create_card', { title: 'after the deletion' });
  assert.notEqual(next.text.match(/\bC-\d+\b/)[0], `C-${String(stored.num).padStart(3, '0')}`,
    'a new card took a ledger number the Trash is still holding');

  // ...and the way back is from here too, with the same id and the same number.
  await mcp.call('restore_card', { card: id });
  const back = await (await fetch(s.base + '/api/state')).json();
  const again = back.cards.find((c) => c.id === id);
  assert.ok(again, 'the restored card did not come back');
  assert.equal(again.num, stored.num);
  assert.equal(new Set(back.cards.map((c) => c.num)).size, back.cards.length,
    'two cards share one ledger number');

  // The board's own vocabulary, or a caller has to guess a category id.
  const described = await mcp.call('describe_board');
  assert.match(described.text, /travel/i);
});

// This is an integration test.
test('no tool erases a card, and one delete never touches its neighbours', async (t) => {
  const s = await startServer({ env: { LODESTAR_SERVICE_TOKEN: TEST_SERVICE_TOKEN } });
  const mcp = await handshake(s.base);
  t.after(async () => { mcp.stop(); await s.stop(); });

  // Rule 2, as an absence — asserted where the absence actually lives.
  // A tool list is the whole of what a client can do, so no name may offer a
  // purge...
  const listed = await mcp.send('tools/list');
  for (const tool of listed.result.tools) {
    assert.doesNotMatch(tool.name, /purge|permanent|erase|destroy/,
      `${tool.name} advertises a permanent delete`);
  }
  // ...and the one module that can reach the board issues no DELETE at all.
  // This is the brain's `test_guardrails.py` rule on this side of the house:
  // the guarantee is that the capability is missing, not that nobody calls it,
  // and a behavioural test can only ever sample the calls somebody thought of.
  const client = readFileSync(join(ROOT, 'mcp', 'board.mjs'), 'utf8');
  assert.doesNotMatch(client, /['\"`]DELETE['\"`]/,
    'mcp/board.mjs can issue a DELETE — the board\'s only hard delete is reachable');

  const ids = [];
  for (const title of ['first card', 'second card', 'third card']) {
    ids.push(idOf((await mcp.call('create_card', { title })).text));
  }

  // Rule 1: the write is the whole board, so the two cards nobody named are
  // still live afterwards. This is the assertion that catches a partial PUT —
  // "the delete landed" passes against a save that archived everything.
  await mcp.call('delete_card', { card: ids[1] });
  const live = await (await fetch(s.base + '/api/state')).json();
  assert.deepEqual(live.cards.map((c) => c.id).sort(), [ids[0], ids[2]].sort());

  // The same rule on the edit path: changing one card leaves the others alone.
  await mcp.call('update_card', { card: ids[0], importance: 'high' });
  const after = await (await fetch(s.base + '/api/state')).json();
  assert.deepEqual(after.cards.map((c) => c.id).sort(), [ids[0], ids[2]].sort());
  assert.equal(after.cards.find((c) => c.id === ids[0]).importance, 'high');

  // A card that is not there is refused in words, not by writing a new one:
  // "update the card I mean" must never be able to mint a second copy.
  const missing = await mcp.call('update_card', { card: 'id-nope', importance: 'low' });
  assert.ok(missing.isError, 'editing a card that does not exist was not refused');
  const untouched = await (await fetch(s.base + '/api/state')).json();
  assert.equal(untouched.cards.length, 2);
});

// This is an integration test.
test('a save the board has moved past is retried, and the retry does not write the card twice', async (t) => {
  const s = await startServer({ env: { LODESTAR_SERVICE_TOKEN: TEST_SERVICE_TOKEN } });
  // The tool is called in this process rather than over stdio, because what is
  // under test is the window *inside* one call — between the read and the PUT —
  // and there is no way to reach into a subprocess at that moment.
  const restore = { url: process.env.LODESTAR_BOARD_URL, token: process.env.LODESTAR_SERVICE_TOKEN };
  process.env.LODESTAR_BOARD_URL = s.base;
  process.env.LODESTAR_SERVICE_TOKEN = TEST_SERVICE_TOKEN;
  const realFetch = globalThis.fetch;
  t.after(async () => {
    globalThis.fetch = realFetch;
    process.env.LODESTAR_BOARD_URL = restore.url ?? '';
    process.env.LODESTAR_SERVICE_TOKEN = restore.token ?? '';
    await s.stop();
  });

  // Somebody else saves this board in the window between our read and our
  // write — a browser tab is usually open on it, so this is the ordinary case
  // and not a contrived one. Run from a subprocess so the interference is
  // *synchronous* from here and lands exactly where it is meant to: two
  // overlapping awaits would be a race, and a race that usually passes is
  // worse than no test at all.
  const interfere = () => execFileSync('node', ['--input-type=module', '-e', `
    const head = { 'content-type': 'application/json',
                   authorization: 'Bearer ' + process.env.T };
    const state = await (await fetch(process.env.B + '/api/state', { headers: head })).json();
    await fetch(process.env.B + '/api/state', { method: 'PUT', headers: head,
      body: JSON.stringify({ cards: [...state.cards,
        { title: 'written by somebody else' }], rev: state.rev }) });
  `], { env: { ...process.env, B: s.base, T: TEST_SERVICE_TOKEN } });

  let puts = 0;
  globalThis.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input?.url ?? '';
    // Only the FIRST save is overtaken. The retry has to be allowed to land,
    // or this would be testing the give-up path instead.
    if (init?.method === 'PUT' && url.includes('/api/state') && puts++ === 0) interfere();
    return realFetch(input, init);
  };

  const answer = await TOOL_BY_NAME.get('create_card').run({ title: 'written by the tool' });
  assert.equal(puts, 2, 'the stale save was not retried');

  const board = await realFetch(s.base + '/api/state',
    { headers: { authorization: `Bearer ${TEST_SERVICE_TOKEN}` } }).then((r) => r.json());
  // The stale save is applied *additively*, so the card is already on the board
  // when the retry runs. A change that appended would append a second copy —
  // and the owner would find two of everything they wrote with a tab open.
  assert.equal(board.cards.filter((c) => c.title === 'written by the tool').length, 1,
    'the retry wrote the card twice');
  // A retry must not undo the write that made it necessary.
  assert.equal(board.cards.filter((c) => c.title === 'written by somebody else').length, 1,
    'the retry archived the card that arrived in between');
  // ...and the one card that landed is the one the tool reported, number and all.
  const made = board.cards.find((c) => c.title === 'written by the tool');
  assert.match(answer.content[0].text, new RegExp(`C-0*${made.num}\\b`));
  assert.equal(new Set(board.cards.map((c) => c.num)).size, board.cards.length,
    'two cards share one ledger number');
});
