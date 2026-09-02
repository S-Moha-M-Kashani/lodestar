// tests/history.test.js — coalesced persistence of the undo timeline.
//
// The stored history is the *whole* log: one localStorage key holding up to
// HISTORY_LIMIT (50) snapshots of the entire board. So every edit used to
// re-serialise every snapshot, synchronously, on the thread that also paints.
//
// BASELINE, measured 2026-09-02 against the code before this change, with a
// 30-card board (200-char notes) and a burst of 20 edits:
//
//     20 localStorage writes, 2,364,061 bytes serialised.
//
// After, same board and same burst: 1 write, 215,840 bytes — 91% less
// serialising, and none of it during the typing. Those are the numbers this
// file asserts the shape of. What must not change is the undo stack itself: it
// is in memory, it is authoritative, and it is updated on every single edit.
// Only the write is deferred.
//
// Why this can be a node test at all: the coalescing lives in js/core/history.js,
// which reaches ui/ and therefore needs a DOM to *evaluate*. The stub below is
// the whole of that DOM — a permissive proxy that swallows the eval-time wiring
// of the ui/ graph, with real recorders for the two things under test
// (`addEventListener` and `visibilityState`). The functions exercised here
// (`recordEntry`, `flushHistory`, `saveTimeline`) deliberately do not call
// `render()`; `commit()` does, and a test that dragged the renderer in would be
// a smoke test of ui/ wearing a history test's name.
import { test } from 'node:test';
import assert from 'node:assert/strict';

// ── The DOM stub, installed before js/ is imported ───────────────────────────

const listeners = new Map(); // event name -> handlers
const fire = (name) => { for (const fn of listeners.get(name) || []) fn(); };
const visibility = { state: 'visible' };

const swallow = new Proxy(function () {}, {
  get(_t, k) {
    if (k === Symbol.toPrimitive) return () => '';
    if (k === 'length') return 0;
    if (k === 'addEventListener') {
      return (name, fn) => {
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(fn);
      };
    }
    if (k === 'visibilityState') return visibility.state;
    return swallow;
  },
  set() { return true; },
  apply() { return swallow; },
  has() { return true; },
});

const store = new Map();
let writes = [];      // every history key written, in order
let refuse = false;   // stand-in for an exceeded quota / disabled storage

globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem(k, v) {
    if (String(k).includes('history')) {
      if (refuse) throw new Error('QuotaExceededError');
      writes.push(String(v));
    }
    store.set(k, String(v));
  },
  removeItem: (k) => { store.delete(k); },
};
globalThis.document = swallow;
globalThis.window = swallow;

// The idle scheduler, under this file's control. history.js reads
// requestIdleCallback off globalThis at call time — that read *is* the feature
// detection for Safari — so deleting it here exercises the setTimeout branch.
let idleQueue = [];
let idleSeq = 0;
const installIdle = () => {
  globalThis.requestIdleCallback = (fn) => { const id = ++idleSeq; idleQueue.push([id, fn]); return id; };
  globalThis.cancelIdleCallback = (id) => { idleQueue = idleQueue.filter(([i]) => i !== id); };
};
const runIdle = () => { const q = idleQueue; idleQueue = []; for (const [, fn] of q) fn(); };

installIdle();

// Imported as a namespace, not destructured: `initTimeline` *reassigns*
// history.js's `timeline`, and a destructured binding would be a copy taken
// before that — a stale object that recordEntry no longer writes into.
const hist = await import('../js/core/history.js');
const { flushHistory, initTimeline, recordEntry, saveTimeline } = hist;
const { setCards, state } = await import('../js/core/state.js');

const HISTORY_KEY = 'lodestar:history';

/** A board big enough that the cost being avoided is visible in the numbers. */
const board = () => Array.from({ length: 30 }, (_, i) => ({
  id: 'c' + i, num: i + 1, title: 'Card ' + i, type: 'task', category: 'work',
  column: 'inbox', notes: 'x'.repeat(200), createdAt: 1, updatedAt: 1, position: i,
}));

/** Put every test on the same footing: a fresh board, an empty log, nothing
 *  scheduled and no recorded writes. */
function reset() {
  refuse = false;
  idleQueue = [];
  store.clear();
  writes = [];
  setCards(board());
  hist.timeline.entries = [{ ts: 1, action: 'Board opened', cards: JSON.parse(JSON.stringify(state.cards)) }];
  hist.timeline.index = 0;
  saveTimeline();  // the opening state is on disk; from here on, writes are ours
  writes = [];
}

