import { cardLabel, columnFilterOn, filtersActive, matchesFilters } from '../core/cards.js';
import { catColor, catLabel } from '../core/categories.js';
import { filters, state } from '../core/state.js';
import { cardAria, renderCaptureRow, typeBadge } from '../ui/board.js';
import { sortMenu } from '../ui/card-actions.js';
import { wireCardContext } from '../ui/card-menu.js';
import { columnCards, columnTitle } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
import { onCardKeydown } from '../ui/keyboard.js';

// Backlog view — the Inbox as a scannable ledger register.
// Backlog view — the Inbox as a scannable ledger register

/** Which column this register is reading out. The Inbox by default — a backlog
 *  is what has not been started — and whatever the column filter names when one
 *  is set, so that filter narrows this view rather than emptying it. `null` is
 *  "All columns", the one case with no single column to name or to sort. */
function backlogColumn() {
  if (!columnFilterOn() || !filters.column) return 'inbox';
  return filters.column === 'all' ? null : filters.column;
}

export function renderBacklog() {
  const sheet = document.createElement('div');
  sheet.className = 'backlog-sheet';

  const col = backlogColumn();
  const visible = (col ? columnCards(col) : state.cards).filter(matchesFilters);

  const head = document.createElement('div');
  head.className = 'backlog-head';

  const title = document.createElement('h2');
  title.className = 'backlog-title';
  title.textContent = col ? `${columnTitle(col)} backlog` : 'Every card';

  const count = document.createElement('span');
  count.className = 'backlog-count';
  count.textContent = `${visible.length} ${visible.length === 1 ? 'card' : 'cards'}`;

  head.append(title, count);

  // Sorting reorders one column's cards, so it is offered only when this
  // register is showing one.
  if (col && visible.length > 1) head.append(sortMenu(col));

  // The same create control the Board shows, not a second one: `#new-card-btn`
  // is an id, and only one view renders into #board at a time.
  sheet.append(head, renderCaptureRow());

  const list = document.createElement('div');
  list.className = 'backlog-list';

  if (visible.length === 0) {
    const hint = document.createElement('div');
    hint.className = 'empty-hint';
    hint.textContent = filtersActive() ? 'No cards match' : 'Start your first card above';
    list.append(hint);
  } else {
    for (const card of visible) list.append(renderBacklogRow(card));
  }

  sheet.append(list);
  return sheet;
}

function renderBacklogRow(card) {
  const row = document.createElement('article');
  row.className = 'backlog-row';
  row.dataset.id = card.id;
  row.tabIndex = 0;
  row.style.setProperty('--cat', catColor(card.category));
  if (card.category) row.classList.add('categorized');
  row.setAttribute('aria-label', cardAria(card));

  // The ledger-number cell sits first; a display: none span would still be a
  // grid item's ghost, so it belongs in the DOM to keep the columns aligned.
  const rowNum = document.createElement('span');
  rowNum.className = 'row-num';
  rowNum.textContent = cardLabel(card);

  const badge = typeBadge(card);

  const main = document.createElement('div');
  main.className = 'row-main';

  const title = document.createElement('p');
  title.className = 'row-title';
  title.textContent = card.title;
  main.append(title);

  if (card.category || card.tags.length) {
    const tags = document.createElement('div');
    tags.className = 'card-tags';
    if (card.category) {
      const cat = document.createElement('span');
      cat.className = 'card-cat';
      cat.textContent = catLabel(card.category);
      tags.append(cat);
    }
    for (const t of card.tags) {
      const chip = document.createElement('span');
      chip.className = 'card-tag';
      chip.textContent = t;
      tags.append(chip);
    }
    main.append(tags);
  }

  const notes = document.createElement('span');
  notes.className = 'row-notes';
  if (card.notes.trim()) {
    notes.textContent = '¶';
    notes.title = 'Has notes';
  }

  row.append(rowNum, badge, main, notes);
  row.addEventListener('click', () => openDialog(card.id));
  row.addEventListener('keydown', (e) => onCardKeydown(e, card.id));
  wireCardContext(row, card.id);
  return row;
}
