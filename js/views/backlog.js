import { filtersActive, matchesFilters } from '../core/cards.js';
import { catColor, catLabel } from '../core/categories.js';
import { cardAria, renderQuickAdd, typeBadge } from '../ui/board.js';
import { sortMenu } from '../ui/card-actions.js';
import { columnCards } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
import { onCardKeydown } from '../ui/keyboard.js';

// Backlog view — the Inbox as a scannable ledger register.
// Backlog view — the Inbox as a scannable ledger register

export function renderBacklog() {
  const sheet = document.createElement('div');
  sheet.className = 'backlog-sheet';

  const visible = columnCards('inbox').filter(matchesFilters);

  const head = document.createElement('div');
  head.className = 'backlog-head';

  const title = document.createElement('h2');
  title.className = 'backlog-title';
  title.textContent = 'Inbox backlog';

  const count = document.createElement('span');
  count.className = 'backlog-count';
  count.textContent = `${visible.length} ${visible.length === 1 ? 'card' : 'cards'}`;

  head.append(title, count);

  if (visible.length > 1) head.append(sortMenu('inbox'));

  sheet.append(head, renderQuickAdd());

  const list = document.createElement('div');
  list.className = 'backlog-list';

  if (visible.length === 0) {
    const hint = document.createElement('div');
    hint.className = 'empty-hint';
    hint.textContent = filtersActive() ? 'No cards match' : 'Write down your first card above';
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

  // No ledger-number cell: the numbers are retired, and a display: none span
  // would still be a grid item's ghost — the columns below assume it is gone.
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

  row.append(badge, main, notes);
  row.addEventListener('click', () => openDialog(card.id));
  row.addEventListener('keydown', (e) => onCardKeydown(e, card.id));
  return row;
}
