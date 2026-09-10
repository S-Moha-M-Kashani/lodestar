// tests/direction.test.js
//
// What the board says to do next, read off the cards with no model involved.
//
// The rules this file pins, and the figure each one was chosen against on the
// owner's real board (2026-09-10, 58 live cards — the change's
// measurements.md):
//
//   - **overdue** (5 cards), **a habit behind its cadence** (7 of 7), **due
//     soon** (0 today, kept because it is `overdue` with the comparison
//     reversed), **a hand-set plan naming now on a card still in the Inbox**
//     (13 cards) and **a life area nothing has moved in for a month** (3
//     areas). The order they are listed in *is* the ordering rule: a
//     committed, time-bound card outranks a merely available one.
//   - **No reason may fill the direction**, so the five overdue cards cannot
//     silence the seven habits that are behind.
//   - **It cannot lie**: an empty board says so, a proposal is never named, a
//     card in the Trash is never named, and nothing appears that was not in
//     the list handed in.
//   - **Encouragement cites or says nothing.** `doneAt` is three days old on
//     the real board, so the scenario that actually fires there is "a bad week
//     is not dressed up" — that is the one this file spends its asserts on.
//
// js/core/direction.js therefore imports nothing — the js/core/merge.js,
// asana.js, plan.js, nesting.js, schedule.js, timespan.js and importplan.js
// rule. The cadence arithmetic is *passed in* for the same reason: `expectedBy`
// below is core/schedule.js's own, and the period ids are the ones
// measurements.md recorded for the day this test fixes.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIRECTION_MAX, direction } from '../js/core/direction.js';
import { expectedBy } from '../js/core/schedule.js';

// Noon on 10 September 2026, local — every assert below is against this
// moment, so the suite cannot change its answer overnight.
const AT = new Date(2026, 8, 10, 12, 0);
const DAY = 86400000;
const ago = (days) => AT.getTime() - days * DAY;

// The period ids AT falls in, per measurements.md. Written out rather than
// derived: core/habits.js owns `habitPeriod` and reaches core/state.js, which
// is exactly why the arithmetic is injected instead of imported.
const PERIOD = { daily: '2026-09-10', weekly: '2026-W37', monthly: '2026-09', yearly: '2026' };

/** The cadence measurement the widget will pass in, composed here from the
 *  real `expectedBy` so the rule is tested against the board's own arithmetic
 *  and not against a copy of it. */
const habitBehind = (card, at) => ({
  done: (card.habitHistory?.[PERIOD[card.habitFreq]] || []).length,
  expected: expectedBy(card.habitFreq, card.habitTimes, card.habitCount, at),
});

const opts = { at: AT, habitBehind };

let n = 0;
/** A sanitized card, as parseState hands one over. */
const card = (extra = {}) => ({
  id: `c${++n}`, num: n, title: `Card ${n}`, notes: '', parentId: '',
  columnId: 'inbox', type: 'task', category: 'work', importance: '', urgency: '',
  effort: 'medium', control: 'influence', deadline: '', plan: '', planSrc: 'auto',
  habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
  tags: [], createdAt: ago(1), updatedAt: ago(1), doneAt: 0, ...extra,
});
const label = (c) => 'C-' + String(c.num).padStart(3, '0');
const labels = (d) => d.items.map((i) => i.label);
const rules = (d) => d.items.map((i) => i.rule);

// This is a unit test.
test('every surviving rule fires, and each item names its card and its reason', () => {
  n = 0;
  const overdue = card({ deadline: '2026-09-08' });
  const habit = card({ type: 'habit', habitFreq: 'daily', habitCount: 2 });
  const soon = card({ deadline: '2026-09-13' });
  const planned = card({ plan: '2026-09', planSrc: 'user' });
  // The one card in its life area, and nothing in that area has been touched
  // for 46 days — the shape of music, travel and money on the real board.
  const stale = card({ category: 'music', updatedAt: ago(46) });
  const board = [overdue, habit, soon, planned, stale];

  const d = direction(board, opts);

  assert.deepEqual(rules(d),
    ['overdue', 'habit-behind', 'due-soon', 'planned-now', 'neglected-area']);
  assert.deepEqual(labels(d), ['C-001', 'C-002', 'C-003', 'C-004', 'C-005']);
  assert.deepEqual(d.items.map((i) => i.reason), [
    'overdue by 2 days',
    'behind its cadence — 0 of 2 done today',
    'due in 3 days',
    'planned for this month, not started',
    'nothing has moved in music for 46 days',
  ]);
  assert.equal(d.note, '', 'a board with something to act on states no note');
  assert.ok(d.items.length <= DIRECTION_MAX);

  // The direction follows the board: finishing a card stops it being named for
  // the reason it was named for.
  const done = board.map((c) => (c.id === overdue.id
    ? { ...c, columnId: 'answered', doneAt: ago(1) } : c));
  assert.ok(!labels(direction(done, opts)).includes('C-001'));
});

