import { boardUrl } from './boards.js';
import { ensureNums, sanitizeCard } from './cards.js';
import { categories, sanitizeCategories, setCategories } from './categories.js';
import { saveState, saveTimeline, snapshot, timeline } from './history.js';
import { HISTORY_LIMIT } from './keys.js';
import { loadedFromStorage, setCards, setDealCards, state } from './state.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';

// Server sync — when a backend is reachable the board is persisted to its
// SQLite database; otherwise the app runs entirely on localStorage. The
// whole board is pushed on every change, so deleting a card is the only
// thing that removes its row server-side.

// Board-scoped on every call: without the parameter the server answers with —
// and writes to — its default board, so a save made on the second board would
// land on the first and archive everything there.
const API = () => boardUrl('/api/state');
export let serverAvailable = false;
let serverOffline = false; // true once a push has failed, to warn only once
let pushTimer = null;

// Order-sensitive fingerprint, to skip redundant work when nothing changed.
const boardFingerprint = (cards) =>
  cards.map((c) => [c.id, c.columnId, c.title, c.notes, c.type, c.category || '', c.importance || '', c.urgency || '',
    c.effort || '', c.control || '', c.effortSrc || '', c.controlSrc || '', c.deadline || '',
    // Habit completions belong here: a board that differs only by a punch is
    // not "already in sync", and skipping the adopt would lose the tick.
    c.habitFreq || '', c.habitCount || 1, (c.habitTimes || []).join('|'),
    JSON.stringify(c.habitHistory || {}),
    c.num, (c.tags || []).join('|')].join('␟')).join('␞');

export function pushToServer() {
  if (!serverAvailable) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(async () => {
    try {
      const res = await fetch(API(), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: 1, cards: state.cards, categories }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      if (serverOffline) { serverOffline = false; announce('Reconnected — changes saved to the server'); }
    } catch (err) {
      if (!serverOffline) {
        serverOffline = true;
        announce('Server unreachable — changes are saved locally for now');
      }
      console.warn('Could not save to server.', err);
    }
  }, 150);
}

/** Drop a save that has not gone yet. Called when leaving a board: the pending
 *  push names the board being left, and if that board is the one just deleted
 *  the write arrives as a 400 against something that no longer exists. The
 *  board's own cards are already on the server — this only ever discards a
 *  push that would have been redundant or refused. */
export function cancelPendingPush() {
  clearTimeout(pushTimer);
}

export async function initServerSync() {
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
  const serverCats = sanitizeCategories(board.categories);

  // A browser that already has its own board wins on load — this guarantees
  // unsynced local edits are never clobbered — and we converge the server to
  // it. A fresh browser (no local board) instead loads from the database.
  if (loadedFromStorage && state.cards.length > 0) {
    pushToServer();
    return;
  }

  if (serverCats) setCategories(serverCats); // fresh browser — the DB's registry wins

  if (board.cards.length === 0) {
    if (state.cards.length > 0) pushToServer(); // fresh DB — save our seed board
    return;
  }

  const incoming = ensureNums(board.cards.map((c) => sanitizeCard(c)).filter(Boolean));
  if (boardFingerprint(incoming) === boardFingerprint(state.cards)) return; // already in sync

  // Fresh browser, and the database has a board — adopt it as the source of truth.
  setCards(incoming);
  saveState();
  timeline.entries.push({ ts: Date.now(), action: `Loaded ${incoming.length} card(s) from the server`, cards: snapshot(incoming) });
  if (timeline.entries.length > HISTORY_LIMIT) timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
  timeline.index = timeline.entries.length - 1;
  saveTimeline();
  setDealCards(true);
  render();
}

export async function adoptServerBoard() {
  // The agent mutated the DB through the Node API; adopt the server's board so a
  // debounced local push can't overwrite the agent's change with stale state.
  try {
    const res = await fetch(API());
    if (!res.ok) return;
    const data = await res.json();
    if (data && Array.isArray(data.cards)) {
      const cats = sanitizeCategories(data.categories);
      if (cats) setCategories(cats);
      // ensureNums matters here: a card the server created (an agent edit, or a
      // just-confirmed proposal) arrives with num 0, and without this it would
      // render as C-000 until the next reload.
      state.cards = ensureNums(data.cards);
      saveState();
    }
  } catch { /* offline — keep the local board */ }
}
