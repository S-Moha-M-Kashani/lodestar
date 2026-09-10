import { direction } from '../core/direction.js';
import { catColor, catLabel } from '../core/categories.js';
import { habitDoneIn, habitExpectedBy, habitPeriod } from '../core/habits.js';
import { state } from '../core/state.js';

// What the chat shows before anybody has said anything: the board's own
// direction.
//
// This is the widget's standing state, and it replaces the eight suggestion
// chips that stood here until 2026-09-10 — measured at 361 px inside a 343 px
// `.chat-log`, i.e. an opener list taller than the box holding it. The chips
// were a list of things to ask a model; this is what the board already says,
// and it is on screen before a single request is made to anybody.
//
// **This module paints; it does not decide and it does not re-word.** Every
// rule, every figure and every sentence is `core/direction.js`'s, which imports
// nothing and is pinned by tests/direction.test.js under plain node — the house
// pattern `cadenceText`, `planWords` and `rangeText` already follow. What is
// left here is three spans per item, which is why there is no test of its own:
// the wording is the leaf's and the surface is the e2e's.
//
// The cadence arithmetic is **passed in** rather than imported by the leaf, so
// this is where the two real modules are composed — `core/habits.js` reaches
// `core/state.js`, and importing it there would make the whole analysis an
// e2e-only surface. Same trade `planImport` makes with `newId`.

/** How many repetitions this habit has recorded in the period `at` falls in,
 *  and how many the clock has already asked for. `core/habits.js`'s own
 *  answers, so the rail's punch strip and the direction can never disagree
 *  about whether a habit is behind. */
const habitBehind = (card, at) => ({
  done: habitDoneIn(card, habitPeriod(card.habitFreq, at)),
  expected: habitExpectedBy(card, at),
});

/** One item as a ledger line: the number in mono, a category tick in the
 *  card's own hue, the title in body text, and the reason on the line under
 *  them in soft ink. Deliberately the children block's own row — a direction
 *  item is the same kind of thing, a card named in a list, and the board has
 *  one way of drawing that. */
function directionRow(item) {
  const card = state.cards.find((c) => c.id === item.cardId);

  const row = document.createElement('div');
  row.className = 'chat-direction-item';
  row.dataset.rule = item.rule;
  if (card?.category) {
    row.classList.add('categorized');
    row.style.setProperty('--cat', catColor(card.category));
  }

  const num = document.createElement('span');
  num.className = 'chat-direction-num';
  num.textContent = item.label;

  const title = document.createElement('span');
  title.className = 'chat-direction-title';
  // The ledger number alone is not something anyone can act on from a corner
  // of the screen; the card it names is the point. The leaf hands over the id
  // rather than the words precisely so it needs no board state of its own.
  title.textContent = card ? card.title : '';

  const reason = document.createElement('span');
  reason.className = 'chat-direction-reason';
  // A substitution, never a rewrite: the neglected-area rule names the area by
  // its id (`item.area`) because it is not handed the registry, so the one
  // thing to do here is put the registry's own label in its place.
  const label = item.area ? catLabel(item.area) : '';
  reason.textContent = label && label !== item.area
    ? item.reason.replace(item.area, label) : item.reason;

  row.append(num, title, reason);
  return row;
}

/** The whole block, for an empty chat in either shell.
 *
 *  Both shells, because chrome.js's `shell` argument decides nothing
 *  behavioural and an empty chat asks the same question wherever it is drawn.
 *  Nothing here is conditional on the widget, and nothing here fetches. */
export function renderDirection() {
  const { items, note, praise } = direction(state.cards, { habitBehind });

  const block = document.createElement('div');
  block.className = 'chat-direction';

  // A paragraph and not a `role="group"` with an `aria-label`: the head below
  // is real text, so it is already read. The rails name their groups that way
  // because a rotated glyph is not an accessible name — the opposite case.
  const head = document.createElement('p');
  head.className = 'chat-direction-head';
  head.textContent = 'What the board says to do next';
  block.appendChild(head);

  for (const item of items) block.appendChild(directionRow(item));

  // Exactly one of the two is ever painted: `note` is '' unless no rule found
  // anything, and then it is the whole answer. Read off the same call, so the
  // block cannot both name something and say there is nothing.
  if (note) {
    const empty = document.createElement('p');
    empty.className = 'chat-direction-note';
    empty.textContent = note;
    block.appendChild(empty);
  }

  // The record's own reading of the last week, praise or not. `earned` travels
  // as a data attribute rather than as a colour: a bad week is stated in the
  // same ink as a good one, and nothing downstream can dress one up as the
  // other.
  const record = document.createElement('p');
  record.className = 'chat-direction-praise';
  record.dataset.earned = String(praise.earned);
  record.textContent = praise.text;
  block.appendChild(record);

  return block;
}
