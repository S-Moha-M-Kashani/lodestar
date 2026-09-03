import { boardSuffix } from './boards.js';
import { categories } from './categories.js';
import { HISTORY_KEY, HISTORY_LIMIT, STORAGE_KEY } from './keys.js';
import { setCards, setDealCards, state } from './state.js';
import { pushToServer } from './sync.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';

// History — an append-only timeline of board snapshots, like a git reflog.
// `index` is where the board currently stands; undo/restore only move the
// pointer, so no state is ever lost until it falls off the HISTORY_LIMIT end.

export const snapshot = (cards) => JSON.parse(JSON.stringify(cards));
export const short = (s) => (s.length > 42 ? s.slice(0, 39) + '…' : s);

export let timeline = { entries: [], index: -1 };

/** Load the timeline, and open it with the board as it stands if there is
 *  nothing saved.
 *
 *  Called from main.js rather than run on import, because the opening entry
 *  needs `state.cards` and this module sits on a cycle back to core/state.js
 *  (state → cards → history → state). Reading `state` while that cycle is still
 *  being evaluated throws "Cannot access 'state' before initialization" and
 *  takes the whole page with it — the one-file version got away with it because
 *  a single closure runs strictly top to bottom. */
export function initTimeline() {
  try {
    // Per board, like the board cache: an undo restores card snapshots, so a
    // shared timeline would let one board's undo deal another board's cards.
    const raw = localStorage.getItem(HISTORY_KEY + boardSuffix);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.entries) && Number.isInteger(parsed.index)) timeline = parsed;
    }
  } catch (_) { /* private mode */ }
  if (!timeline.entries.length) {
    timeline.entries = [{ ts: Date.now(), action: 'Board opened', cards: snapshot(state.cards) }];
  }
  timeline.index = Math.min(Math.max(timeline.index, 0), timeline.entries.length - 1);
}

// ── Persisting the timeline ──────────────────────────────────────────────────
//
// The timeline in memory is authoritative and every edit updates it at once;
// only the *write* to localStorage is coalesced. It has to be: the stored value
// is the whole log, so each write serialises up to HISTORY_LIMIT snapshots of
// the entire board. Measured on 2026-09-02 with a 30-card board and a burst of
// 20 edits: 20 writes and 2,364,061 bytes serialised, synchronously, on the
// thread that also has to paint. Coalesced, the same burst is 1 write and
// 215,840 bytes — and none of it while the typing is happening.
//
// Deliberately not a plain debounce (design.md, decision 1): a debounce only
// ever moves the write later, so a burst that ends with the tab being hidden or
// the page being replaced loses it. What is here instead is one scheduled flush
// per burst *plus* an explicit `flushHistory` on the page-lifecycle events.

// Upper bound on how long an edit may sit unpersisted. Passed to
// requestIdleCallback as its `timeout`, so it is a deadline and not a delay:
// the browser fires the callback at the first idle moment and this number is
// only what happens when idle never comes. A judgement, not a measurement —
// long enough that a burst of typing coalesces into one write, short enough
// that a crash costs at most a second of history.
const HISTORY_IDLE_TIMEOUT_MS = 1000;

let historyDirty = false;   // the timeline holds something localStorage does not
let flushHandle = null;     // the scheduled flush, if one is outstanding
let flushKind = null;       // 'idle' | 'timeout' — which canceller to use

/** The actual write. Never throws: a full or disabled localStorage must not
 *  take down the edit that triggered the save, and the undo stack lives in
 *  memory and keeps working without it. Returns whether the write landed. */
function writeTimeline() {
  try {
    localStorage.setItem(HISTORY_KEY + boardSuffix, JSON.stringify(timeline));
    return true;
  } catch (err) {
    // Left dirty on purpose, so the next burst's flush retries rather than
    // silently deciding the log is safely on disk. No reschedule here: a quota
    // that is full stays full, and a self-rescheduling flush would spin.
    console.warn('Could not save history.', err);
    return false;
  }
}

