import { cardLabel } from '../core/cards.js';
import { TYPE_RANK, priorityOf } from '../core/constants.js';
import { commit, short } from '../core/history.js';
import { state } from '../core/state.js';
import { ask } from './dialogs.js';
import { announce, columnCards, columnTitle, getCard } from './dom.js';

// Card actions — deleting a card, and sorting a column.

export async function deleteCard(cardId) {
  const card = getCard(cardId);
  if (!card) return;
  const sure = await ask({
    title: 'Delete this card?',
    message: `${cardLabel(card)} “${card.title}” will be moved off the board. It stays recoverable — bring it back with Undo, or from the History panel — until you delete it permanently there.`,
    okLabel: 'Delete card',
    danger: true,
  });
  if (!sure) return;
  state.cards = state.cards.filter((c) => c.id !== cardId);
  commit(`Deleted ${cardLabel(card)} “${short(card.title)}”`);
  announce(`Deleted “${card.title}”`);
}

// The sort menu's orders. Deadline: earliest first, undated at the back
// (ISO dates compare correctly as strings; '' would sort first, so undated
// cards get a sentinel past every real date). Priority: P1 → P4, unlabelled
// last. Array.sort is stable, so ties keep their existing order.
const SORTERS = {
  deadline: { label: 'By deadline', cmp: (a, b) => (a.deadline || '9999-12-31').localeCompare(b.deadline || '9999-12-31') },
  priority: { label: 'By priority', cmp: (a, b) => (priorityOf(a) || 5) - (priorityOf(b) || 5) },
  type:     { label: 'By type',     cmp: (a, b) => TYPE_RANK[a.type] - TYPE_RANK[b.type] },
  newest:   { label: 'Newest first', cmp: (a, b) => b.createdAt - a.createdAt },
  oldest:   { label: 'Oldest first', cmp: (a, b) => a.createdAt - b.createdAt },
};

function sortColumn(columnId, key) {
  const sorter = SORTERS[key];
  if (!sorter) return;
  const sorted = columnCards(columnId).sort(sorter.cmp);
  state.cards = [...state.cards.filter((c) => c.columnId !== columnId), ...sorted];
  commit(`Sorted ${columnTitle(columnId)} ${sorter.label.toLowerCase()}`);
  announce(`Sorted ${columnTitle(columnId)} ${sorter.label.toLowerCase()}`);
}

// A command-select: picking an order applies it once and the control snaps
// back to its placeholder — it reads as a menu of actions, not a setting.
export function sortMenu(columnId) {
  const sel = document.createElement('select');
  sel.className = 'sort-select';
  sel.setAttribute('aria-label', `Sort ${columnTitle(columnId)}`);
  sel.title = 'Sort these cards';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Sort ⇅';
  sel.append(placeholder);
  for (const [key, sorter] of Object.entries(SORTERS)) {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = sorter.label;
    sel.append(opt);
  }
  sel.addEventListener('change', () => {
    const key = sel.value;
    sel.value = '';
    sortColumn(columnId, key);
  });
  return sel;
}
