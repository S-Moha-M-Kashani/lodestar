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
test('every planned card has exactly one home in the rail', () => {
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
  assert.equal(s('2030-05-01'), 'year');

  // A dream is a life-size want, not a date, so its category decides.
  assert.equal(s('2026-08-26', { category: 'dream' }), 'dreams');
  assert.equal(s('', { category: 'dream' }), 'dreams');

  // Nothing to show: no plan, already done, or a habit (it has its own strip).
  assert.equal(s(''), '');
  assert.equal(s('2026-08-26', { columnId: 'answered' }), '');
  assert.equal(s('2026-08-26', { type: 'habit', habitFreq: 'daily' }), '');
});

// This is a unit test.
test('stacked lists each card once; a horizon accumulates', () => {
  const cards = [
    card('year', '2026-12-01'),
    card('today', '2026-08-26'),
    card('overdue', '2026-07-01'),
    card('week', '2026-08-29'),
    card('month', '2026-08-31'),
    card('dream', '', { category: 'dream' }),
    card('unplanned', ''),
  ];

  const groups = planGroups(cards, AT);
  assert.deepEqual(groups.map((g) => g.id), PLAN_SECTIONS.map((s) => s.id));
  assert.deepEqual(
    Object.fromEntries(groups.map((g) => [g.id, g.cards.map((c) => c.id)])),
    { day: ['overdue', 'today'], week: ['week'], month: ['month'],
      year: ['year'], dreams: ['dream'] },
  );

  // Read one horizon at a time and the nearer ones come with it.
  assert.deepEqual(planCardsIn(cards, 'today', AT).map((c) => c.id), ['overdue', 'today']);
  assert.deepEqual(planCardsIn(cards, 'week', AT).map((c) => c.id), ['overdue', 'today', 'week']);
  assert.deepEqual(planCardsIn(cards, 'month', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month']);
  assert.deepEqual(planCardsIn(cards, 'year', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month', 'year']);
  assert.deepEqual(planCardsIn(cards, 'dream', AT).map((c) => c.id), ['dream']);
});
