import { cardLabel, ensureNums, sanitizeCard } from './cards.js';
import { COLUMNS } from './constants.js';
import { commit, saveTimeline, short, timeline } from './history.js';
import { state } from './state.js';
import { serverAvailable } from './sync.js';
import { ask } from '../ui/dialogs.js';
import { announce, getCard } from '../ui/dom.js';

// Trash — deleting a card from the board only hides it; the server keeps
// the row (soft delete) so it stays recoverable even if this browser's local
// history is cleared. Only an explicit "Delete permanently" purges it for
// good. The Trash is server-backed, so it only appears when a backend is
// running (localStorage-only mode relies on the History timeline instead).

const TRASH_API = '/api/trash';

export async function fetchTrash() {
  if (!serverAvailable) return [];
  try {
    const res = await fetch(TRASH_API, { headers: { Accept: 'application/json' } });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data.cards) ? data.cards.map((c) => sanitizeCard(c)).filter(Boolean) : [];
  } catch (_) {
    return [];
  }
}

export function restoreFromTrash(card) {
  if (getCard(card.id)) return; // already back on the board
  const revived = { ...card, columnId: COLUMNS.some((c) => c.id === card.columnId) ? card.columnId : 'inbox' };
  state.cards = [...state.cards, revived];
  ensureNums(state.cards);
  commit(`Restored ${cardLabel(revived)} “${short(revived.title)}”`); // re-adds the row server-side (clears deleted_at)
  announce(`Restored “${revived.title}”`);
}

export async function purgeFromTrash(card) {
  const sure = await ask({
    title: 'Delete permanently?',
    message: `${cardLabel(card)} “${card.title}” will be erased from the database for good. This is the only action that truly deletes it, and it cannot be undone.`,
    okLabel: 'Delete permanently',
    danger: true,
  });
  if (!sure) return false;
  try {
    const res = await fetch(`/api/cards/${encodeURIComponent(card.id)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
  } catch (err) {
    announce('Could not delete permanently — the server was unreachable');
    console.warn('Purge failed.', err);
    return false;
  }
  scrubFromTimeline(card.id);
  announce(`Permanently deleted “${card.title}”`);
  return true;
}

// Once a card is purged, drop it from every local history snapshot too, so
// time-travelling back through History can't resurrect what was deleted for good.
function scrubFromTimeline(id) {
  let changed = false;
  for (const entry of timeline.entries) {
    const before = entry.cards.length;
    entry.cards = entry.cards.filter((c) => c.id !== id);
    if (entry.cards.length !== before) changed = true;
  }
  if (changed) saveTimeline();
}
