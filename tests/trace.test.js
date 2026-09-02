// tests/trace.test.js
//
// The developer trace surface: where a turn's exact model envelope is stored,
// and the door in front of the page that reads it.
//
// Two things make this worth its own file. The first is that the trace holds
// the most sensitive text this application ever assembles — the system prompt,
// the user's question, and everything the tools returned about their board, in
// one record — so the door has to be tested as a door, not as a feature. The
// second is that the whole surface must be *absent* without `LODESTAR_DEV_KEY`:
// not merely locked, absent, so a board running the ordinary way has no
// developer page to find.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startServer, rawFetch, authorize } from './helpers/server-harness.mjs';

const DEV_KEY = 'dev-key-for-tests-9f2a';

/** One turn's envelope, in the shape the brain files. */
function trace(over = {}) {
  return {
    trace_id: 'tr-1', session_id: 'chat-1', board_id: 'main',
    status: 'completed', model: 'fake', provider: 'fake',
    started_at: 1_700_000_000_000, ended_at: 1_700_000_003_400,
    error: '', usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
    entries: [
      { seq: 0, role: 'system', content: 'you are lodestar', metadata: {} },
      { seq: 1, role: 'human', content: 'which cards are about work?', metadata: {} },
      { seq: 2, role: 'ai', content: '', metadata: { tool_calls: [
        { name: 'list_cards', args: { column: 'inbox' }, id: 'c0' }] } },
      { seq: 3, role: 'tool', content: '[{"id":"C-001"}]',
        metadata: { tool_call_id: 'c0', name: 'list_cards' } },
      { seq: 4, role: 'ai', content: 'One: C-001.', metadata: {} },
    ],
    ...over,
  };
}

const post = (base, path, body) => fetch(base + path, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

/** Unlock the trace surface the way a developer does — a form post — and
 *  return a cookie header carrying BOTH credentials.
 *
 *  Both, because that is the requirement: the dev key is a second lock in front
 *  of the ordinary session, so a request holding only one of the two must not
 *  get in, and a test that sent only the dev cookie would be proving the wrong
 *  thing when it 401'd. The harness attaches the session automatically to every
 *  bare fetch, but not when a cookie header is set by hand — which is exactly
 *  what these requests do. */
async function unlock(base, key = DEV_KEY) {
  const res = await fetch(base + '/dev/trace/unlock', {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: `key=${encodeURIComponent(key)}`, redirect: 'manual',
  });
  const dev = (res.headers.get('set-cookie') || '').split(';')[0];
  const session = await authorize(base);
  return { res, dev, cookie: dev ? `${session}; ${dev}` : '' };
}

const get = (base, path, cookie) =>
  fetch(base + path, { headers: cookie ? { cookie } : {} });

// This is an integration test.
test('with no dev key the whole surface is absent, not merely locked', async () => {
  const s = await startServer();
  try {
    for (const path of ['/dev/trace', '/dev/trace?session=chat-1', '/dev/trace/unlock']) {
      const res = await fetch(s.base + path);
      assert.equal(res.status, 404, `${path} must not exist without a dev key`);
    }
    // And nothing can be filed either: a board that cannot show traces has no
    // business storing them.
    const res = await post(s.base, '/api/trace', trace());
    assert.equal(res.status, 404);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a filed trace is stored once per turn and read back whole', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    assert.equal((await post(s.base, '/api/trace', trace({ status: 'in_flight', ended_at: null }))).status, 200);
    assert.equal((await post(s.base, '/api/trace', trace())).status, 200);

    const { cookie } = await unlock(s.base);
    const detail = await get(s.base, '/dev/trace?session=chat-1', cookie);
    assert.equal(detail.status, 200);
    const html = await detail.text();

    // One record, not two: the second filing updates the first.
    assert.equal(html.match(/data-trace="tr-1"/g).length, 1);
    // The tape, in the model's order, with the tool call and its answer
    // associated — and the status said out loud rather than inferred.
    // Anchored on the element, not on the attribute: the page's own stylesheet
    // names `[data-role="system"]` too, and a looser match reads the CSS as
    // part of the transcript.
    const order = [...html.matchAll(/<div class="entry" data-role="(\w+)"/g)].map((m) => m[1]);
    assert.deepEqual(order, ['system', 'human', 'ai', 'tool', 'ai']);
    assert.match(html, /you are lodestar/);
    assert.match(html, /list_cards/);
    assert.match(html, /completed/);
    // No-store, always: a trace is read to see the current state of a turn, and
    // a cached copy of it is a wrong answer that looks like a right one.
    assert.equal(detail.headers.get('cache-control'), 'no-store');
  } finally { await s.stop(); }
});

// This is an integration test.
test('two prompts in one session stay two prompts', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    await post(s.base, '/api/trace', trace());
    await post(s.base, '/api/trace', trace({
      trace_id: 'tr-2', started_at: 1_700_000_010_000,
      entries: [{ seq: 0, role: 'human', content: 'and the rest?', metadata: {} },
                { seq: 1, role: 'ai', content: 'Two more.', metadata: {} }],
    }));
    const { cookie } = await unlock(s.base);
    const html = await (await get(s.base, '/dev/trace?session=chat-1', cookie)).text();
    assert.equal(html.match(/data-trace="tr-\d"/g).length, 2);
    // The second prompt's messages are not merged into the first's group.
    assert.ok(html.indexOf('which cards are about work?') < html.indexOf('and the rest?'));

    const index = await (await get(s.base, '/dev/trace', cookie)).text();
    assert.match(index, /chat-1/);
    assert.match(index, /2/);   // two prompts under that session
  } finally { await s.stop(); }
});

