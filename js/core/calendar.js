// The month grid behind `Between…`, and the whole two-click rule as one pure
// function.
//
// The span was two date inputs, which is a control that can hold a
// contradiction — a start after its end — and which asks the reader to know a
// date rather than to find a day. One calendar answers it the way a flight
// search does: the first day pressed is the start, the second is the end, and a
// third begins again.
//
// This module imports nothing — the js/core/plan.js, nesting.js, schedule.js,
// timespan.js, lenses.js, direction.js and duplicates.js rule. The renderer
// that uses it (js/ui/date-range.js) reaches ui/dom.js, so anything left in
// there is an e2e-only surface; the decision table is exactly what wants a node
// test, and tests/calendar.test.js pins it against fixed days.
//
// It deliberately knows nothing about `filters.range`: it is handed a
// `{ from, to }` and answers a new one. What that means for the board — which
// field it measures, when it takes effect — is js/core/timespan.js's and the
// renderer's, and `custom` was already the preset there before this existed.

const pad2 = (n) => String(n).padStart(2, '0');
const iso = (y, m, d) => `${y}-${pad2(m)}-${pad2(d)}`;

/** An ISO calendar day, or ''. The same round-trip `dayVal` uses in
 *  js/core/timespan.js and `deadlineVal` uses on the card: it rejects the
 *  shape-valid impossibilities — 2026-02-30, 2026-13-01 — that a hand-edited
 *  value or an older build can hold. Written out rather than imported, because
 *  importing it is what would give this module its first dependency. */
export function dayVal(v) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return '';
  const [y, m, d] = v.split('-').map(Number);
  const at = new Date(y, m - 1, d);
  return at.getFullYear() === y && at.getMonth() === m - 1 && at.getDate() === d ? v : '';
}

/** What pressing a day does to the span being chosen.
 *
 *  The whole rule, in one place and with no state of its own, so the five cases
 *  can be read as the table they are:
 *
 *    - nothing chosen yet → that day is the start, and the end stays open;
 *    - a start and no end → the span closes, with the two days put in order —
 *      a reader pressing an earlier day is correcting themselves, and a
 *      backwards span or an error would both answer a question they did not
 *      ask. The same day twice is that one day, whole;
 *    - a settled span → a new span begins on that day, so nobody is made to
 *      clear the old one first. "Settled" includes an end left open by hand
 *      ("up to the first of March"), which is a span the reader finished
 *      saying rather than one they are halfway through.
 *
 *  A day it cannot read leaves the span exactly as it was: a calendar handed a
 *  30th of February narrows nothing rather than narrowing by a date nobody
 *  named. */
export function pressDay(range, day) {
  const from = dayVal(range?.from);
  const to = dayVal(range?.to);
  const pressed = dayVal(day);
  if (!pressed) return { from, to };
  // Halfway through: a start chosen, no end yet. Anything else — both ends, an
  // open start, or nothing at all — is a span to begin again from.
  const halfway = Boolean(from) && !to;
  if (!halfway) return { from: pressed, to: '' };
  return pressed < from ? { from: pressed, to: from } : { from, to: pressed };
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

/** The weekday names, Monday first — the week this board already counts in
 *  (`habitPeriod`'s ISO week, and `spanOf`'s "this week"). */
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** One month as whole weeks, with the neighbours the control steps to.
 *
 *  Whole weeks, so every column stands under one row of weekday names; the
 *  days either side carry `inMonth: false` rather than being blanks, because a
 *  reader choosing the 1st of a month often means the 31st of the one before
 *  and a grid of empty cells is a calendar that refuses the press.
 *
 *  `prev` and `next` are computed here too. They are day-0 arithmetic on a
 *  local Date, which is the one place in this app that knows nothing about
 *  month lengths or leap years — and a renderer doing it a second time is a
 *  second place to get February wrong. */
export function monthGrid(day) {
  const seed = dayVal(day);
  const now = new Date();
  const [y, m] = seed
    ? seed.split('-').map(Number)
    : [now.getFullYear(), now.getMonth() + 1];

  const first = new Date(y, m - 1, 1);
  // getDay() is 0 for Sunday, so Sunday is six days into the week, not minus
  // one — the same correction `spanOf`'s week makes.
  const back = (first.getDay() + 6) % 7;
  const start = new Date(y, m - 1, 1 - back);
  const length = new Date(y, m, 0).getDate();
  const cells = Math.ceil((back + length) / 7) * 7;

  const days = [];
  for (let i = 0; i < cells; i++) {
    const at = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    days.push({
      iso: iso(at.getFullYear(), at.getMonth() + 1, at.getDate()),
      day: at.getDate(),
      inMonth: at.getMonth() === m - 1 && at.getFullYear() === y,
    });
  }

  const before = new Date(y, m - 2, 1);
  const after = new Date(y, m, 1);
  return {
    year: y,
    month: m,
    label: `${MONTHS[m - 1]} ${y}`,
    days,
    prev: iso(before.getFullYear(), before.getMonth() + 1, 1),
    next: iso(after.getFullYear(), after.getMonth() + 1, 1),
  };
}
