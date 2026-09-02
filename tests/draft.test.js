// tests/draft.test.js
//
// The draft: a card-shaped object the dialog holds before any card exists.
// Two things here can go quietly wrong and take a promise with them.
//
//   1. What a duplicate carries. Cadence is the design and travels; the
//      completion history is the record and must not — a copy that inherits a
//      year of ticks is a record of things that did not happen, the same rule
//      that keeps the assistant from ticking a habit. Identity must not travel
//      either: a draft holding the source's id or ledger number would overwrite
//      it on save instead of joining it.
//   2. When the settings fold opens itself. Two fields lie if you read their
//      value: effort and control *always* hold one (the scale's midpoint stands
//      in until somebody judges), and a plan that is merely following the
//      deadline has been resolved to it. Reading either as "the user set this"
//      leaves the fold open on every card ever made, which is the feature
//      failing silently rather than loudly.
//
// js/core/draft.js imports nothing, like js/core/plan.js and js/core/merge.js:
// the moment it reaches ui/dom.js this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { blankDraft, cardHasDetails, draftFrom } from '../js/core/draft.js';

const habitCard = {
  id: 'src-id', num: 14, columnId: 'in-progress',
  title: 'Meditate', notes: 'ten minutes', type: 'habit', category: 'mind',
  importance: 'high', urgency: 'low', effort: 'low', control: 'act',
  effortSrc: 'user', controlSrc: 'ai',
  deadline: '2026-12-01', plan: '2026-11', planSrc: 'user',
  habitFreq: 'daily', habitCount: 2, habitTimes: ['07:00', '21:00'],
  habitHistory: { '2026-09-01': ['07:04'], '2026-08-31': ['07:00', '20:58'] },
  tags: ['calm'], createdAt: 1, updatedAt: 2,
};

// This is a unit test.
test('a duplicate carries the card and not its identity or its record', () => {
  const copy = draftFrom(habitCard);

  // Everything a similar card needs.
  for (const field of ['columnId', 'title', 'notes', 'type', 'category',
    'importance', 'urgency', 'effort', 'control', 'effortSrc', 'controlSrc',
    'deadline', 'plan', 'planSrc', 'habitFreq', 'habitCount']) {
    assert.equal(copy[field], habitCard[field], `${field} should be carried`);
  }
  assert.deepEqual(copy.habitTimes, ['07:00', '21:00']);
  assert.deepEqual(copy.tags, ['calm']);

  // The record does not travel, and the source keeps its own.
  assert.deepEqual(copy.habitHistory, {});
  assert.deepEqual(Object.keys(habitCard.habitHistory).length, 2);

  // Nor does identity: the caller mints these, or the save overwrites C-014.
  for (const field of ['id', 'num', 'createdAt', 'updatedAt']) {
    assert.equal(copy[field], undefined, `${field} must not be carried`);
  }

  // The two arrays are copies, or editing the draft edits the source.
  copy.habitTimes.push('12:00');
  copy.tags.push('later');
  assert.deepEqual(habitCard.habitTimes, ['07:00', '21:00']);
  assert.deepEqual(habitCard.tags, ['calm']);

  // A blank draft is the same shape, and a habit gets the cadence the dialog
  // defaults to rather than a habit with no cadence at all.
  assert.equal(blankDraft({ type: 'habit', category: 'health' }).habitFreq, 'daily');
  assert.equal(blankDraft({ type: 'task', category: '' }).habitFreq, '');
  assert.equal(blankDraft().type, 'question');
  assert.deepEqual(blankDraft().habitHistory, {});
  // An unfiltered board reads '' on both filters, not undefined, so a default
  // parameter would not fire and the picker would open with no stamp selected.
  assert.equal(blankDraft({ type: '', category: '' }).type, 'question');
});

// This is a unit test.
test('the fold opens only for a setting somebody actually chose', () => {
  // The standing defaults every card is created with: nothing here was decided.
  const bare = {
    columnId: 'inbox', title: 'A thought', notes: '', type: 'question',
    category: '', importance: '', urgency: '',
    effort: 'medium', control: 'influence', effortSrc: 'default', controlSrc: 'default',
    deadline: '', plan: '', planSrc: 'auto',
    habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {}, tags: [],
  };
  assert.equal(cardHasDetails(bare), false);

  // Trap one: effort and control always hold a value. Only who set it counts.
  assert.equal(cardHasDetails({ ...bare, effort: 'high' }), false);
  assert.equal(cardHasDetails({ ...bare, control: 'none' }), false);
  assert.equal(cardHasDetails({ ...bare, effort: 'high', effortSrc: 'user' }), true);
  assert.equal(cardHasDetails({ ...bare, control: 'none', controlSrc: 'ai' }), true);

  // Trap two: an auto plan has been resolved to the deadline, so a dated card
  // carries a plan it was never asked about. The deadline is the detail there.
  assert.equal(cardHasDetails({ ...bare, plan: '2026-12-01', planSrc: 'auto' }), false);
  assert.equal(cardHasDetails({ ...bare, plan: '2027', planSrc: 'user' }), true);

  // The plain ones.
  assert.equal(cardHasDetails({ ...bare, category: 'work' }), true);
  assert.equal(cardHasDetails({ ...bare, importance: 'high' }), true);
  assert.equal(cardHasDetails({ ...bare, urgency: 'low' }), true);
  assert.equal(cardHasDetails({ ...bare, deadline: '2026-12-01' }), true);
  assert.equal(cardHasDetails({ ...bare, tags: ['planning'] }), true);

  // A duplicate of a filled card opens showing what it copied.
  assert.equal(cardHasDetails(draftFrom(habitCard)), true);
});
