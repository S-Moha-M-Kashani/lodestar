// What an import file means for the board it lands on, decided in full before
// anything is written.
//
// "Add to the board" used to add whatever the file held. Re-importing
// yesterday's export — or a file the Assistant built from the board it had just
// read — therefore wrote a second copy of every card, and the two copies drifted
// apart under separate ledger numbers, which is the opposite of what an import
// is for.
//
// The rule that replaces "add everything": **a card is its words.** Same title
// and same notes = the same thought, whatever ids either side happens to use.
// From there each incoming card is one of three things:
//
//   - **add** — nothing on the board has those words;
//   - **skip** — something does, and nothing else about the two differs;
//   - **substitute** — something does, but they differ: a property, or which
//     cards hang off it. The file's version is added and the board's copy is
//     retired to the Trash. Not an edit of the old card: a ledger number belongs
//     to a card for life, so the replacement earns one of its own and the old
//     number leaves with the old card, which is what makes the swap legible in
//     History and reversible from the Trash.
//
// **Children are planned before their parents.** That is the order the review
// reads them in, and it is what lets a parent's substitution hand the whole
// family over: by the time the reviewer is asked about the parent, every child
// has already been settled.
//
// **A substitution retires exactly one card.** Children are never deleted by
// one — they move to the card that replaced their parent. So no step of an
// import can lose a card the step did not name, and the one card it does name
// is in the Trash rather than gone.
//
// This module imports nothing, and must keep importing nothing — the
// js/core/merge.js, asana.js, plan.js, nesting.js and schedule.js rule. Every
// other module under core/ reaches ui/dom.js sooner or later, and the moment
// this one does, tests/importplan.test.js stops loading under plain node and
// the only cover left for the whole decision table is an e2e.

/** A card's identity: its words, with the capitals and the stray whitespace
 *  taken out. Forgiving on purpose — a file written by hand, or by a model,
 *  differs from the board in exactly those ways, and two cards that read the
 *  same are the same card. */
export const cardWords = (card) =>
  `${String(card?.title || '').trim().toLowerCase()}\n${String(card?.notes || '').trim().toLowerCase()}`;

/** What makes a card out of date, field by field.
 *
 *  Everything the ledger shows and nothing else. Left out deliberately:
 *  `id`, `num`, `createdAt`, `updatedAt` and `doneAt` are bookkeeping, not
 *  content (and `doneAt` follows the column, which *is* compared); `parentId`
 *  is answered by comparing the family below; `effortSrc`/`controlSrc` record
 *  who set a value rather than what it is; and `habitHistory` is a record of
 *  what actually happened, which a file cannot know — it rides across a
 *  substitution instead of triggering one. */
const COMPARED = ['columnId', 'type', 'category', 'importance', 'urgency',
  'effort', 'control', 'deadline', 'plan', 'planSrc', 'habitFreq', 'habitCount'];

const sameList = (a, b) => JSON.stringify(a || []) === JSON.stringify(b || []);
const sameTags = (a, b) => sameList([...(a || [])].sort(), [...(b || [])].sort());

/** The words of the cards hanging off `id`, sorted — a family compared as a set
 *  of thoughts rather than as a list of ids, which is the only comparison that
 *  can mean anything across two files that share no ids. */
const familyWords = (cards, id) =>
  cards.filter((c) => c.parentId === id).map(cardWords).sort();

/** Is the file's version of a card different from the board's? */
function outOfDate(incoming, held, fileCards, boardCards) {
  for (const f of COMPARED) {
    if ((incoming[f] ?? '') !== (held[f] ?? '')) return true;
  }
  if (!sameTags(incoming.tags, held.tags)) return true;
  if (!sameList(incoming.habitTimes, held.habitTimes)) return true;
  return !sameList(familyWords(fileCards, incoming.id), familyWords(boardCards, held.id));
}

/** The file's cards, in the order the review walks them: every child before the
 *  parent it names, and otherwise the order the file wrote them in. */
