// The lens table: five named questions, each one a set of filter values the
// board already knows how to apply.
//
// Nobody composes six predicates in their head. They arrive wanting one of five
// things — what is due now, what is due this week, everything still open, what
// they have started, what they finished — so the composing is done here and
// `matchesFilters` is not touched at all. A lens writes the same `filters`
// object the panel writes; the only new claim on screen is which lens is
// pressed, and that is *derived* (`lensOf`) rather than stored, so moving one
// control by hand un-presses the row instead of leaving a lit button over a
// board it no longer describes.
//
// Two rules the table keeps, both of them about not lying to the reader:
//
//   1. **Every lens names every field a lens may set.** A patch that mentioned
//      only what it cares about would leave the previous lens's type, tag or
//      column in force, and the board would answer a question nobody asked.
//   2. **No lens touches the text search or the life area.** A search is what
//      the reader typed a second ago and the area tabs are a second axis they
//      are standing in; a lens that cleared either would throw away the thing
//      they were looking at.
//
// This module imports nothing — the js/core/plan.js, nesting.js, schedule.js,
// timespan.js, direction.js and duplicates.js rule. The whole table is pinned
// by tests/lenses.test.js under plain node, and one import of a ui/ module
// would silently make it an e2e-only surface. The range shapes below are the
// ones js/core/timespan.js validates; they are written out rather than built,
// so the table can be read as the spec it is.

/** The time filter switched off — its own default, spelled out because a lens
 *  has to be able to *clear* a span as well as set one. */
const NO_SPAN = { field: 'created', preset: '', from: '', to: '' };

/** The fields a lens sets, with the values every lens shares left at their
 *  defaults. Everything a lens may narrow by appears here exactly once. */
const base = (over = {}) => ({
  type: '', prio: '', tags: [], column: '', nesting: '', range: NO_SPAN, ...over,
});

export const LENSES = [
  {
    id: 'today',
    label: 'Today',
    // Due through the end of today, late work included: a card due last
    // Tuesday is more today's work than one due in a fortnight. It reads the
    // deadline alone — a plan is a part-date with its own section in the rail.
    hint: 'Due today or already late, in the open columns',
    set: base({ range: { field: 'due', preset: 'through-today', from: '', to: '' } }),
  },
  {
    id: 'week',
    label: 'This week',
    hint: 'Due between this Monday and this Sunday',
    set: base({ range: { field: 'due', preset: 'week', from: '', to: '' } }),
  },
  {
    id: 'open',
    // Every filter off, which is also the state the board boots in — so this
    // lens reads as pressed on a board nobody has narrowed. That is the honest
    // answer rather than a special case: an unnarrowed board *is* everything
    // open, and Done is kept out of the other views by the existing default.
    label: 'Everything open',
    hint: 'Every filter off — the whole board, minus what is finished',
    set: base(),
  },
  {
    id: 'waiting',
    label: 'Waiting on me',
    // The In Progress column: work already begun and stalled on nobody else.
    // Inert on the Board by the rule that already governs that filter — the
    // Board *is* its columns — so this lens answers on the Backlog, the map,
    // the Matrix and the register.
    hint: 'Started and not finished — the In Progress column',
    set: base({ column: 'in-progress' }),
  },
  {
    id: 'done',
    label: 'Finished',
    // Names the Done column *and* the month on `doneAt`, so the hide-Done
    // default stands down by the suspension `askingAboutFinished` already
    // writes rather than by a rule invented for lenses.
    hint: 'Finished this month',
    set: base({ column: 'answered', range: { field: 'done', preset: 'month', from: '', to: '' } }),
  },
];

const byId = (id) => LENSES.find((l) => l.id === id) || null;

/** A **fresh** patch of filter values, or `null`. Fresh, because the table is
 *  module state: handing back the very object the caller is about to write the
 *  reader's own edits into would let one press redefine a lens for good. */
export function lensFilters(id) {
  const lens = byId(id);
  if (!lens) return null;
  return { ...lens.set, tags: [...lens.set.tags], range: { ...lens.set.range } };
}

const sameTags = (a, b) => {
  const x = [...(a || [])].sort();
  const y = [...(b || [])].sort();
  return x.length === y.length && x.every((t, i) => t === y[i]);
};

/** Which lens the board is currently showing, or `''` for none.
 *
 *  Read off the filters every time rather than stored: a pressed state that
 *  remembered itself would survive the reader changing a control by hand, and
 *  a lit button over a board it does not describe is worse than no button at
 *  all. `search` and `category` are not consulted, by rule 2 above — a lens
 *  stays pressed while you search inside it or stand in one life area. */
export function lensOf(filters) {
  const f = filters || {};
  const r = f.range || {};
  for (const lens of LENSES) {
    const s = lens.set;
    if ((f.type || '') !== s.type) continue;
    if ((f.prio || '') !== s.prio) continue;
    if ((f.column || '') !== s.column) continue;
    if ((f.nesting || '') !== s.nesting) continue;
    if (!sameTags(f.tags, s.tags)) continue;
    if ((r.field || 'created') !== s.range.field) continue;
    if ((r.preset || '') !== s.range.preset) continue;
    if ((r.from || '') !== s.range.from) continue;
    if ((r.to || '') !== s.range.to) continue;
    return lens.id;
  }
  return '';
}
