// tests/calendar.test.js
//
// The two-click rule behind `Between…`, and the month grid it is pressed on.
//
// The span used to be typed into two date boxes, which is a control that can
// hold a contradiction — a start after its end — and which asks the reader to
// know a date rather than to find a day. One calendar answers it the way a
// flight search does: press the start, press the end. Four things there can go
// quietly wrong, and each one hands the board a span nobody asked for rather
// than an error:
//
//   1. **A backwards span.** A reader correcting themselves presses a day
//      earlier than the one they just chose. That is the correction, not a
//      mistake, so it becomes the start and the old day becomes the end.
//   2. **A third press with nowhere to go.** With both ends settled, the next
//      press has to begin a new span rather than stretching the old one or
//      being ignored — or the reader is made to clear the old one first.
//   3. **One day that is not a span.** Pressing the 5th twice means the 5th,
//      whole. An implementation that refused it, or that left the end open,
//      would answer a question the reader did not ask.
//   4. **A half-chosen span narrowing the board.** Only the start is set, so
//      `spanOf` must still be reading nothing: the board must not repaint
//      under the hand that is choosing.
//
// Every assert names a fixed day, so this suite cannot change its answer
// overnight. And js/core/calendar.js imports nothing — the js/core/plan.js /
// nesting.js / schedule.js / timespan.js / lenses.js rule: the moment it
// reaches ui/dom.js this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { monthGrid, pressDay } from '../js/core/calendar.js';

const span = (from = '', to = '') => ({ from, to });

// This is a unit test.
test('the first press sets the start and leaves the end open', () => {
  assert.deepEqual(pressDay(span(), '2026-09-03'), { from: '2026-09-03', to: '' });
});

// This is a unit test.
test('the second press closes the span, in either direction', () => {
  // Forwards: the 3rd then the 11th.
  assert.deepEqual(pressDay(span('2026-09-03'), '2026-09-11'),
    { from: '2026-09-03', to: '2026-09-11' });
  // Backwards: the reader is correcting themselves, so the earlier day becomes
  // the start and the day already chosen becomes the end. No error, and never
  // a span that reads from the 11th to the 3rd.
  assert.deepEqual(pressDay(span('2026-09-11'), '2026-09-03'),
    { from: '2026-09-03', to: '2026-09-11' });
  // And the same day twice is that one day, start and end alike.
  assert.deepEqual(pressDay(span('2026-09-05'), '2026-09-05'),
    { from: '2026-09-05', to: '2026-09-05' });
});

// This is a unit test.
test('a third press begins a new span rather than stretching the old one', () => {
  const chosen = { from: '2026-09-03', to: '2026-09-11' };
  assert.deepEqual(pressDay(chosen, '2026-09-20'), { from: '2026-09-20', to: '' });
  // The span it was pressed on is untouched: the table is read, never written.
  assert.deepEqual(chosen, { from: '2026-09-03', to: '2026-09-11' });
  // An end left open by hand — "since the first of March" — is a settled span
  // too, so the next press starts again rather than closing it behind the
  // reader's back.
  assert.deepEqual(pressDay({ from: '', to: '2026-09-11' }, '2026-09-20'),
    { from: '2026-09-20', to: '' });
});

// This is a unit test.
test('a malformed day is refused rather than stored', () => {
  assert.deepEqual(pressDay(span(), '2026-02-30'), { from: '', to: '' });
  assert.deepEqual(pressDay(span('2026-09-03'), 'nonsense'),
    { from: '2026-09-03', to: '' });
});

// This is a unit test.
test('the month grid is whole weeks, Monday first, and says which days are its own', () => {
  // September 2026 begins on a Tuesday and has 30 days.
  const grid = monthGrid('2026-09-14');
  assert.equal(grid.year, 2026);
  assert.equal(grid.month, 9);
  assert.equal(grid.label, 'September 2026');
  // Whole weeks, so the columns line up under one row of weekday names.
  assert.equal(grid.days.length % 7, 0);
  assert.equal(grid.days[0].iso, '2026-08-31'); // the Monday before the 1st
  assert.equal(grid.days[0].inMonth, false);
  assert.equal(grid.days[1].iso, '2026-09-01');
  assert.equal(grid.days[1].inMonth, true);
  assert.equal(grid.days.filter((d) => d.inMonth).length, 30);
  // And the neighbours it steps to, so the control needs no date maths of its
  // own — a second place doing this is a second place to get February wrong.
  assert.equal(monthGrid('2026-01-15').prev, '2025-12-01');
  assert.equal(monthGrid('2026-12-15').next, '2027-01-01');
});