function childrenFirst(fileCards) {
  const out = [];
  const seen = new Set();
  const take = (c) => { if (!seen.has(c.id)) { seen.add(c.id); out.push(c); } };
  for (const c of fileCards) {
    for (const kid of fileCards) if (kid.parentId === c.id) take(kid);
    take(c);
  }
  return out;
}

/**
 * Decide what an import file does to a board, without touching either.
 *
 * @param {Array} fileCards  the file's cards, already through sanitizeCard
 * @param {Array} boardCards the board as it stands
 * @param {Function} newId   mints an id — passed in rather than imported, so
 *   this module stays importless and the tests stay deterministic
 * @returns {{steps: Array, skipped: number}} one step per decision the reviewer
 *   would be asked to make, in review order; `skipped` counts the cards the
 *   board already holds unchanged, which are not decisions and are not shown.
 */
export function planImport(fileCards, boardCards, newId) {
  const cards = Array.isArray(fileCards) ? fileCards : [];
  const board = Array.isArray(boardCards) ? boardCards : [];

  // First match wins, so a board that already holds two cards with one set of
  // words answers with the same one every time rather than by array order.
  const held = new Map();
  for (const c of board) if (!held.has(cardWords(c))) held.set(cardWords(c), c);

  const fileById = new Map(cards.map((c) => [c.id, c]));
  const fresh = new Map(cards.map((c) => [c.id, newId()]));

  // Where a child's link points *at plan time*. A parent the board already has
  // — whether it will be kept or replaced — is that parent, because that is the
  // card the board holds while the child is being accepted; a substitution
  // moves the child on when it is applied. A parent arriving with the file is
  // its own new id. A parent the file names but does not carry is nothing,
  // which is the answer resolveLineage gives a link the board cannot honour.
  const parentFor = (c) => {
    const p = c.parentId ? fileById.get(c.parentId) : null;
    if (!p) return '';
    const twin = held.get(cardWords(p));
    return twin ? twin.id : fresh.get(p.id);
  };

  const steps = [];
  let skipped = 0;
  for (const c of childrenFirst(cards)) {
    const twin = held.get(cardWords(c));
    const card = { ...c, id: fresh.get(c.id), num: 0, parentId: parentFor(c) };
    if (!twin) {
      steps.push({ kind: 'add', card });
    } else if (outOfDate(c, twin, cards, board)) {
      // The completions ride across: a habit being replaced by its own updated
      // self is the same habit, and a file has no way of knowing what was
      // actually done. This is the mirror of the rule that a Duplicate never
      // inherits a history — there the copy is a different habit.
      steps.push({ kind: 'substitute', replaces: twin, card: { ...card, habitHistory: twin.habitHistory || {} } });
    } else {
      skipped += 1;
    }
  }
  return { steps, skipped };
}

/**
 * Apply one accepted step to a list of cards, and return the new list.
 *
 * Pure, so the whole flow — accept these, refuse that one — is testable without
 * a DOM, and so a review that is abandoned half way through has left the board
 * in exactly the state its accepted steps describe.
 *
 * A substitution puts the replacement **where the old card sat**: array order is
 * what decides a card's place in its column, so appending it would silently move
 * the card to the bottom of a column it had been near the top of.
 */
export function applyStep(cards, step) {
  if (!step || !step.card) return cards;
  if (step.kind !== 'substitute') return [...cards, step.card];

  const goneId = step.replaces.id;
  const at = cards.findIndex((c) => c.id === goneId);
  const rest = cards
    .filter((c) => c.id !== goneId)
    // The family follows the parent. Nothing is deleted here but the one card
    // the step names — a child left pointing at a card that is no longer on the
    // board would be detached by the next save, with nothing on screen saying so.
    .map((c) => (c.parentId === goneId ? { ...c, parentId: step.card.id } : c));
  rest.splice(at === -1 ? rest.length : at, 0, step.card);
  return rest;
}
