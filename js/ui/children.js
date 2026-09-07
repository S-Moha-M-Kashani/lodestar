import { cardLabel } from '../core/cards.js';
import { catColor } from '../core/categories.js';
import { childrenOf } from '../core/nesting.js';
import { state } from '../core/state.js';
import { wireCardContext } from './card-menu.js';
import { columnTitle } from './dom.js';
import { openDialog } from './edit-dialog.js';

// The children block: the smaller cards one card is made of, listed at its
// foot behind a fold that is closed by default.
//
// One renderer for both places a parent is shown — the board card and the card
// dialog — because they are the same list answering the same question, and two
// copies would drift on the day one of them gained a column or a strike. The
// rows are ledger lines rather than cards: the number in mono, a category tick
// in the card's own hue, the title in body text, the column at the right in
// soft ink.
//
// A finished child is struck through rather than tinted. Colour on this board
// means category and nothing else, so "done" cannot be a colour — the same rule
// that keeps the type stamps neutral and draws the dialog's column chips in
// plain ink.

/** The fold's own label, so it can be read without being opened. `4 · 1 done`,
 *  or nothing at all when the card has no children — a count of zero is a fact
 *  nobody needs printed. */
export function childrenSummary(cards) {
  if (!cards.length) return '';
  const done = cards.filter((c) => c.columnId === 'answered').length;
  return done ? `${cards.length} · ${done} done` : String(cards.length);
}

/** One child, as a row. A button, because activating it opens that child; and
 *  `wireCardContext`, so it answers the same right-click as every other place
 *  a card is listed. */
export function childRow(child) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'child-row' + (child.columnId === 'answered' ? ' done' : '');
  row.dataset.id = child.id;
  row.style.setProperty('--cat', catColor(child.category));
  if (child.category) row.classList.add('categorized');

  const num = document.createElement('span');
  num.className = 'child-num';
  num.textContent = cardLabel(child);

  const title = document.createElement('span');
  title.className = 'child-title';
  title.textContent = child.title;

  const col = document.createElement('span');
  col.className = 'child-col';
  col.textContent = columnTitle(child.columnId);

  row.append(num, title, col);
  row.setAttribute('aria-label',
    `${cardLabel(child)}: ${child.title} — in ${columnTitle(child.columnId)}. Open this card.`);
  row.addEventListener('click', (e) => {
    e.stopPropagation(); // the board card underneath opens its own dialog
    openDialog(child.id);
  });
  wireCardContext(row, child.id);
  return row;
}

// Which parents' folds are open, by card id. A board card is rebuilt by every
// repaint — a habit punched, a letter typed into the search box, a save landing
// — so a `<details>` on its own forgets on each one, and a fold that shuts
// while it is being read is worse than one that has to be opened. This is the
// `openTapes` set the habit card used to carry, for the same reason and in the
// same shape; the dialog's own children block is a different element and stays
// shut on every open, which is what the spec asks of it.
const openFolds = new Set();

/** The whole fold for a board card, or null when the card has no children —
 *  a childless card shows no block at all and is no taller than before.
 *  Closed until somebody opens it. */
export function childrenFold(card) {
  const kids = childrenOf(state.cards, card.id);
  if (!kids.length) return null;

  const fold = document.createElement('details');
  fold.className = 'card-children card-children-inline';
  fold.open = openFolds.has(card.id);
  fold.addEventListener('toggle', () => {
    if (fold.open) openFolds.add(card.id);
    else openFolds.delete(card.id);
  });

  const summary = document.createElement('summary');
  summary.className = 'child-fold-summary';
  summary.textContent = 'Children';
  const count = document.createElement('span');
  count.className = 'hint';
  count.textContent = childrenSummary(kids);
  summary.append(' ', count);
  fold.append(summary);

  for (const kid of kids) fold.append(childRow(kid));
  return fold;
}