// This is a unit test.
test('a commitment comes first, and no one reason fills the direction', () => {
  n = 0;
  // Three overdue cards: the oldest, then two of one age split by the two
  // judgements the board is rich in — which is the only thing they are used for.
  const worst = card({ deadline: '2026-09-01' });
  const dull = card({ deadline: '2026-09-05' });
  const keen = card({ deadline: '2026-09-05', importance: 'high', urgency: 'high' });
  const daily = card({ type: 'habit', habitFreq: 'daily', habitCount: 2 });
  const weekly = card({ type: 'habit', habitFreq: 'weekly', habitCount: 1 });
  const today = card({ plan: '2026-09-10', planSrc: 'user' });
  const month = card({ plan: '2026-09', planSrc: 'user' });
  const d = direction([worst, dull, keen, daily, weekly, today, month], opts);

  // Overdue before a habit before a plan somebody has not started: the
  // committed, time-bound card outranks the merely available one.
  assert.deepEqual(labels(d), ['C-001', 'C-003', 'C-004', 'C-005', 'C-006']);
  assert.deepEqual(rules(d).slice(0, 2), ['overdue', 'overdue']);

  // The third overdue card gives way rather than crowding the habits out — the
  // whole point of the per-reason cap.
  assert.ok(!labels(d).includes(label(dull)));
  assert.equal(d.items.length, DIRECTION_MAX, 'a long list of candidates is still short');
  assert.equal(new Set(rules(d)).size, 3, 'and three reasons are heard, not one');
});

// This is a unit test.
test('the direction never names something that is not on the board', () => {
  n = 0;
  const nothing = direction([], opts);
  assert.deepEqual(nothing.items, []);
  assert.equal(nothing.note, 'This board holds no cards to act on.');

  // Cards, but nothing any rule can honestly claim.
  const quiet = direction([card({ updatedAt: ago(1) })], opts);
  assert.deepEqual(quiet.items, []);
  assert.equal(quiet.note, 'Nothing is overdue, behind or planned for now.');

  // An assistant proposal is not yet on the board, and a card in the Trash has
  // left it. Both are overdue, and neither may be named.
  const proposal = card({ deadline: '2026-09-01', pending: 1 });
  const trashed = card({ deadline: '2026-09-01', deletedAt: ago(2) });
  const real = card({ deadline: '2026-09-02' });
  const d = direction([proposal, trashed, real], opts);
  assert.deepEqual(labels(d), [label(real)]);

  // Scoping is by argument and nothing else. This card belongs to another
  // board and is overdue by longer than anything above, so it would be named
  // *first* by a module that reached for state.cards instead of its own
  // argument — which is the day this assert exists to fail on.
  const otherBoard = card({ deadline: '2026-01-01' });
  const scoped = direction([real], opts);
  assert.deepEqual(labels(scoped), [label(real)]);
  assert.ok(!labels(scoped).includes(label(otherBoard)));
});

// This is a unit test.
test('encouragement cites the record or says what the record shows', () => {
  n = 0;
  const behindDaily = card({ type: 'habit', habitFreq: 'daily', habitCount: 2 });
  const behindWeekly = card({ type: 'habit', habitFreq: 'weekly', habitCount: 3 });
  // The scenario that actually fires on the real board: doneAt is three days
  // old, so there is nothing to praise. It must say so rather than reach.
  const bad = direction([behindDaily, behindWeekly], opts).praise;
  assert.equal(bad.earned, false);
  assert.deepEqual(bad.labels, [], 'nothing is cited, because nothing can be');
  assert.equal(bad.text, 'Nothing has been finished in the last 7 days, and 2 habits are behind.');

  // A Done card with no date and a Done card finished a month ago prove
  // nothing about this week — the first is the shape of the seven finished
  // cards sitting in the real board's Trash, which carry doneAt = 0.
  const undated = card({ columnId: 'answered', doneAt: 0 });
  const old = card({ columnId: 'answered', doneAt: ago(30) });
  assert.deepEqual(direction([undated, old], opts).praise.labels, []);

  // Earned: one card finished two days ago and one habit whose period is full.
  const finished = card({ columnId: 'answered', doneAt: ago(2) });
  const kept = card({
    type: 'habit', habitFreq: 'daily', habitCount: 2,
    habitHistory: { '2026-09-10': [ago(0.2), ago(0.1)] },
  });
  const good = direction([undated, old, finished, kept, behindDaily], opts).praise;
  assert.equal(good.earned, true);
  assert.deepEqual(good.labels, [label(finished), label(kept)]);
  assert.equal(good.text, '1 card finished and 1 habit kept in the last 7 days: C-005, C-006.');
});
