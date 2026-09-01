// tests/plan.test.js
//
// The plan a card carries: a year, a year and month, or a full day. Three
// things here can go quietly wrong and take the whole feature with them, so
// each has a test:
//
//   1. The partial date. "A day needs a month, a month needs a year" is
//      structural in this shape ('2027-03' cannot hold a stray day), but a bad
//      tail must be dropped rather than the whole value — losing a year someone
//      typed because they picked the 30th of February is the wrong trade.
//   2. The deadline link. A plan follows the deadline until a person edits it,
//      and never afterwards. Both directions are load-bearing.
//   3. Where a card lands. Every planned card belongs to exactly one section in
//      the rail, and the edges (today, the Sunday of the ISO week, the last day
//      of a short month, an overdue plan of any precision) are where an
//      off-by-one hides.
//
// js/core/plan.js imports nothing, like js/core/merge.js: the moment it reaches
// ui/dom.js this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PLAN_SECTIONS, planCardsIn, planConflict, planEnd, planGroups, planSection,
  planSrcVal, planStart, planVal, resolvePlan,
} from '../js/core/plan.js';

// A Wednesday, mid-month, mid-year — every edge is a different date.
const AT = new Date(2026, 7, 26, 10, 0); // 2026-08-26, local time

const card = (id, plan, over = {}) =>
  ({ id, num: 1, title: id, plan, planSrc: 'user', deadline: '',
     columnId: 'inbox', type: 'task', category: '', ...over });

// This is a unit test.
test('a plan is a year, a year and month, or a day — and a bad tail is dropped', () => {
  assert.equal(planVal('2027'), '2027');
  assert.equal(planVal('2027-03'), '2027-03');
  assert.equal(planVal('2027-03-04'), '2027-03-04');

  // The tail goes, the part that made sense stays.
  assert.equal(planVal('2027-02-30'), '2027-02'); // February has no 30th
  assert.equal(planVal('2027-13'), '2027');       // no 13th month
  assert.equal(planVal('2027-13-04'), '2027');

  assert.equal(planVal('not a date'), '');
  assert.equal(planVal('26-03'), '');
  assert.equal(planVal(1234), '');
  assert.equal(planVal(undefined), '');

  assert.equal(planSrcVal('user'), 'user');
  assert.equal(planSrcVal('ai'), 'ai');
  assert.equal(planSrcVal('nonsense'), 'auto');
});

// This is a unit test.
test('a plan covers a window: its first day and its last', () => {
  assert.equal(planStart('2027'), '2027-01-01');
  assert.equal(planEnd('2027'), '2027-12-31');
  assert.equal(planStart('2027-02'), '2027-02-01');
  assert.equal(planEnd('2027-02'), '2027-02-28'); // and a short month knows it
  assert.equal(planEnd('2028-02'), '2028-02-29'); // leap year
  assert.equal(planStart('2027-03-04'), '2027-03-04');
  assert.equal(planEnd('2027-03-04'), '2027-03-04');
  assert.equal(planStart(''), '');
});

// This is a unit test.
test('the plan follows the deadline until a person sets it', () => {
  // auto: the deadline is the plan, whatever was stored.
  assert.equal(resolvePlan({ plan: '2020', planSrc: 'auto', deadline: '2026-09-01' }), '2026-09-01');
  assert.equal(resolvePlan({ plan: '2020', planSrc: 'auto', deadline: '' }), '');

  // user: untouched, including a deliberately empty plan on a dated card.
  assert.equal(resolvePlan({ plan: '2027-03', planSrc: 'user', deadline: '2026-09-01' }), '2027-03');
  assert.equal(resolvePlan({ plan: '', planSrc: 'user', deadline: '2026-09-01' }), '');
});

// This is a unit test.
test('a plan that starts after the deadline is a contradiction', () => {
  // The start, not the end: planning "some time in 2026" for a thing due in
  // September 2026 still has room before the date.
  assert.equal(planConflict('2026', '2026-09-01'), false);
  assert.equal(planConflict('2026-09', '2026-09-01'), false);
  assert.equal(planConflict('2026-09-01', '2026-09-01'), false);

  assert.equal(planConflict('2026-09-02', '2026-09-01'), true);
  assert.equal(planConflict('2026-10', '2026-09-01'), true);
  assert.equal(planConflict('2027', '2026-09-01'), true);

  // Nothing to contradict.
  assert.equal(planConflict('', '2026-09-01'), false);
  assert.equal(planConflict('2027', ''), false);
});

// This is a unit test.
test('a plan lands in the nearest calendar frame that fits it', () => {
  const s = (plan, over) => planSection(card('x', plan, over), AT);

  // Overdue is nearest, whatever precision it was planned at — it needs
  // today's attention, so it is listed with today.
  assert.equal(s('2026-08-20'), 'day');
  assert.equal(s('2026-07'), 'day');
  assert.equal(s('2025'), 'day');

  assert.equal(s('2026-08-26'), 'day');   // today
  assert.equal(s('2026-08-30'), 'week');  // the Sunday of this ISO week
  assert.equal(s('2026-08-31'), 'month'); // Monday, still this month
  assert.equal(s('2026-08'), 'month');    // this month, no day picked
  assert.equal(s('2026-09-01'), 'year');
  assert.equal(s('2026-09'), 'year');
  assert.equal(s('2026'), 'year');

  // Next year is its own frame, and it takes the years after it too — the
  // rows carry their own dates, so a plan for 2030 says so.
  assert.equal(s('2027'), 'next');
  assert.equal(s('2027-03'), 'next');
  assert.equal(s('2027-01-01'), 'next');
  assert.equal(s('2030-05-01'), 'next');

  // A dream is filed by its date like anything else. Being a dream puts it in
  // the Dreams list as well, which planGroups does — not this.
  assert.equal(s('2026-08-26', { type: 'dream' }), 'day');
  assert.equal(s('', { type: 'dream' }), '');

  // Nothing to show: no plan, already done, or a habit (it has its own strip).
  assert.equal(s(''), '');
  assert.equal(s('2026-08-26', { columnId: 'answered' }), '');
  assert.equal(s('2026-08-26', { type: 'habit', habitFreq: 'daily' }), '');
});

