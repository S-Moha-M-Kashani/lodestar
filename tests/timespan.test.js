// tests/timespan.test.js
//
// The span maths behind the time-range filter: what "this week" means, where a
// typed range starts and stops, and how the control reads its own value back.
//
// Four things here can go quietly wrong and each one lies to the reader rather
// than erroring:
//
//   1. **A boundary in the wrong time zone.** These spans are one person's
//      local week, month and year. A week computed from a UTC midnight starts
//      on Sunday evening for a reader in Europe, so "captured this week" would
//      answer with Sunday's cards on a Monday morning — right shape, wrong
//      week, and nothing on screen to show it.
//   2. **An end that stops at midnight.** A range ending today has to include
//      what was finished an hour ago, which is the commonest thing anyone will
//      ask of it. An exclusive end silently drops the whole of the last day.
//   3. **An empty range that narrows anyway.** The filter is off by default and
//      `spanOf` answering a span rather than "no span" would narrow every view
//      in the app the moment the module is imported.
//   4. **A read-back that does not say what the filter is doing.** Three
//      controls showing a field, a preset and two dates never *look* wrong even
//      when they say something else — the `planWords` lesson. `rangeText` is
//      the sentence the reader checks the filter against.
//
// Every assert names a fixed moment, `new Date('2026-09-07T10:00')` — a Monday
// — so this suite cannot change its answer overnight. And js/core/timespan.js
// imports nothing, the js/core/plan.js / nesting.js / schedule.js rule: the
// moment it reaches ui/dom.js this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rangeText, rangeVal, spanOf } from '../js/core/timespan.js';

// The Monday every span below is measured from, and the two neighbours the
// boundaries are checked against.
const NOW = new Date('2026-09-07T10:00');
const at = (s) => new Date(s).getTime();
const range = (over = {}) => ({ field: 'created', preset: '', from: '', to: '', ...over });

// This is a unit test.
test('an empty range narrows nothing, and says so', () => {
  // `null` and not an infinite span: it is what gives `matchesRange` one early
  // return, and what makes "is the filter on at all" the same question
  // `filtersActive()` and the control's read-back both ask, in one place.
  assert.equal(spanOf(range(), NOW), null);
  // 'custom' with neither date typed is the same answer: the reader has opened
  // the control and not yet said anything with it.
  assert.equal(spanOf(range({ preset: 'custom' }), NOW), null);
  // A stored value that is no longer a legal range is discarded, not trusted:
  // rangeVal is what a localStorage read and a hand-edited select both go
  // through, and an unknown preset that survived would narrow the board with a
  // rule nothing can name.
  assert.deepEqual(rangeVal({ field: 'nonsense', preset: 'fortnight', from: 7, to: null }),
    { field: 'created', preset: '', from: '', to: '' });
  assert.deepEqual(rangeVal(null), { field: 'created', preset: '', from: '', to: '' });
  assert.deepEqual(rangeVal({ field: 'done', preset: 'week', from: '', to: '' }),
    { field: 'done', preset: 'week', from: '', to: '' });
});

// This is a unit test.
test('each preset spans its own local period, end of day included', () => {
  // This week: the local Monday, and NOW is that Monday — so a card captured
  // at one minute past midnight this morning is inside it and one captured at
  // one minute to midnight last night is not.
  const week = spanOf(range({ preset: 'week' }), NOW);
  assert.equal(week.from, at('2026-09-07T00:00:00.000'));
  assert.equal(week.to, at('2026-09-13T23:59:59.999'));
  // The boundary is the local Monday and not a UTC one: at UTC+2 a week
  // measured from a UTC midnight would start at 02:00 local, and this card
  // would fall outside the week it was captured in.
  assert.ok(at('2026-09-07T00:30') >= week.from);
  assert.ok(at('2026-09-06T23:59') < week.from);

  const month = spanOf(range({ preset: 'month' }), NOW);
  assert.equal(month.from, at('2026-09-01T00:00:00.000'));
  assert.equal(month.to, at('2026-09-30T23:59:59.999'));

  const year = spanOf(range({ preset: 'year' }), NOW);
  assert.equal(year.from, at('2026-01-01T00:00:00.000'));
  assert.equal(year.to, at('2026-12-31T23:59:59.999'));

  // Thirty whole days, today included — so the span opens on the 9th of
  // August, twenty-nine days back, and closes at the end of today rather than
  // at the moment it was asked.
  const days30 = spanOf(range({ preset: 'days30' }), NOW);
  assert.equal(days30.from, at('2026-08-09T00:00:00.000'));
  assert.equal(days30.to, at('2026-09-07T23:59:59.999'));
});

// This is a unit test.
test('a typed range takes whole days, and either end may be left open', () => {
  const both = spanOf(range({ preset: 'custom', from: '2026-03-01', to: '2026-03-15' }), NOW);
  assert.equal(both.from, at('2026-03-01T00:00:00.000'));
  assert.equal(both.to, at('2026-03-15T23:59:59.999'));
  // The whole end day, which is what makes "up to today" usable: a card
  // finished at 23:00 on the last day of the span is inside it.
  assert.ok(at('2026-03-15T23:00') <= both.to);

  // A start with no end — "since the first of March".
  const since = spanOf(range({ preset: 'custom', from: '2026-03-01' }), NOW);
  assert.equal(since.from, at('2026-03-01T00:00:00.000'));
  assert.equal(since.to, Infinity);

  // An end with no start — "up to the first of March".
  const until = spanOf(range({ preset: 'custom', to: '2026-03-01' }), NOW);
  assert.equal(until.from, -Infinity);
  assert.equal(until.to, at('2026-03-01T23:59:59.999'));

  // A malformed date is no date: the shape is checked and an impossible day
  // (2026-02-30) is refused by the same round-trip `deadlineVal` uses, so a
  // half-typed input narrows nothing rather than narrowing to nonsense.
  assert.equal(spanOf(range({ preset: 'custom', from: '01/03/2026' }), NOW), null);
  assert.equal(spanOf(range({ preset: 'custom', from: '2026-02-30' }), NOW), null);
  assert.equal(spanOf(range({ preset: 'custom', from: '2026-3-1' }), NOW), null);

  // A start after its end is a contradiction the reader typed, and it is
  // answered as the empty span it is — from above to — rather than being
  // silently swapped into the span they did not ask for.
  const back = spanOf(range({ preset: 'custom', from: '2026-03-15', to: '2026-03-01' }), NOW);
  assert.ok(back.from > back.to);
});

// This is a unit test.
test('the read-back says what the filter is doing, at the precision given', () => {
  assert.equal(rangeText(range()), 'Any time');
  assert.equal(rangeText(range({ preset: 'week' })), 'Captured this week');
  assert.equal(rangeText(range({ field: 'done', preset: 'month' })), 'Finished this month');
  assert.equal(rangeText(range({ field: 'done', preset: 'days30' })), 'Finished in the last 30 days');
  assert.equal(rangeText(range({ preset: 'year' })), 'Captured this year');
  assert.equal(rangeText(range({ field: 'done', preset: 'custom', from: '2026-03-01', to: '2026-03-15' })),
    'Finished 1 Mar 2026 – 15 Mar 2026');
  assert.equal(rangeText(range({ preset: 'custom', from: '2026-03-01' })), 'Captured since 1 Mar 2026');
  assert.equal(rangeText(range({ field: 'done', preset: 'custom', to: '2026-03-01' })), 'Finished up to 1 Mar 2026');
  // An opened-but-unsaid custom range reads as the nothing it is, so the
  // sentence never claims a span the filter is not applying.
  assert.equal(rangeText(range({ preset: 'custom' })), 'Any time');
});
