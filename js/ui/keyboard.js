import { columnAccepts, matchesFilters, moveCard } from '../core/cards.js';
import { COLUMNS } from '../core/constants.js';
import { setFocusCard } from '../core/state.js';
import { deleteCard } from './card-actions.js';
import { announce, columnCards, columnIndex, getCard } from './dom.js';
import { openDialog } from './edit-dialog.js';

// Keyboard support — moving and reordering cards without a pointer.

export function onCardKeydown(e, cardId) {
  const card = getCard(cardId);
  if (!card) return;

  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    openDialog(cardId);
    return;
  }

  if (e.key === 'Delete' || (e.key === 'Backspace' && e.metaKey)) {
    e.preventDefault();
    deleteCard(cardId);
    return;
  }

  if (e.key === '[' || e.key === ']') {
    e.preventDefault();
    const step = e.key === ']' ? 1 : -1;
    let next = columnIndex(card.columnId) + step;
    // Step over a column this card cannot be in, rather than stopping dead on
    // it — a key that silently does nothing reads as a broken board rather than
    // as a rule. In Progress refusing a habit is the only such rule today and
    // it is no longer reachable from here (a habit is not painted as a board
    // card since 2026-09-07, so this handler never sees one); the skip stays
    // because it is the general answer, and `columnAccepts` is where the rule
    // itself lives.
    if (COLUMNS[next] && !columnAccepts(card, COLUMNS[next].id)) next += step;
    if (next < 0 || next >= COLUMNS.length) return;
    setFocusCard(cardId);
    moveCard(cardId, COLUMNS[next].id);
    announce(`Moved “${card.title}” to ${COLUMNS[next].title}`);
    return;
  }

  if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    e.preventDefault();
    const visible = columnCards(card.columnId).filter(matchesFilters);
    const i = visible.findIndex((c) => c.id === cardId);
    if (i === -1) return;
    let beforeId;
    if (e.key === 'ArrowUp') {
      if (i === 0) return;
      beforeId = visible[i - 1].id;
    } else {
      if (i === visible.length - 1) return;
      beforeId = i + 2 < visible.length ? visible[i + 2].id : null;
    }
    setFocusCard(cardId);
    moveCard(cardId, card.columnId, beforeId);
    announce(`Moved “${card.title}” ${e.key === 'ArrowUp' ? 'up' : 'down'}`);
  }
}
