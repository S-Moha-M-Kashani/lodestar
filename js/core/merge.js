// Reconciling two copies of the same board: the one this browser has been
// looking at, and the one the database holds.
//
// This module imports nothing, and must keep importing nothing — the rule
// js/core/asana.js follows, for the same reason. Every other module under core/
// reaches ui/dom.js sooner or later, and the moment this one does, its unit test
// stops loading under plain node and the only cover left is an e2e.
//
// It exists because of 2026-08-22: a second machine opened this board with a
// days-old copy in its localStorage, the browser pushed that copy as the truth,
// and the whole-board save archived the 24 cards the copy had never heard of.
// The rule that replaces "whoever loads last wins" is: nothing is dropped
// because one side had not heard of it, and where both sides know a card the
// newer edit wins.
//
// Two costs are accepted deliberately, and named here so the next reader does
// not file them as bugs:
//
//   - A reorder is not timestamped. `position` is re-derived from array order
//     on every save and never bumps `updatedAt`, so a merge cannot tell a
//     reordered board from an untouched one: local order leads, and a reorder
//     made on the other machine can be reverted. Card order is cosmetic; a
//     card is not.
//   - A *purged* card can come back. Purge is the one hard delete, so the card
//     is in neither list, and a local copy that still has it looks like an
//     addition. The soft-deleted ones are handled — that is what `tombstones`
//     is — and resurrection is the direction this board errs in on purpose.

/** The newer of two copies of a card. A missing `updatedAt` counts as oldest:
 *  it means "hand-written, imported, or from a version that did not stamp", and
 *  none of those should outrank a copy that can say when it changed. */
const newer = (a, b) => ((b.updatedAt || 0) > (a.updatedAt || 0) ? b : a);

/**
 * Merge the board this browser holds with the board the server sent.
 *
 * @param {Array} local  cards as this browser has them
 * @param {Array} server cards as GET /api/state returned them
 * @param {Set}   tombstones ids the server has in its Trash — a local-only card
 *   whose id is in here was deleted somewhere else, and keeping it would mean
 *   two machines can never delete anything. Defaults to empty, which makes the
 *   merge purely additive.
 * @returns {Array} local order first, then the cards only the server had, in
 *   its own order.
 */
export function mergeCardLists(local, server, tombstones = new Set()) {
  const fromServer = new Map(server.map((c) => [c.id, c]));
  const seen = new Set();
  const out = [];

  for (const card of local) {
    seen.add(card.id);
    const theirs = fromServer.get(card.id);
    if (theirs) { out.push(newer(card, theirs)); continue; }
    if (!tombstones.has(card.id)) out.push(card);
  }
  for (const card of server) {
    if (!seen.has(card.id)) out.push(card);
  }
  return out;
}