// This is a unit test.
test('a dream is listed by its date and again as a dream', () => {
  const cards = [
    card('year', '2026-12-01'),
    card('today', '2026-08-26'),
    card('overdue', '2026-07-01'),
    card('week', '2026-08-29'),
    card('month', '2026-08-31'),
    card('next', '2027-06'),
    card('dated dream', '2026-08-31', { type: 'dream' }),
    card('open dream', '', { type: 'dream' }),
    card('done dream', '2026-08-31', { type: 'dream', columnId: 'answered' }),
    card('unplanned', ''),
  ];

  const groups = planGroups(cards, AT);
  assert.deepEqual(groups.map((g) => g.id), PLAN_SECTIONS.map((s) => s.id));
  assert.deepEqual(
    Object.fromEntries(groups.map((g) => [g.id, g.cards.map((c) => c.id)])),
    {
      day: ['overdue', 'today'],
      week: ['week'],
      // The dated dream is here *and* below: it is a want with a date, and
      // both facts are worth seeing.
      month: ['month', 'dated dream'],
      year: ['year'],
      next: ['next'],
      // Nearest first here too: a dream with a date is nearer than an
      // open-ended one.
      dreams: ['dated dream', 'open dream'],
    },
  );
  // A finished dream is finished, in both lists.
  assert.ok(!JSON.stringify(groups).includes('done dream'));

  // Read one horizon at a time and the nearer ones come with it.
  assert.deepEqual(planCardsIn(cards, 'today', AT).map((c) => c.id), ['overdue', 'today']);
  assert.deepEqual(planCardsIn(cards, 'week', AT).map((c) => c.id), ['overdue', 'today', 'week']);
  assert.deepEqual(planCardsIn(cards, 'month', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month', 'dated dream']);
  assert.deepEqual(planCardsIn(cards, 'year', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month', 'dated dream', 'year']);
  // Next year is the exception, and it is the one the accumulation made
  // useless: with nothing planned for 2027 it repeated 'This year' word for
  // word, so the two entries in the picker answered the same question. It
  // shows next year and the years after it, alone.
  assert.deepEqual(planCardsIn(cards, 'next', AT).map((c) => c.id), ['next']);
  // The dreams horizon is the whole life area, dated or not.
  assert.deepEqual(planCardsIn(cards, 'dream', AT).map((c) => c.id),
    ['dated dream', 'open dream']);
});

// This is a unit test.
test('cards sort by deadline: nearest last day first, then card number', () => {
  // Year-only plans sort by their last day (12-31), month-only by last day of month.
  const cards = [
    card('plan-2027', '2027', { num: 1 }),        // deadline: 2027-12-31
    card('plan-2025', '2025', { num: 2 }),        // deadline: 2025-12-31 (overdue)
    card('plan-2026-12', '2026-12', { num: 3 }),  // deadline: 2026-12-31
    card('plan-2026-07', '2026-07', { num: 4 }),  // deadline: 2026-07-31 (overdue)
    card('plan-2026-08-20', '2026-08-20', { num: 5 }), // deadline: 2026-08-20 (overdue)
    card('plan-2026-08-26', '2026-08-26', { num: 6 }), // deadline: 2026-08-26 (today)
    card('plan-2026-09-10', '2026-09-10', { num: 7 }), // deadline: 2026-09-10
  ];

  // Sort by deadline nearest first: overdue grouped as today, then by date
  const sorted = planGroups(cards, AT);
  const dayCards = sorted[0].cards.map((c) => c.id);
  const yearCards = sorted[3].cards.map((c) => c.id);
  const nextCards = sorted[4].cards.map((c) => c.id);

  // Day section: overdue comes before today (sorted by deadline)
  assert.deepEqual(dayCards, [
    'plan-2025', 'plan-2026-07', 'plan-2026-08-20', 'plan-2026-08-26',
  ]);

  // Year section: sorted by deadline (end date)
  // plan-2026-09-10 ends 2026-09-10, plan-2026-12 ends 2026-12-31
  assert.deepEqual(yearCards, [
    'plan-2026-09-10', 'plan-2026-12',
  ]);

  // Next section: plan-2027 ends 2027-12-31
  assert.deepEqual(nextCards, ['plan-2027']);

  // Within same precision, earlier deadlines come first
  const samePrecision = [
    card('late-sep', '2026-09-15', { num: 1 }),
    card('early-sep', '2026-09-05', { num: 2 }),
    card('mid-sep', '2026-09-10', { num: 3 }),
  ];
  const result = planCardsIn(samePrecision, 'year', AT).map((c) => c.id);
  assert.deepEqual(result, ['early-sep', 'mid-sep', 'late-sep']);

  // Tie-breaking by card number
  const sameDateDifferentNum = [
    card('num-3', '2026-09-10', { num: 3 }),
    card('num-1', '2026-09-10', { num: 1 }),
    card('num-2', '2026-09-10', { num: 2 }),
  ];
  const tieResult = planCardsIn(sameDateDifferentNum, 'year', AT).map((c) => c.id);
  assert.deepEqual(tieResult, ['num-1', 'num-2', 'num-3']);
});
