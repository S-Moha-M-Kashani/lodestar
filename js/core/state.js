import { parseState, seedCards } from './cards.js';
import { setCategories } from './categories.js';
import { COLUMNS, VIEWS } from './constants.js';
import { STORAGE_KEY, VIEW_KEY } from './keys.js';

// The board's shared mutable state — the one module every other one reads.
//
// `state` and the four interaction flags below are exported as live bindings:
// readers import them by name and always see the current value. Only this
// module may *assign* them (an imported binding is read-only everywhere else),
// so every replacement goes through one of the setters at the foot of the file.
// That is the whole discipline — a grep for `setCards(` finds every place the
// board is swapped out, which the old single-closure file could not tell you.

export let loadedFromStorage = false; // true when this browser already had a saved board

function loadState() {
  try {
    const json = localStorage.getItem(STORAGE_KEY);
    if (json) {
      const saved = parseState(json);
      if (saved.categories) setCategories(saved.categories);
      loadedFromStorage = true;
      return saved;
    }
  } catch (err) {
    console.warn('Could not load saved board, starting fresh.', err);
  }
  return { version: 1, columns: COLUMNS, cards: seedCards() };
}

export let state = loadState();
export const filters = { search: '', type: '', category: '', prio: '', tags: new Set() };
export let focusCardId = null; // restore focus after re-render (keyboard moves)
export let draggedId = null;
export let dealCards = true; // deal-in animation runs on first render only

export let view = 'board';
try {
  const v = localStorage.getItem(VIEW_KEY);
  if (VIEWS.includes(v)) view = v;
} catch (_) { /* private mode */ }

export const nextNum = () => state.cards.reduce((m, c) => Math.max(m, c.num || 0), 0) + 1;

// --------------------------------------------------------------------------
// The setters. `version` and `columns` are fixed, so swapping the board is
// only ever swapping its cards — the three callers that used to rebuild the
// whole object by hand (a server adopt, a history restore, an import) all said
// the same thing three times, and now say it once.
// --------------------------------------------------------------------------

export function setCards(cards) {
  state = { version: 1, columns: COLUMNS, cards };
}

/** Which view is on screen. The *switch* (with its focus and repaint work)
 *  is setView in ui/render.js; this only records where we are. */
export function setCurrentView(next) {
  view = next;
}

export function setFocusCard(id) {
  focusCardId = id;
}

export function setDraggedId(id) {
  draggedId = id;
}

export function setDealCards(on) {
  dealCards = on;
}
