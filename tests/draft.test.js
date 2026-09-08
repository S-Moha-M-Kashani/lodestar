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
import { blankDraft, draftFrom } from '../js/core/draft.js';

const habitCard = {
  id: 'src-id', num: 14, columnId: 'in-progress',
  title: 'Meditate', notes: 'ten minutes', type: 'habit', category: 'mind',
  importance: 'high', urgency: 'low', effort: 'low', control: 'act',
  effortSrc: 'user', controlSrc: 'ai',
  deadline: '2026-12-01', plan: '2026-11', planSrc: 'user',
  habitFreq: 'daily', habitCount: 2,
  habitTimes: [{ month: '', weekday: '', day: '', time: '07:00' },
    { month: '', weekday: '', day: '', time: '21:00' }],
  habitHistory: { '2026-09-01': ['07:04'], '2026-08-31': ['07:00', '20:58'] },
  parentId: '', tags: ['calm'], createdAt: 1, updatedAt: 2,
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
  assert.deepEqual(copy.habitTimes.map((s) => s.time), ['07:00', '21:00']);
  assert.deepEqual(copy.tags, ['calm']);

  // The record does not travel, and the source keeps its own.
  assert.deepEqual(copy.habitHistory, {});
  assert.deepEqual(Object.keys(habitCard.habitHistory).length, 2);

  // Nor does identity: the caller mints these, or the save overwrites C-014.
  for (const field of ['id', 'num', 'createdAt', 'updatedAt']) {
    assert.equal(copy[field], undefined, `${field} must not be carried`);
  }

  // The two arrays are copies, or editing the draft edits the source — and the
  // slots are copied row by row, since a shallow array copy still shares them.
  copy.habitTimes.push({ month: '', weekday: '', day: '', time: '12:00' });
  copy.habitTimes[0].time = '05:00';
  copy.tags.push('later');
  assert.deepEqual(habitCard.habitTimes.map((s) => s.time), ['07:00', '21:00']);
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

// This is a unit test. A duplicate is another part of the same piece of work,
// so a child's parent travels; a parent's children never do, and there is
// nothing to leave out — the link lives on the child, so a card holds no list
// of them and a duplicate is simply a card nobody is part of yet.
test('a duplicate keeps its parent and gains no children', () => {
  const parent = { ...habitCard, id: 'p', parentId: '' };
  const child = { ...habitCard, id: 'k', parentId: 'p' };
  assert.equal(draftFrom(child).parentId, 'p');
  assert.equal(draftFrom(parent).parentId, '');
});

