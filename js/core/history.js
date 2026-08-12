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

export function saveTimeline() {
  try {
    localStorage.setItem(HISTORY_KEY + boardSuffix, JSON.stringify(timeline));
  } catch (err) {
    console.warn('Could not save history.', err);
  }
}

export function saveState() {
  try {
    localStorage.setItem(STORAGE_KEY + boardSuffix, JSON.stringify({ ...state, categories }));
  } catch (err) {
    console.warn('Could not save board.', err);
  }
  pushToServer(); // keep the SQLite-backed server in sync when one is present
}

export function commit(action) {
  saveState();
  timeline.entries.push({ ts: Date.now(), action, cards: snapshot(state.cards) });
  if (timeline.entries.length > HISTORY_LIMIT) {
    timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
  }
  timeline.index = timeline.entries.length - 1;
  saveTimeline();
  render();
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
