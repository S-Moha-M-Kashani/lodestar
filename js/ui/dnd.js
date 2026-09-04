import { columnAccepts, moveCard } from '../core/cards.js';
import { draggedId } from '../core/state.js';
import { announce, columnTitle, getCard } from './dom.js';

// Drag & drop — moving a card between columns with the pointer.
//
// A zone that will not take the card being dragged never lights up and never
// accepts the drop: In Progress takes no habit, and a card that disappeared
// into a column it is not painted in would read as data loss rather than as a
// refusal. Not preventing the dragover default is what makes the browser paint
// "no drop" and skip the drop event.

const refuses = (cardsEl) => {
  const card = getCard(draggedId);
  return Boolean(card) && !columnAccepts(card, cardsEl.dataset.col);
};

const dropIndicator = document.createElement('div');
dropIndicator.className = 'drop-indicator';

export const clearDropIndicator = () => dropIndicator.remove();

function getCardAfterPointer(container, y) {
  const cards = [...container.querySelectorAll('.card:not(.dragging)')];
  let closest = { offset: -Infinity, el: null };
  for (const el of cards) {
    const box = el.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) closest = { offset, el };
  }
  return closest.el;
}

export function wireDropZone(cardsEl) {
  cardsEl.addEventListener('dragover', (e) => {
    if (!draggedId || refuses(cardsEl)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    cardsEl.classList.add('drop-target');
    const after = getCardAfterPointer(cardsEl, e.clientY);
    if (after) cardsEl.insertBefore(dropIndicator, after);
    else cardsEl.append(dropIndicator);
  });

  cardsEl.addEventListener('dragleave', (e) => {
    if (!cardsEl.contains(e.relatedTarget)) {
      cardsEl.classList.remove('drop-target');
      if (dropIndicator.parentElement === cardsEl) clearDropIndicator();
    }
  });

  cardsEl.addEventListener('drop', (e) => {
    e.preventDefault();
    const id = draggedId || e.dataTransfer.getData('text/plain');
    if (!id || refuses(cardsEl)) return;
    const after = getCardAfterPointer(cardsEl, e.clientY);
    clearDropIndicator();
    const card = getCard(id);
    moveCard(id, cardsEl.dataset.col, after ? after.dataset.id : null);
    if (card) announce(`Moved “${card.title}” to ${columnTitle(cardsEl.dataset.col)}`);
  });
}
