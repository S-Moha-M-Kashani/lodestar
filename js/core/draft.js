// A draft: a card-shaped object the dialog holds before any card exists.
//
// The card dialog used to open only on a card already on the board. It now
// also opens on one of these — blank from the board's create control, or
// prefilled from an existing card by Duplicate — and nothing reaches
// state.cards until the user saves. That is why the two factories here leave
// `id`, `num`, `createdAt` and `updatedAt` to the caller: a cancelled capture
// must burn no permanent ledger number, and a draft holding the source card's
// id would overwrite that card on save instead of joining it.
//
// This module imports nothing, the js/core/plan.js and js/core/merge.js rule.
// Every other core module reaches ui/dom.js sooner or later; staying importless
// is what lets tests/draft.test.js load it under plain node with no DOM, and
// the field rules below are the part of this feature most worth testing that
// way. One import of a ui/ module silently makes it an e2e-only surface.

/** The fields a card carries beyond its identity, in the order sanitizeCard
 *  writes them. `habitHistory` is deliberately absent: see draftFrom. */
const CARRIED = [
  'columnId', 'title', 'notes', 'type', 'category', 'importance', 'urgency',
  'effort', 'control', 'effortSrc', 'controlSrc', 'deadline', 'plan',
  'planSrc', 'habitFreq', 'habitCount',
];

/** An empty draft, at the standing defaults — the literal the Inbox's
 *  quick-add form used to build inline. `type` and `category` come from the
 *  board's active filters, so a card captured inside an open drawer belongs to
 *  it; a habit gets the cadence the dialog defaults to rather than a habit
 *  with no cadence at all. */
export const blankDraft = ({ type, category } = {}) => ({
  columnId: 'inbox',
  title: '',
  notes: '',
  // `||`, not a default parameter: the board's filters are '' when nothing is
  // filtered, and a default parameter only fires on `undefined` — so a default
  // parameter here hands back type: '' and a picker with no stamp selected.
  type: type || 'question',
  category: category || '',
  importance: '',
  urgency: '',
  effort: 'medium',
  control: 'influence',
  effortSrc: 'default',
  controlSrc: 'default',
  deadline: '',
  plan: '',
  planSrc: 'auto',
  habitFreq: type === 'habit' ? 'daily' : '',
  habitCount: 1,
  habitTimes: [],
  habitHistory: {},
  tags: [],
});

/** A draft prefilled from an existing card.
 *
 *  Cadence travels (habitFreq/habitCount/habitTimes) because that is the
 *  habit's design; `habitHistory` does not, because that is its record. A copy
 *  inheriting a year of completions is a record of things that did not happen
 *  — the same reason the agent has no tool to tick a habit. The two arrays are
 *  copied rather than shared, or editing the draft edits the card it came
 *  from, which is a mutation nobody asked for and nothing would undo. */
export function draftFrom(card) {
  const draft = { habitTimes: [...card.habitTimes], habitHistory: {}, tags: [...card.tags] };
  for (const field of CARRIED) draft[field] = card[field];
  return draft;
}

/** Does this card carry a setting somebody actually chose?
 *
 *  The card dialog folds everything but the card, its notes and its type into
 *  one block, and opens that block when the answer here is yes — so nothing
 *  already set is hidden behind a fold the user did not know to open.
 *
 *  Two fields lie if you read their value. **Effort and control always hold
 *  one**: effortVal returns 'medium' and controlVal 'influence' for anything
 *  unset, so testing the value would open the fold on every card ever made.
 *  Only the provenance says a person or the assistant decided. And **an auto
 *  plan has been resolved to the deadline** by sanitizeCard, so every dated
 *  card carries a plan it was never asked about; there the deadline is the
 *  detail and the plan is it restated. */
export const cardHasDetails = (card) => Boolean(
  card.category
  || card.importance
  || card.urgency
  || card.deadline
  || card.tags?.length
  || (card.planSrc && card.planSrc !== 'auto')
  || (card.effortSrc && card.effortSrc !== 'default')
  || (card.controlSrc && card.controlSrc !== 'default'),
);
