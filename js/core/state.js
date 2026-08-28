import { DEFAULT_BOARD_ID, activeBoardId, boardSuffix } from './boards.js';
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

// True when this browser already had a board of its own saved. It is not the
// same question as "does the board have cards": a browser opening for the first
// time is handed the seed cards, and those are an explanation of the app, not
// work worth defending against the database. js/core/sync.js is the only reader,
// and treating the two alike duplicated the whole seed set onto the server the
// first time a fresh browser opened an existing board.
export let loadedFromStorage = false;

function loadState() {
  try {
    // Per board: the cache is this browser's copy of one board's cards, and a
    // single key would hand the board you left to the board you opened.
    const json = localStorage.getItem(STORAGE_KEY + boardSuffix);
    if (json) {
      const saved = parseState(json);
      if (saved.categories) setCategories(saved.categories);
      loadedFromStorage = true;
      return saved;
    }
  } catch (err) {
    console.warn('Could not load saved board, starting fresh.', err);
  }
  // The seed cards are what an empty *app* opens with — an explanation of the
  // board, written as cards. A board someone just created is not that: they
  // asked for somewhere new to put things, and six examples in it would be six
  // cards to delete. Only the default board is ever seeded.
  const cards = activeBoardId === DEFAULT_BOARD_ID ? seedCards() : [];
  return { version: 1, columns: COLUMNS, cards };
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
