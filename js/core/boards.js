import { KEY_PREFIX } from './keys.js';

// Which board is on screen, and the calls that manage the set of them.
//
// This module imports one constant and nothing else — deliberately. Everything
// under core/ reaches ui/ sooner or later, and the storage keys in state.js and
// history.js are composed from `boardSuffix` while those modules are still
// evaluating. A leaf cannot be half-initialised when someone reads it.

const BOARD_KEY = KEY_PREFIX + 'board';
const API = '/api/boards';

// The board a database has always had. Its storage keys are the unsuffixed
// ones, so a browser that used this app before boards existed opens on its own
// board with its own undo history rather than on an empty one.
export const DEFAULT_BOARD_ID = 'main';

const suffixFor = (id) => (id === DEFAULT_BOARD_ID ? '' : ':' + id);

function stored() {
  try {
    return localStorage.getItem(BOARD_KEY) || DEFAULT_BOARD_ID;
  } catch (_) {
    return DEFAULT_BOARD_ID; // private mode
  }
}

export let activeBoardId = stored();
export let boardSuffix = suffixFor(activeBoardId);
export let boards = []; // the live boards, as the server last listed them

/** Add `?board=` to an API path. Every board-scoped route takes it; the ones
 *  addressed by a card, chat or message id do not need it, because those ids
 *  are unique across boards and the row itself knows where it belongs. */
export const boardUrl = (path) =>
  path + (path.includes('?') ? '&' : '?') + 'board=' + encodeURIComponent(activeBoardId);

/** Load the picker's contents. Returns false when there is no backend — the
 *  app still runs entirely on localStorage there, so the picker hides rather
 *  than the boot failing. */
export async function loadBoards() {
  try {
    const res = await fetch(API, { headers: { Accept: 'application/json' } });
    if (!res.ok) return false;
    const data = await res.json();
    if (!data || !Array.isArray(data.boards) || data.boards.length === 0) return false;
    boards = data.boards;
    // The stored board can be one that has since been deleted or purged — from
    // another browser, or from this one before a reload. Falling back keeps the
    // app usable; reloading is what makes it read the right board's cards,
    // since state.js took its localStorage key from the old id on the way in.
    if (!boards.some((b) => b.id === activeBoardId)) {
      setActiveBoard(data.defaultId || boards[0].id);
      location.reload();
      return false;
    }
    return true;
  } catch (_) {
    return false; // static / offline
  }
}

/** Record which board we are on. Does not repaint anything: the only caller
 *  that changes boards mid-session is openBoard, which reloads. */
export function setActiveBoard(id) {
  activeBoardId = id;
  boardSuffix = suffixFor(id);
  try { localStorage.setItem(BOARD_KEY, id); } catch (_) { /* private mode */ }
}

export function setBoards(list) {
  boards = list;
}

export const activeBoard = () => boards.find((b) => b.id === activeBoardId) || null;

/**
 * Switch boards by reloading the page.
 *
 * The alternative is re-initialising the cards, the undo timeline, the filters,
 * the Review state, the proposal and suggestion lists, and the Assistant's
 * sheet with its three panels — in the right order, and never missing one as
 * the app grows. A reload is a hundred milliseconds against a local server and
 * it makes a board's state structurally unable to leak into the next one, which
 * is the whole promise of the feature.
 */
export function openBoard(id) {
  if (id === activeBoardId) return;
  setActiveBoard(id);
  location.reload();
}

const send = async (path, options) => {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
  return body;
};

const asJson = (payload) => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});

export const createBoard = (name) => send(API, asJson({ name })).then((b) => b.board);

export const renameBoard = (id, name) =>
  send(`${API}/${encodeURIComponent(id)}`,
    { ...asJson({ name }), method: 'PATCH' }).then((b) => b.board);

export const deleteBoard = (id) =>
  send(`${API}/${encodeURIComponent(id)}`, { method: 'DELETE' });

export const fetchDeletedBoards = () =>
  send(`${API}/trash`).then((b) => b.boards);

export const restoreBoard = (id) =>
  send(`${API}/trash/${encodeURIComponent(id)}/restore`, { method: 'POST' }).then((b) => b.board);

export const purgeBoard = (id) =>
  send(`${API}/trash/${encodeURIComponent(id)}`, { method: 'DELETE' });
