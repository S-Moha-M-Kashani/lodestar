// Which cards on this board are the same thought twice, and which copy of one
// survives the clean-up. Decided in full before anything is written.
//
// Two copies of a card are two places one thought can drift apart, and the
// board had no way to find them: the cure was reading every card and dragging
// the losers to the Trash one at a time. They arrive through `Add to the
// board`, which added everything a file held until the import reconciliation
// of 2026-09-08 learned to skip a card the board already has — so every board
// imported before that date is carrying whatever it was handed twice.
//
// **The identity rule is `cardWords`, reused and not restated.** Title and
// notes, trimmed and lower-cased, is already how the import review decides
// whether the board holds a thought; a second rule for "the same thought"
// would be two hand-kept lists that can disagree, which this codebase has
// already been bitten by twice (`EDITABLE_FIELDS`, `writeProposal`'s INSERT).
// Nothing else makes two cards duplicates and nothing else keeps two cards
// with the same words from being one.
//
// **The survivor is the copy furthest along the flow** — Done, then In
// Progress, then Inbox. "Always the newer" was the first reading of the
// request, and measured on the owner's own board it would have reopened
// `C-009`: a card in In Progress whose Inbox twin was the newer of the two.
// The copy that has been worked on is the one holding real state, and a
// clean-up that quietly undoes progress is worse than the duplicate it
// removed. "Always the older" loses the other way: the older copy is usually
// the one an import already superseded, and `doneAt` follows the column, so
// keeping the finished copy keeps the day it was finished.
//
// **`createdAt` alone is not a total order on this board.** The two real
// pairs found here share a `createdAt` to the millisecond, because the import
// that made them copied it across — so the ledger number is the final
// tie-break. A rule that can answer "either" is a rule that deletes a
// different card the next time it runs.
//
// Everything that *changes* the board is the caller's: re-pointing the
// children of a retired card, swapping the array, the one commit. What is
// decided here and nowhere else is what a duplicate is, which copy stays, and
// what that copy inherits — because those are the questions a test has to be
// able to ask without a DOM.
//
// This module imports two leaves and nothing else — the importplan.js,
// merge.js, asana.js, plan.js, nesting.js, schedule.js and timespan.js rule.
// Every other module under core/ reaches ui/dom.js sooner or later, and the
// moment this one does, tests/duplicates.test.js stops loading under plain
// node and the only cover left for the whole decision table is an e2e.
import { COLUMNS } from './constants.js';
import { cardWords } from './importplan.js';

// How far along the flow a column is. COLUMNS is already in that order, so
// "furthest along" is an index in a list the whole board already agrees on.
// A column the board does not know is the start of the flow, never the end:
// an unreadable filing must not be what saves a copy.
const RANK = Object.fromEntries(COLUMNS.map((c, i) => [c.id, i]));
const rankOf = (card) => RANK[card?.columnId] ?? 0;

/** The better copy first: furthest along, then newest, then the higher ledger
 *  number. Cards that tie on all three keep the order the board holds them in,
 *  so the answer is the same every run. */
const betterFirst = (a, b) =>
  rankOf(b) - rankOf(a) || (b.createdAt || 0) - (a.createdAt || 0) || (b.num || 0) - (a.num || 0);

/** Every completion either copy recorded, in one history.
 *
 *  A habit's history is a record of things that actually happened, so the
 *  union is the only honest answer — the mirror of the Duplicate rule, which
 *  deliberately copies none: there the copy is a different habit and here it
 *  is provably the same one. A moment both copies hold is one completion, not
 *  two. Nothing is capped here; sanitizeCard owns the period limit. */
function unionHistory(copies) {
  const out = {};
  for (const c of copies) {
    for (const [period, stamps] of Object.entries(c?.habitHistory || {})) {
      if (!Array.isArray(stamps)) continue;
      const kept = out[period] || (out[period] = []);
      for (const t of stamps) if (!kept.includes(t)) kept.push(t);
    }
  }
  for (const stamps of Object.values(out)) stamps.sort((x, y) => x - y);
  return out;
}

/**
 * Decide which cards are duplicates of one another, without touching any.
 *
 * @param {Array} cards the live cards of the board in front of the owner
 * @returns {Array<{keep: object, drop: Array}>} one entry per set of cards
 *   sharing their words, in the order the sets first appear on the board: the
 *   copy that survives — a new object carrying anything the retired copies
 *   recorded, so the board it read is left exactly as it was — and the copies
 *   to retire. Cards with no twin are not a decision and are absent.
 */
export function planDedupe(cards) {
  const list = Array.isArray(cards) ? cards : [];

  const groups = new Map(); // words -> every card holding them
  for (const c of list) {
    const words = cardWords(c);
    if (!groups.has(words)) groups.set(words, []);
    groups.get(words).push(c);
  }

  const out = [];
  for (const copies of groups.values()) {
    if (copies.length < 2) continue;
    const [keep, ...drop] = [...copies].sort(betterFirst);
    out.push({ keep: { ...keep, habitHistory: unionHistory(copies) }, drop });
  }
  return out;
}
