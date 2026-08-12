import { COLUMNS } from '../core/constants.js';
import { state } from '../core/state.js';

// The small helpers every module reaches for: element lookup, the board's own
// queries, and the live region that announces a change to a screen reader.

export const $ = (sel, root = document) => root.querySelector(sel);

export const getCard = (id) => state.cards.find((c) => c.id === id);
export const columnCards = (columnId) => state.cards.filter((c) => c.columnId === columnId);
export const columnIndex = (columnId) => COLUMNS.findIndex((c) => c.id === columnId);
export const columnTitle = (columnId) => COLUMNS[columnIndex(columnId)].title;

export function announce(message) {
  $('#live-region').textContent = message;
}

/**
 * In-app replacement for confirm()/alert(). Native dialogs are silently
 * blocked in sandboxed embeds (e.g. artifact viewers), so all confirmations
 * go through this <dialog> instead. Pass cancelLabel: null for an alert.
 */
