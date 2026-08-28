import { boardSuffix, boardUrl, leavingBoard } from './boards.js';
import { ensureNums, sanitizeCard } from './cards.js';
import { categories, sanitizeCategories, setCategories } from './categories.js';
import { saveState, saveTimeline, snapshot, timeline } from './history.js';
import { HISTORY_LIMIT, SYNC_KEY } from './keys.js';
import { mergeCardLists } from './merge.js';
import { loadedFromStorage, setCards, setDealCards, state } from './state.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';

// Server sync — when a backend is reachable the board is persisted to its
// SQLite database; otherwise the app runs entirely on localStorage. The
// whole board is pushed on every change, so deleting a card is the only
// thing that removes its row server-side.
//
// Who owns the board, and why it is the server. Until 2026-08-22 a browser that
// had a saved board won on load: it pushed its own copy and never looked at what
// the database held. That is safe with one browser and destroys data with two —
// a second machine opened this board with a days-old copy, and the whole-board
// save archived the 24 cards that copy had never heard of. So: the server is the
// board's source of truth whenever it answers, localStorage is a cache, and this
// browser's copy is pushed as the truth in exactly one case — when it holds
// changes the server never acknowledged. That case is *observed*, by comparing
// the board against a watermark written at the last acknowledged sync, never
// promised by a flag set in a failure path: a tab closed while a save was in
// flight leaves no failure to catch, and a comparison notices anyway.
//
// Board-scoped on every call: without the parameter the server answers with —
// and writes to — its default board, so a save made on the second board would
// land on the first and archive everything there.
const API = () => boardUrl('/api/state');
const TRASH = () => boardUrl('/api/trash');
export let serverAvailable = false;
let serverOffline = false; // true once a push has failed, to warn only once
let pushTimer = null;
let sending = null; // the PUT in flight, so two can never overlap
let queued = false; // a change arrived while that one was on the wire

// The version of the board this browser is derived from, as the server named it
// (server.js `revOf`). Sent with every save: matching it is what authorises the
// server to treat an absent card as deleted. It always comes from a server
// response and is deliberately NOT persisted — a rev read from storage could
// claim a board this browser has not actually seen, and a load always fetches
// before it saves anyway. A local edit does not invalidate it: this browser is
// still derived from that server board, and clearing it on every keystroke
// would mean no deletion ever propagated.
let lastRev = '';

// Order-sensitive fingerprint, to skip redundant work when nothing changed.
const boardFingerprint = (cards) =>
  cards.map((c) => [c.id, c.columnId, c.title, c.notes, c.type, c.category || '', c.importance || '', c.urgency || '',
    c.effort || '', c.control || '', c.effortSrc || '', c.controlSrc || '', c.deadline || '',
    // A save that only re-plans a card must not look "already in sync".
    c.plan || '', c.planSrc || '',
    // Habit completions belong here: a board that differs only by a punch is
    // not "already in sync", and skipping the adopt would lose the tick.
    c.habitFreq || '', c.habitCount || 1, (c.habitTimes || []).join('|'),
    JSON.stringify(c.habitHistory || {}),
    c.num, (c.tags || []).join('|')].join('␟')).join('␞');

/** FNV-1a, because the watermark stores a hash of the fingerprint rather than
 *  the fingerprint itself: that string holds every title, note and habit
 *  history on the board, and keeping a second copy of it in localStorage would
 *  roughly double what this browser stores per board for no added certainty. */
