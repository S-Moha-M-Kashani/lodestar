// tests/lenses.test.js
//
// The lens table: five named questions, each one a set of filter values the
// app already knows how to apply. Nobody composes six predicates in their
// head — they arrive wanting one of five things — so a lens is the composing
// done for them, and `matchesFilters` is not touched at all.
//
// Four things here can go quietly wrong, and every one of them lies to the
// reader rather than erroring:
//
//   1. **A lens that leaves the last one behind.** Pressing a second lens must
//      replace the first completely. A patch that names only the fields it
//      cares about leaves the previous lens's type, tag or column in force,
//      and the board then answers a question nobody asked — with a row of
//      buttons on screen claiming it is answering another.
//   2. **A lens the row cannot recognise.** The pressed state is *derived*
//      from the filters, never stored, so that changing one control by hand
//      un-presses the lens rather than leaving a button lit over a board it no
//      longer describes. That makes `lensOf` the other half of every entry in
//      the table: a patch `lensOf` cannot match is a button that can never
//      look pressed.
//   3. **A shared object.** The table is module state. Handing a caller the
//      very object it is about to write the reader's own edits into would let
//      one press of a lens permanently redefine it.
//   4. **A lens reaching past its own business.** The text search and the life
//      area are not part of any lens: a search is what the reader just typed,
//      and the area tabs are a second axis they are standing in. A lens that
//      cleared either would throw away the thing they were looking at.
//
// js/core/lenses.js imports nothing — the js/core/plan.js, nesting.js,
// schedule.js, timespan.js, direction.js and duplicates.js rule. The moment it
// reaches ui/dom.js this file stops loading under node and the whole table
// becomes an e2e-only surface.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LENSES, lensFilters, lensOf } from '../js/core/lenses.js';

// The six fields a lens is allowed to set. `search` and `category` are
// deliberately not among them (rule 4 above).
const KEYS = ['type', 'prio', 'tags', 'column', 'nesting', 'range'];

// The filter state the app boots in, spelled out rather than imported:
// js/core/state.js reaches ui/dom.js, so importing it would take this suite
// off node. If these two ever disagree the check below is what says so.
const DEFAULTS = {
  type: '', prio: '', tags: new Set(), column: '', nesting: '',
  range: { field: 'created', preset: '', from: '', to: '' },
};

// This is a unit test.
test('the five lenses, in a fixed order, each setting every field it can', () => {
  assert.deepEqual(LENSES.map((l) => l.id),
    ['today', 'week', 'open', 'waiting', 'done']);
  assert.deepEqual(LENSES.map((l) => l.label),
    ['Today', 'This week', 'Everything open', 'Waiting on me', 'Finished']);

  // Rule 1: every lens names every field, so pressing one cannot leave the
  // previous one's narrowing in force. Exactly these keys, and no others.
  for (const lens of LENSES) {
    assert.deepEqual(Object.keys(lensFilters(lens.id)).sort(), [...KEYS].sort(),
      `${lens.id} must set every field a lens may set, and only those`);
  }

  // The table itself, written out — this is the spec the row is read against.
  // 'Today' is what is due through the end of today, overdue included, in the
  // open columns: a card due last Tuesday is more today's work than one due in
  // a fortnight. It reads the deadline alone; a plan is a partial date the
  // rail already keeps its own section for.
  assert.deepEqual(lensFilters('today'), {
    type: '', prio: '', tags: [], column: '', nesting: '',
    range: { field: 'due', preset: 'through-today', from: '', to: '' },
  });
  assert.deepEqual(lensFilters('week'), {
    type: '', prio: '', tags: [], column: '', nesting: '',
    range: { field: 'due', preset: 'week', from: '', to: '' },
  });
  // 'Everything open' is every filter off — which is also the state the app
  // boots in, so this lens reads as pressed on a board nobody has narrowed.
  // That is the honest answer and not a special case: an unnarrowed board *is*
  // everything open.
  assert.deepEqual(lensFilters('open'), {
    type: '', prio: '', tags: [], column: '', nesting: '',
    range: { field: 'created', preset: '', from: '', to: '' },
  });
  // 'Waiting on me' is the In Progress column: work already begun and stalled
  // on nobody but the reader.
  assert.deepEqual(lensFilters('waiting'), {
    type: '', prio: '', tags: [], column: 'in-progress', nesting: '',
    range: { field: 'created', preset: '', from: '', to: '' },
  });
  // 'Finished' names the Done column *and* the month on `doneAt`, so the
  // hide-Done default stands down by the rule that already exists
  // (`askingAboutFinished`) rather than by a rule written for lenses.
  assert.deepEqual(lensFilters('done'), {
    type: '', prio: '', tags: [], column: 'answered', nesting: '',
    range: { field: 'done', preset: 'month', from: '', to: '' },
  });
});

// This is a unit test.
test('a lens hands out a fresh patch, never the table', () => {
  // Rule 3. Two presses of one lens must not be able to see each other's
  // edits, and neither may a caller that normalises the patch in place.
  const a = lensFilters('done');
  a.column = 'inbox';
  a.tags.push('berlin');
  a.range.preset = 'year';
  const b = lensFilters('done');
  assert.equal(b.column, 'answered');
  assert.deepEqual(b.tags, []);
  assert.equal(b.range.preset, 'month');
  assert.notEqual(a.range, b.range);
});

// This is a unit test.
test('an unknown lens narrows nothing', () => {
  // The id can reach here from a button that outlived its entry or a value an
  // older build wrote. It answers "no such lens" rather than throwing or
  // handing back a half-built patch — the `rangeVal` rule, and what keeps a
  // press of something unknown from clearing the board's filters.
  assert.equal(lensFilters('inbox-zero'), null);
  assert.equal(lensFilters(''), null);
  assert.equal(lensFilters(undefined), null);
});

// This is a unit test.
test('which lens is pressed is read back off the filters, never stored', () => {
  // Rule 2, both directions: every lens in the table must be recognisable from
  // the state it produces, or the row can never light up.
  for (const lens of LENSES) {
    const f = lensFilters(lens.id);
    assert.equal(lensOf({ ...f, tags: new Set(f.tags) }), lens.id, lens.id);
  }

  // The board as it boots is 'Everything open'.
  assert.equal(lensOf(DEFAULTS), 'open');

  // One control moved by hand un-presses the row: the values stay, the claim
  // goes. A button still lit over a board it no longer describes is the whole
  // failure this function exists to prevent.
  const today = lensFilters('today');
  const asSet = (f) => ({ ...f, tags: new Set(f.tags) });
  assert.equal(lensOf(asSet({ ...today, type: 'task' })), '');
  assert.equal(lensOf(asSet({ ...today, nesting: 'all' })), '');
  assert.equal(lensOf(asSet({ ...today, prio: '1' })), '');
  assert.equal(lensOf({ ...today, tags: new Set(['berlin']) }), '');
  assert.equal(lensOf(asSet({ ...today, range: { ...today.range, preset: 'month' } })), '');

  // Rule 4: a lens is silent about the text search and the life area, so it
  // stays pressed while the reader searches inside it or stands in one area.
  assert.equal(lensOf({ ...asSet(today), search: 'visa', category: 'work' }), 'today');

  // A tag list is the same filter whichever order it was ticked in — otherwise
  // a lens could un-press itself when a Set is rebuilt from storage.
  assert.equal(lensOf({ ...asSet(lensFilters('open')), tags: new Set(['b', 'a']) }), '');
});
