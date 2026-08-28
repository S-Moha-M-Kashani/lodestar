// tests/plan.test.js
//
// The plan horizons. The rail's five distances — today, this week, this month,
// this year, life dream — are not five stored lists: they are one question
// ("what is due by the end of X?") asked of the deadline every card already
// carries. That is the whole point of doing it this way, and it is also the
// part that can quietly go wrong, because every horizon has an edge (the last
// day of an ISO week, a short month, New Year's Eve) and an overdue card must
// never fall out of the nearest one — a card that slipped past its date is the
// first thing a plan for today has to say.
//
// js/core/plan.js imports nothing, like js/core/merge.js and js/core/asana.js:
// the moment it reaches ui/dom.js this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PLAN_HORIZONS, planCardsIn, planHorizonEnd, planHorizonVal } from '../js/core/plan.js';

// A Wednesday, mid-month, mid-year — every horizon end is a different date.
const AT = new Date(2026, 7, 26, 10, 0); // 2026-08-26, local time

const card = (id, deadline, over = {}) =>
  ({ id, num: 1, title: id, deadline, columnId: 'inbox', type: 'task', ...over });

// This is a unit test.
test('the five horizons end where the calendar ends', () => {
  assert.deepEqual(PLAN_HORIZONS.map((h) => h.id), ['today', 'week', 'month', 'year', 'dream']);
  assert.equal(planHorizonEnd('today', AT), '2026-08-26');
  assert.equal(planHorizonEnd('week', AT), '2026-08-30'); // the Sunday of that ISO week
  assert.equal(planHorizonEnd('month', AT), '2026-08-31');
  assert.equal(planHorizonEnd('year', AT), '2026-12-31');
  assert.equal(planHorizonEnd('dream', AT), ''); // a dream has no last day
  assert.equal(planHorizonVal('nonsense'), 'today'); // a stale stored pick
});

// This is a unit test.
test('a horizon holds everything due by its end, nearest date first', () => {
  const cards = [
    card('year', '2026-12-01'),
    card('today', '2026-08-26'),
    card('overdue', '2026-07-01'),
    card('week', '2026-08-30'),
    card('month', '2026-08-31'),
    card('dream', ''),
  ];

  // Overdue first, and never dropped: the horizons nest, so what is late is
  // still what is nearest.
  assert.deepEqual(planCardsIn(cards, 'today', AT).map((c) => c.id), ['overdue', 'today']);
  assert.deepEqual(planCardsIn(cards, 'week', AT).map((c) => c.id), ['overdue', 'today', 'week']);
  assert.deepEqual(planCardsIn(cards, 'month', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month']);
  assert.deepEqual(planCardsIn(cards, 'year', AT).map((c) => c.id),
    ['overdue', 'today', 'week', 'month', 'year']);

  // Life dream is the other half of the board: what was never pinned to a date.
  assert.deepEqual(planCardsIn(cards, 'dream', AT).map((c) => c.id), ['dream']);
});

// This is a unit test.
test('a plan lists only what is still open, and never a habit', () => {
  const cards = [
    card('open', '2026-08-26'),
    card('done', '2026-08-26', { columnId: 'answered' }),
    card('habit', '2026-08-26', { type: 'habit', habitFreq: 'daily' }),
  ];
  // Done is where the checkmark sends a card, so a finished card leaving the
  // list is the feedback that the click worked. Habits keep their own strip
  // in the rail above and would otherwise be listed twice.
  assert.deepEqual(planCardsIn(cards, 'today', AT).map((c) => c.id), ['open']);
});