function hashFp(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

/** The board as of the last time this browser and the server agreed on it, or
 *  null when they never have. Per board, like every other key here. */
function readWatermark() {
  try {
    const raw = localStorage.getItem(SYNC_KEY + boardSuffix);
    const mark = raw ? JSON.parse(raw) : null;
    return mark && typeof mark.fp === 'string' ? mark : null;
  } catch (_) {
    return null; // private mode
  }
}

/** Record that these exact cards are what the server has. Called only when the
 *  server said so — a 2xx save, or a board adopted from it — and always with
 *  the cards that travelled, never with `state.cards` as it stands afterwards:
 *  an edit made during the round trip has not been acknowledged by anyone. */
function writeWatermark(cards) {
  try {
    localStorage.setItem(SYNC_KEY + boardSuffix,
      JSON.stringify({ fp: hashFp(boardFingerprint(cards)) }));
  } catch (_) { /* private mode */ }
}

/**
 * Take the server's board as this browser's board.
 *
 * The single place that does this, deliberately: `initServerSync`, the two
 * agent paths, and a save the server answered with a merge all converge here,
 * so the rev and the watermark cannot be updated by one path and forgotten by
 * another. Forgetting them is not a visible failure — it leaves the browser
 * claiming a rev the server has moved past, and from then on every deletion it
 * makes is silently refused.
 *
 * Writes localStorage without pushing back (`saveState({ push: false })`): the
 * board just came from the server, and echoing it straight back would be a
 * round trip that can only lose a race.
 *
 * @returns {Array} the cards now on the board, sanitised.
 */
function applyServerBoard(data) {
  const cats = sanitizeCategories(data.categories);
  if (cats) setCategories(cats);
  // ensureNums matters here: a card the server created (an agent edit, or a
  // just-confirmed proposal) arrives with num 0, and without this it would
  // render as C-000 until the next reload.
  const cards = ensureNums(data.cards.map((c) => sanitizeCard(c)).filter(Boolean));
  setCards(cards);
  lastRev = typeof data.rev === 'string' ? data.rev : '';
  saveState({ push: false });
  writeWatermark(cards);
  return cards;
}

export function pushToServer() {
  // Nothing is written to a board we are on our way off: the URL names that
  // board, and by the time a debounced save fires it may have been deleted.
  if (!serverAvailable || leavingBoard) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(sendNow, 150);
}

/** One save at a time. Two overlapping whole-board PUTs are a bug of their own:
 *  the first one's payload still contains the card the second one deleted, and
 *  whichever lands last decides — so a deletion can be undone by a save that
 *  was already on the wire. Later changes coalesce into one follow-up request,
 *  because a whole-board save always describes the board as it is now. */
function sendNow() {
  if (leavingBoard) return Promise.resolve();
  if (sending) { queued = true; return sending; }
  sending = doSend().finally(() => {
    sending = null;
    if (queued) { queued = false; sendNow(); }
  });
  return sending;
}

async function doSend() {
  // The list this request describes. Captured before the await so the watermark
  // records what was actually sent.
  const cards = state.cards;
  try {
    const res = await fetch(API(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      // Always a `rev`, '' included. Omitting the field means "apply the old
      // contract and sweep" server-side, and no client path should be able to
      // ask for that by forgetting to say anything.
      body: JSON.stringify({ version: 1, cards, categories, rev: lastRev }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json().catch(() => null);
    if (data && data.stale && Array.isArray(data.cards)) {
      // The server was ahead of us and merged instead of sweeping. Adopt what
      // it now holds, so the next save is authorised to delete again.
      const merged = applyServerBoard(data);
      if (boardFingerprint(merged) !== boardFingerprint(cards)) {
        setDealCards(false);
        render();
        announce('The board had newer changes — they have been merged in');
      }
    } else {
      if (data && typeof data.rev === 'string') lastRev = data.rev;
      writeWatermark(cards);
    }
    if (serverOffline) { serverOffline = false; announce('Reconnected — changes saved to the server'); }
  } catch (err) {
    if (!serverOffline) {
      serverOffline = true;
      announce('Server unreachable — changes are saved locally for now');
    }
    console.warn('Could not save to server.', err);
  }
}

/** Drop a save that has not gone yet. Called when leaving a board: the pending
 *  push names the board being left, and if that board is the one just deleted
 *  the write arrives as a 400 against something that no longer exists. The
 *  board's own cards are already on the server — this only ever discards a
 *  push that would have been redundant or refused. */
export function cancelPendingPush() {
  clearTimeout(pushTimer);
}

/** Send the pending save now and wait for it. For leaving a board that is NOT
 *  being deleted: a change made inside the 150 ms before the switch would
 *  otherwise be dropped, and the browser would then look — correctly — like it
 *  holds unsynced work, which is what puts it back on the merge path next time.
 *  A no-op once `leavingBoard` is set, which is what makes the delete path
 *  (where it is set first) still discard its save. */
export async function flushPendingPush() {
  clearTimeout(pushTimer);
  if (sending) await sending;
  if (!serverAvailable || leavingBoard) return;
  await sendNow();
}

export async function initServerSync() {
  // Taken before the fetch: the app has already rendered, so a card can be
  // typed while this request is in the air, and adopting the server's board
  // over it would be the very loss this module exists to stop.
  const before = boardFingerprint(state.cards);
  let board;
  try {
    const res = await fetch(API(), { headers: { Accept: 'application/json' } });
    if (!res.ok) return; // no usable backend — stay in localStorage mode
    board = await res.json();
  } catch (_) {
    return; // static / offline — localStorage mode
  }
  if (!board || !Array.isArray(board.cards)) return;
  serverAvailable = true;
  lastRev = typeof board.rev === 'string' ? board.rev : '';

  if (board.cards.length === 0) {
    // A fresh database: this browser's board is all there is.
    if (state.cards.length > 0) pushToServer();
    return;
  }

  const serverCards = ensureNums(board.cards.map((c) => sanitizeCard(c)).filter(Boolean));
  const mark = readWatermark();
  // Does this browser hold anything the server never acknowledged?
  //
  // `loadedFromStorage` is the first term and it is load-bearing: a browser
  // opening for the first time holds the *seed* cards, which are an explanation
  // of the app rather than anyone's work, and merging those into an existing
  // board writes a second copy of all six onto the server. It has nothing to
  // protect, so it adopts.
  //
  // For a browser that does have a board of its own, a missing watermark counts
  // as unsynced — that is what every existing browser looks like the first time
  // it loads after this change, and merging is the safe answer to "I cannot
  // tell". The last term catches a card typed while the fetch was in the air.
  const unsynced = loadedFromStorage
    && (!mark || mark.fp !== hashFp(before) || boardFingerprint(state.cards) !== before);

  if (!unsynced) {
    // In sync, so the database decides — including about cards it no longer
    // has, which is how a deletion made on another machine reaches this one.
    if (boardFingerprint(serverCards) === boardFingerprint(state.cards)) {
      writeWatermark(state.cards); // identical boards; just re-stamp the mark
      return;
    }
    const adopted = applyServerBoard(board);
    recordAdoption(adopted, `Loaded ${adopted.length} card(s) from the server`);
    setDealCards(true);
    render();
    return;
  }

  // Unsynced local work: neither side is authoritative, so nothing is dropped
  // for being absent. The server's Trash is what keeps deletion working — a
  // local-only card the server has already archived was deleted elsewhere.
  let tombstones = new Set();
  try {
    const res = await fetch(TRASH(), { headers: { Accept: 'application/json' } });
    if (res.ok) {
      const trash = await res.json();
      if (trash && Array.isArray(trash.cards)) tombstones = new Set(trash.cards.map((c) => c.id));
    }
  } catch (_) { /* no trash read — the merge is then purely additive */ }

  const serverCats = sanitizeCategories(board.categories);
  if (serverCats) {
    // The registry merges the same way the cards do, and for a sharper reason:
    // the save that follows carries a fresh rev, so the server will replace the
    // registry with whatever this push names. Sending only the local copy would
    // wipe a life area added on the other machine — and `cleanCard` would then
    // blank that category on every card holding it.
    const have = new Set(serverCats.map((c) => c.id));
    setCategories([...serverCats, ...categories.filter((c) => !have.has(c.id))]);
  }

  const merged = mergeCardLists(state.cards, serverCards, tombstones);
  if (boardFingerprint(merged) === boardFingerprint(state.cards)) {
    pushToServer(); // nothing to show; the server still needs our copy
    return;
  }
  setCards(merged);
  recordAdoption(merged, `Merged this browser's board with the server's (${merged.length} card(s))`);
  setDealCards(true);
  saveState(); // localStorage, then the debounced push of the merged board
  render();
}

/** Note a board that arrived from somewhere else in the undo timeline, so the
 *  state before it is one keystroke away.
 *
 *  Only when the outcome actually differs from what this browser had: with the
 *  server as the source of truth this path now runs on ordinary reloads, and an
 *  entry each time would rotate real undo history out of a 50-entry log. */
function recordAdoption(cards, action) {
  timeline.entries.push({ ts: Date.now(), action, cards: snapshot(cards) });
  if (timeline.entries.length > HISTORY_LIMIT) {
    timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
  }
  timeline.index = timeline.entries.length - 1;
  saveTimeline();
}

export async function adoptServerBoard() {
  // The agent mutated the DB through the Node API; adopt the server's board so a
  // debounced local push can't overwrite the agent's change with stale state.
  try {
    const res = await fetch(API());
    if (!res.ok) return;
    const data = await res.json();
    if (data && Array.isArray(data.cards)) applyServerBoard(data);
  } catch { /* offline — keep the local board */ }
}
