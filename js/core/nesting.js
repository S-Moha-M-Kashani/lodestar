// The lineage: a card can be part of one other card.
//
// The link lives on the **child**, as `parentId`, and the parent carries
// nothing. The board is saved as a whole-board PUT, so any fact stored twice is
// a fact two writes can disagree about: a stale tab holding `childIds: [a, b]`
// would resurrect a link the other machine broke, and `mergeBoard` — which adds
// and updates but never removes — could not tell a dropped child from one it
// had never seen. One writer per fact is the rule `pending` and `board_id`
// already follow. Order comes from `state.cards`, exactly as a card's place in
// a column does, so a `childIds` array would also have been a second answer to
// "which order".
//
// This module imports nothing, the js/core/plan.js, js/core/draft.js and
// js/core/merge.js rule. Every other core module reaches ui/dom.js sooner or
// later, and habits.js reaches core/state.js for habitCards(), so staying
// importless is what lets tests/nesting.test.js load these rules under plain
// node with no DOM. That is also why the habit test below is written out here
// rather than imported from habits.js — one import of that module and this
// suite becomes an e2e-only surface.

/** A habit, read exactly as `isHabit` in js/core/habits.js reads it: the stamp
 *  alone is not enough, a habit needs a cadence. Kept in step by hand for the
 *  importless rule above; both are one expression, so the drift is visible. */
const isHabitCard = (card) => card?.type === 'habit' && Boolean(card.habitFreq);

/** The field validator: an id, or nothing. Shape only — whether that id names
 *  a card the board actually holds is a question about the *list*, which is
 *  what resolveLineage is for. */
export const parentIdVal = (v) => (typeof v === 'string' ? v.trim() : '');

/** A parent's children, in board order.
 *
 *  A blank id answers with nothing rather than with every unparented card: the
 *  children of no card is not the top level. */
export const childrenOf = (cards, id) =>
  (id ? cards.filter((c) => parentIdVal(c.parentId) === id) : []);

// One level deep, read from either end. A card that is part of something else
// cannot itself be broken up, and a card already broken up cannot become a
// part. What would change this: a real board where somebody wants a
// sub-sub-task and works around its absence — the same evidence that justified
// the link over the `Subtask of: …` line asana.js writes into the notes.
// canHaveChildren reads the card alone — being a part is a fact the child
// carries — so it takes no list, where canHaveParent has to go looking.
export const canHaveChildren = (card) =>
  Boolean(card) && !isHabitCard(card) && !parentIdVal(card.parentId);
export const canHaveParent = (card, cards) =>
  Boolean(card) && !isHabitCard(card) && childrenOf(cards, card.id).length === 0;

/** Blank every link the board cannot honour, and keep every card.
 *
 *  The `category` precedent: `cleanCard` blanks an unknown category rather
 *  than dropping the card, because refusing the card would lose real work to a
 *  dangling pointer. A child outlives its parent whenever two machines merge,
 *  half a board is imported, or a parent is purged — so this blanks and never
 *  refuses. Mutates in place and returns the same array, as `ensureNums` does.
 *
 *  Two phases over one snapshot, which is what makes the answer independent of
 *  the order the cards arrive in:
 *
 *    1. Drop the links that are impossible whatever else is on the board — a
 *       parent that is absent, the card itself, or a habit; and any link at all
 *       on a habit.
 *    2. Of what is left, drop a link whose parent *still* holds one. That is
 *       the one-level rule, and judging it on the survivors rather than on the
 *       raw claims is deliberately the generous reading: if the middle card's
 *       own link broke in phase 1 it is not a child any more, so the link
 *       pointing at it is a legitimate one-level link and survives. A cycle has
 *       no such escape — every card in it still holds a link, so every link in
 *       it goes and every card stays. */
export function resolveLineage(cards) {
  const byId = new Map(cards.map((c) => [c.id, c]));

  const phase1 = new Map();
  for (const c of cards) {
    const id = parentIdVal(c.parentId);
    const parent = id && id !== c.id ? byId.get(id) : undefined;
    phase1.set(c.id, parent && !isHabitCard(parent) && !isHabitCard(c) ? id : '');
  }

  for (const c of cards) {
    const id = phase1.get(c.id);
    c.parentId = id && phase1.get(id) ? '' : id;
  }
  return cards;
}
