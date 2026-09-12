import { catColor, catLabel } from '../core/categories.js';
import { direction } from '../core/direction.js';
import { habitDoneIn, habitExpectedBy, habitPeriod } from '../core/habits.js';
import { state, view } from '../core/state.js';
import { $ } from './dom.js';
import { openDialog } from './edit-dialog.js';

// The board's summary line: one line at the head of the Board saying what the
// board is asking for, and nothing else.
//
// `js/core/direction.js` has ranked the board by five rules in a fixed order
// since 2026-09-10 — overdue, a habit behind its cadence, due soon, a hand-set
// plan naming now, a life area nothing has moved in — and wrote the reason in
// words. It ran in one place only: inside the Assistant widget, behind a pill
// in the corner. So a board that already knew what mattered made the reader
// scan three columns to find out, and the filters above them were furniture
// bought to make that scan survivable.
//
// **This module paints; it does not decide and it does not re-word.** Every
// rule, figure and sentence is the leaf's, which imports nothing and is pinned
// by tests/direction.test.js under plain node — the house pattern
// `cadenceText`, `planWords` and `rangeText` already follow.

/** How many repetitions this habit has recorded in the period `at` falls in,
 *  and how many the clock has already asked for. `core/habits.js`'s own
 *  answers, so the rail's punch strip, the widget and this line can never
 *  disagree about whether a habit is behind.
 *
 *  Composed here and exported rather than imported by the leaf: `core/habits.js`
 *  reaches `core/state.js`, and one such import inside `core/direction.js`
 *  would make the whole analysis an e2e-only surface. Same trade
 *  `js/core/importplan.js` makes with `newId`. The Assistant's own renderer
 *  takes it from here, so the two surfaces cannot drift into two answers. */
export const habitBehind = (card, at) => ({
  done: habitDoneIn(card, habitPeriod(card.habitFreq, at)),
  expected: habitExpectedBy(card, at),
});

/** One item: the ledger number, then the one phrase that surfaced the card.
 *
 *  A button, because a reason nobody can act on from where they are reading it
 *  is a notice rather than direction — it opens the card it names, which is the
 *  same thing clicking the card itself does. */
function directionItem(item) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'board-direction-item';
  btn.dataset.rule = item.rule;

  const card = state.cards.find((c) => c.id === item.cardId);
  if (card?.category) btn.style.setProperty('--cat', catColor(card.category));

  const num = document.createElement('span');
  num.className = 'board-direction-num';
  num.textContent = item.label;

  const reason = document.createElement('span');
  reason.className = 'board-direction-reason';
  // A substitution, never a rewrite: the neglected-area rule names the area by
  // its id (`item.area`) because it is not handed the registry, so the one
  // thing to do here is put the registry's own label in its place.
  const label = item.area ? catLabel(item.area) : '';
  reason.textContent = label && label !== item.area
    ? item.reason.replace(item.area, label) : item.reason;

  btn.append(num, reason);
  // The card's own title is the accessible name, since `C-042 overdue by 2
  // days` says which row of a ledger to open and not which piece of work.
  btn.title = card ? card.title : '';
  btn.setAttribute('aria-label', card ? `${item.label} ${card.title} — ${reason.textContent}` : reason.textContent);
  btn.addEventListener('click', () => openDialog(item.cardId));
  return btn;
}

/** The line, painted into its own static slot above `#board`.
 *
 *  Its own slot and not a child of `#board`: the board is a grid of three card
 *  columns plus the rail, and a full-width line inside it would have to be
 *  placed on a row of its own in a template that four rules already tune.
 *  Outside it, the line is one more flex child of the board-view body — which
 *  is exactly how `#habit-banner-slot` above it already works.
 *
 *  The Board only. Every other view is a different question being asked of the
 *  same cards, and the Backlog answering "what should I do next" above a list
 *  the reader opened to browse is the widget's old mistake moved one screen
 *  along. */
export function renderDirectionLine() {
  const slot = $('#direction-slot');
  if (!slot) return;
  slot.innerHTML = '';
  if (view !== 'board') return;

  const { items, note } = direction(state.cards, { habitBehind });

  const line = document.createElement('div');
  line.className = 'board-direction';
  // A group with a name, because the items are buttons whose text is a reason
  // and not a command: without it a screen reader meets four buttons with no
  // account of what they have in common.
  line.setAttribute('role', 'group');
  line.setAttribute('aria-label', 'What the board says to do next');

  for (const item of items) line.append(directionItem(item));

  // Exactly one of the two is ever painted: `note` is '' unless no rule found
  // anything, and then it is the whole answer. Read off the same call, so the
  // line cannot both name something and say there is nothing. A board with
  // nothing overdue is a fact worth one line of paper — the alternative is a
  // line that disappears on the good days, which reads as a bug.
  if (note) {
    const empty = document.createElement('p');
    empty.className = 'board-direction-note';
    empty.textContent = note;
    line.append(empty);
  }

  slot.append(line);
}