function cancelScheduledFlush() {
  if (flushHandle === null) return;
  if (flushKind === 'idle') {
    if (typeof globalThis.cancelIdleCallback === 'function') globalThis.cancelIdleCallback(flushHandle);
  } else {
    clearTimeout(flushHandle);
  }
  flushHandle = null;
  flushKind = null;
}

/** Mark the timeline unsaved and make sure exactly one flush is scheduled.
 *
 *  requestIdleCallback is read off `globalThis` at call time rather than
 *  captured, because it is the feature detection: Safari still ships without
 *  it, and there the setTimeout branch is the whole mechanism. */
function queueTimelineSave() {
  historyDirty = true;
  if (flushHandle !== null) return; // one flush per burst
  const idle = globalThis.requestIdleCallback;
  if (typeof idle === 'function') {
    flushKind = 'idle';
    flushHandle = idle(() => { flushHandle = null; flushKind = null; flushHistory(); },
      { timeout: HISTORY_IDLE_TIMEOUT_MS });
  } else {
    flushKind = 'timeout';
    flushHandle = setTimeout(() => { flushHandle = null; flushKind = null; flushHistory(); },
      HISTORY_IDLE_TIMEOUT_MS);
  }
}

/** Persist the timeline now, if it holds anything unsaved, and drop any
 *  scheduled flush. Returns whether a write actually landed — false both when
 *  there was nothing to save and when the write was refused. */
export function flushHistory() {
  cancelScheduledFlush();
  if (!historyDirty) return false;
  const ok = writeTimeline();
  historyDirty = !ok;
  return ok;
}

/** Write the timeline immediately.
 *
 *  Kept as the write-through call for the rare, single-shot writers — a purge
 *  scrubbing a card out of every snapshot (core/trash.js), a board adopted from
 *  the server (core/sync.js). Neither is an edit-rate path, and a purge in
 *  particular is a promise about what is gone: delaying it would leave a
 *  localStorage copy from which History could deal the purged card again. */
export function saveTimeline() {
  cancelScheduledFlush();
  historyDirty = !writeTimeline();
}

// The lifecycle pair. `visibilitychange` to hidden and `pagehide` are the two
// events a browser actually guarantees — beforeunload/unload are skipped
// outright when a page is frozen or discarded from the back/forward cache, so
// they are the wrong place for the last write of a session.
//
// Registered as this module evaluates, which is allowed: the handlers only ever
// run on a real event, long after boot. *Calling* into core/state.js during
// evaluation is what breaks the state → cards → history → state cycle.
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushHistory();
  });
}
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('pagehide', () => flushHistory());
}

/** Write the board to this browser's cache and, unless told not to, queue the
 *  save that carries it to the server.
 *
 *  `{ push: false }` is for a board that just *came* from the server: it still
 *  belongs in the cache, but pushing it back is a round trip whose only possible
 *  outcome is losing a race with a real edit. */
export function saveState({ push = true } = {}) {
  try {
    localStorage.setItem(STORAGE_KEY + boardSuffix, JSON.stringify({ ...state, categories }));
  } catch (err) {
    console.warn('Could not save board.', err);
  }
  if (push) pushToServer(); // keep the SQLite-backed server in sync when one is present
}

export function commit(action) {
  saveState();
  recordEntry(action);
  render();
}

/** Append the board as it stands to the timeline and queue its persistence.
 *
 *  Split out of `commit` so the coalescing can be exercised without a DOM:
 *  `render()` reaches the whole of ui/, and a test that dragged it in would be
 *  a smoke test of the renderer wearing a history test's name. Everything
 *  `commit` does to the undo stack happens here, synchronously. */
export function recordEntry(action) {
  timeline.entries.push({ ts: Date.now(), action, cards: snapshot(state.cards) });
  if (timeline.entries.length > HISTORY_LIMIT) {
    timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
  }
  timeline.index = timeline.entries.length - 1;
  queueTimelineSave();
}

/** Point the board at timeline entry `i` without writing a new entry. */
export function restoreEntry(i, message) {
  const entry = timeline.entries[i];
  if (!entry) return;
  setCards(snapshot(entry.cards));
  timeline.index = i;
  saveState();
  saveTimeline();
  setDealCards(true);
  render();
  announce(message);
}