// This is an integration test.
test('an interrupted turn keeps what happened and claims no answer', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    await post(s.base, '/api/trace', trace({
      trace_id: 'tr-bad', status: 'failed', error: 'upstream exploded',
      entries: [{ seq: 0, role: 'system', content: 'sys', metadata: {} },
                { seq: 1, role: 'human', content: 'go', metadata: {} }],
    }));
    const { cookie } = await unlock(s.base);
    const html = await (await get(s.base, '/dev/trace?session=chat-1', cookie)).text();
    assert.match(html, /failed/);
    assert.match(html, /upstream exploded/);
    assert.equal([...html.matchAll(/<div class="entry" data-role="(\w+)"/g)].map((m) => m[1]).join(),
      'system,human');
  } finally { await s.stop(); }
});

// This is an integration test.
test('the door: a wrong key is refused, the right one is never echoed', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    const locked = await fetch(s.base + '/dev/trace');
    assert.equal(locked.status, 200);
    const form = await locked.text();
    assert.match(form, /type="password"/);
    // The form is all a locked visitor gets: no session list, no trace.
    assert.doesNotMatch(form, /data-trace=/);

    const wrong = await unlock(s.base, 'not-the-key');
    assert.equal(wrong.res.status, 401);
    const said = await wrong.res.text();
    assert.doesNotMatch(said, /not-the-key/, 'never echo what was submitted');
    assert.equal(wrong.dev, '', 'a wrong key mints no token');

    const { res, cookie } = await unlock(s.base);
    assert.equal(res.status, 303, 'a right key redirects to the page asked for');
    const setCookie = res.headers.get('set-cookie') || '';
    assert.match(setCookie, /HttpOnly/);
    assert.match(setCookie, /SameSite=Strict/);
    assert.match(setCookie, /Path=\/dev\/trace/);
    assert.doesNotMatch(setCookie, new RegExp(DEV_KEY),
      'the cookie carries a random token, never the key');
    // And the key never reaches a URL: the unlock is a POST and the redirect
    // goes to a bare path.
    assert.doesNotMatch(res.headers.get('location') || '', new RegExp(DEV_KEY));

    // Locking gives the token back.
    const lock = await fetch(s.base + '/dev/trace/lock', {
      method: 'POST', headers: { cookie }, redirect: 'manual' });
    assert.equal(lock.status, 303);
    const after = await (await get(s.base, '/dev/trace', cookie)).text();
    assert.match(after, /type="password"/, 'a revoked token unlocks nothing');
  } finally { await s.stop(); }
});

// This is an integration test.
test('the trace surface is behind the ordinary login as well as the dev key', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY }, login: false });
  try {
    const res = await rawFetch(s.base + '/dev/trace');
    assert.equal(res.status, 401, 'the dev key is a second lock, never the only one');
    const filed = await rawFetch(s.base + '/api/trace', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(trace()) });
    assert.equal(filed.status, 401);
  } finally { await s.stop(); }
});

// This is an integration test.
test('trace text is escaped, and no trace route changes anything', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    const before = await (await fetch(s.base + '/api/state')).json();
    await post(s.base, '/api/trace', trace({
      entries: [{ seq: 0, role: 'human',
                  content: '<script>alert("xss")</script>', metadata: {} }],
    }));
    const { cookie } = await unlock(s.base);
    const html = await (await get(s.base, '/dev/trace?session=chat-1', cookie)).text();
    assert.doesNotMatch(html, /<script>alert/);
    assert.match(html, /&lt;script&gt;/);

    // Reading a trace touches neither the board nor the chat record.
    const after = await (await fetch(s.base + '/api/state')).json();
    assert.deepEqual(after.cards.map((c) => c.id), before.cards.map((c) => c.id));
    const chat = await (await fetch(s.base + '/api/chat/sessions')).json();
    assert.deepEqual(chat.sessions, []);
  } finally { await s.stop(); }
});

// This is an integration test.
test('a malformed or unknown session is a page, not a crash', async () => {
  const s = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    const { cookie } = await unlock(s.base);
    for (const q of ['?session=', '?session=%zz', '?session=nope',
                     "?session=' OR 1=1 --"]) {
      const res = await get(s.base, '/dev/trace' + q, cookie);
      assert.ok(res.status === 200 || res.status === 404,
        `${q} answered ${res.status}`);
    }
    // A body that is not a trace is refused, not stored.
    assert.equal((await post(s.base, '/api/trace', { nope: true })).status, 400);
    assert.equal((await post(s.base, '/api/trace', trace({ trace_id: '' }))).status, 400);
  } finally { await s.stop(); }
});

// This is an integration test.
test('the browser can ask whether tracing is available at all', async () => {
  const off = await startServer();
  try {
    const res = await fetch(off.base + '/api/trace/status');
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { enabled: false });
  } finally { await off.stop(); }

  const on = await startServer({ env: { LODESTAR_DEV_KEY: DEV_KEY } });
  try {
    assert.deepEqual(await (await fetch(on.base + '/api/trace/status')).json(),
      { enabled: true });
  } finally { await on.stop(); }
});