const stored = () => JSON.parse(store.get(HISTORY_KEY));

// This is a unit test.
test('a burst of edits updates undo at once and writes once', () => {
  reset();
  installIdle();

  for (let i = 0; i < 20; i++) {
    state.cards[0].title = 'Card 0 rev ' + i;
    recordEntry('edit ' + i);
    // The point of the whole change: undo does not wait for the disk. Every
    // edit is already in the log, and the newest entry is the one just made.
    assert.equal(hist.timeline.entries.length, i + 2);
    assert.equal(hist.timeline.index, i + 1);
    assert.equal(hist.timeline.entries[hist.timeline.index].action, 'edit ' + i);
  }

  // Baseline was 20 writes / 2,364,061 bytes. Nothing has been written yet:
  // one flush is scheduled for the whole burst.
  assert.equal(writes.length, 0, 'a burst must not write per edit');
  assert.equal(idleQueue.length, 1, 'exactly one flush is scheduled per burst');

  runIdle();
  assert.equal(writes.length, 1, '20 edits, one write');
  assert.ok(writes[0].length < 300_000, `one write of ${writes[0].length} bytes, not 2.3 MB`);

  // And the flush is spent: a second one with nothing pending writes nothing.
  assert.equal(flushHistory(), false);
  assert.equal(writes.length, 1);
});

// This is a unit test.
test('without requestIdleCallback the timeout fallback carries the write', () => {
  reset();
  delete globalThis.requestIdleCallback;   // Safari
  delete globalThis.cancelIdleCallback;

  recordEntry('edit on safari');
  assert.equal(writes.length, 0);
  assert.equal(hist.timeline.entries.length, 2, 'undo does not depend on the scheduler');

  // The fallback is a real timer, so let it run rather than asserting on the
  // handle: this is the branch Safari takes for every single edit.
  return new Promise((resolve) => setTimeout(() => {
    assert.equal(writes.length, 1, 'the setTimeout deadline flushed the burst');
    assert.equal(stored().entries.length, 2);
    installIdle();
    resolve();
  }, 1200));
});

// This is a unit test.
test('an interrupted burst is flushed by the lifecycle events, and reloads identically', () => {
  reset();
  installIdle();

  recordEntry('typed something');
  recordEntry('typed some more');
  assert.equal(writes.length, 0, 'still pending when the page goes away');

  // A tab being hidden. beforeunload/unload are deliberately not used — they
  // are skipped outright when a page is frozen or discarded — so these two are
  // the events the pending write hangs off.
  visibility.state = 'hidden';
  fire('visibilitychange');
  visibility.state = 'visible';
  assert.equal(writes.length, 1, 'visibilitychange to hidden persisted the burst');
  assert.equal(idleQueue.length, 0, 'and the scheduled flush was cancelled, not left to fire twice');

  // What comes back after a reload is the same log, not an approximation of it.
  const expected = JSON.parse(JSON.stringify(hist.timeline));
  initTimeline();
  assert.deepEqual(hist.timeline.entries, expected.entries);
  assert.equal(hist.timeline.index, expected.index);
  assert.equal(hist.timeline.entries[hist.timeline.index].action, 'typed some more');

  // The other half of the pair, for a navigation rather than a hide.
  recordEntry('one more edit');
  fire('pagehide');
  assert.equal(writes.length, 2);
  assert.equal(stored().entries.at(-1).action, 'one more edit');
});

// This is a unit test.
test('a refused write leaves undo working, and the next flush retries', () => {
  reset();
  installIdle();
  refuse = true;
  const warn = console.warn;
  const warned = [];
  console.warn = (...a) => warned.push(a[0]);

  try {
    recordEntry('edit while storage is full');
    runIdle();
    assert.match(warned.join(' '), /Could not save history/,
      'the refusal is reported, not swallowed');

    // Nothing was written and nothing was thrown at the caller: the undo stack
    // does not live in localStorage, and it is still growing.
    assert.equal(writes.length, 0);
    assert.equal(hist.timeline.entries.length, 2);
    recordEntry('and another');
    assert.equal(hist.timeline.entries.length, 3);
    assert.equal(hist.timeline.index, 2);

    // Still dirty, deliberately: a failed write must not be mistaken for a
    // completed one, so the next flush tries again and both edits land.
    refuse = false;
    runIdle();
    assert.equal(writes.length, 1);
    assert.deepEqual(stored().entries.map((e) => e.action),
      ['Board opened', 'edit while storage is full', 'and another']);
  } finally { console.warn = warn; }
});
