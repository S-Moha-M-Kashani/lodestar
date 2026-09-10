// tests/duplicates.test.js
//
// Which cards on a board are the same thought twice, and which copy of one
// survives the clean-up.
//
// The rules this file pins:
//
//   - **A card IS its words.** Title and notes, trimmed and lower-cased —
//     `cardWords`, the identity rule the import review already decides by,
//     reused rather than restated. Nothing else makes two cards duplicates,
//     and nothing else keeps two cards with the same words from being one.
//   - **The survivor is the copy furthest along the flow** — Done, then In
//     Progress, then Inbox — because the copy that has been worked on is the
//     one holding real state, and a clean-up that quietly reopens a started
//     card is worse than the duplicate it removed.
//   - Where the leading copies share a column, the **newer** one survives, and
//     the ledger number breaks a tie `createdAt` cannot: two cards that arrived
//     in one import share a `createdAt` to the millisecond, and a rule that can
//     answer "either" deletes a different card every time it runs.
//   - **A habit's completions are the union of both copies'** — a record of
//     things that actually happened, which the survivor inherits rather than
//     loses. The mirror of the Duplicate rule, which copies none: there the
//     copy is a different habit, here it is provably the same one.
//
// js/core/duplicates.js therefore imports two leaves and nothing else — the
// js/core/importplan.js, merge.js, asana.js, plan.js, nesting.js, schedule.js
// and timespan.js rule. The moment it reaches ui/, this file stops loading
// under plain node and the whole decision table is an e2e-only surface.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planDedupe } from '../js/core/duplicates.js';

/** A sanitized card, as the board holds one. */
const card = (id, title, notes = '', extra = {}) => ({
  id, num: Number(String(id).replace(/\D/g, '')) || 0, title, notes,
  parentId: '', columnId: 'inbox', type: 'task', category: '',
  tags: [], habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
  createdAt: 1000, updatedAt: 1000, doneAt: 0, ...extra,
});

const ids = (groups) => groups.map((g) => [g.keep.id, ...g.drop.map((c) => c.id)]);

// This is a unit test.
test('two cards are duplicates when their words match, and only then', () => {
  const cards = [
    card('1', 'Book the flight ', 'Zurich → Lisbon', { columnId: 'inbox', tags: ['travel'] }),
    // Same words once the capitals and the stray whitespace are taken out, and
    // filed differently in every way a card can be filed. Still one thought.
    card('2', 'book the flight', 'zurich → lisbon', { columnId: 'in-progress', tags: [], category: 'travel' }),
    // Same title, different notes: two cards, and neither is offered.
    card('3', 'Call the landlord', 'About the boiler'),
    card('4', 'Call the landlord', 'About the deposit'),
    // Alone.
    card('5', 'Learn Blackbird'),
  ];

  assert.deepEqual(ids(planDedupe(cards)), [['2', '1']]);
  assert.deepEqual(planDedupe([card('1', 'Alone')]), [], 'one card is never a duplicate of itself');
  assert.deepEqual(planDedupe([]), []);
});

// This is a unit test.
test('the survivor is the copy furthest along, then the newest, then the higher number', () => {
  // Done beats In Progress beats Inbox, whatever order the board holds them in.
  const flow = planDedupe([
    card('a1', 'Renew the passport', '', { columnId: 'answered', doneAt: 900 }),
    card('a2', 'Renew the passport', '', { columnId: 'in-progress' }),
    card('a3', 'Renew the passport', '', { columnId: 'inbox' }),
  ]);
  assert.deepEqual(ids(flow), [['a1', 'a2', 'a3']], 'three copies leave one, and it is the finished one');
  assert.equal(flow[0].keep.doneAt, 900, 'so the day it was finished survives with it');

  // C-009 on the owner's own board: the Inbox twin was the newer card, and
  // deleting by age alone would have reopened work already under way.
  const started = planDedupe([
    card('b1', 'Sort the insurance', '', { columnId: 'in-progress', createdAt: 100 }),
    card('b2', 'Sort the insurance', '', { columnId: 'inbox', createdAt: 200 }),
  ]);
  assert.deepEqual(ids(started), [['b1', 'b2']]);

  // Same column: the newer one, and where an import copied the timestamp
  // across, the ledger number is what makes the answer the same every run.
  const same = planDedupe([
    card('c1', 'Pay the tax bill', '', { createdAt: 100 }),
    card('c2', 'Pay the tax bill', '', { createdAt: 200 }),
  ]);
  assert.deepEqual(ids(same), [['c2', 'c1']]);

  const tied = planDedupe([
    card('20', 'Wax the skis', '', { createdAt: 100 }),
    card('2', 'Wax the skis', '', { createdAt: 100 }),
  ]);
  assert.deepEqual(ids(tied), [['20', '2']]);
});

// This is a unit test.
test('a retired habit hands its completions to the survivor', () => {
  const kept = card('h2', 'Practise scales', '', {
    type: 'habit', habitFreq: 'daily', columnId: 'inbox', createdAt: 200,
    habitHistory: { '2026-09-08': [10], '2026-09-09': [20] },
  });
  const gone = card('h1', 'Practise scales', '', {
    type: 'habit', habitFreq: 'daily', columnId: 'inbox', createdAt: 100,
    habitHistory: { '2026-09-07': [5], '2026-09-09': [20, 30] },
  });

  const [group] = planDedupe([kept, gone]);
  assert.equal(group.keep.id, 'h2');
  assert.deepEqual(group.keep.habitHistory, {
    '2026-09-07': [5],        // only the retired copy recorded it
    '2026-09-08': [10],       // only the survivor did
    '2026-09-09': [20, 30],   // both, and the one they share is not counted twice
  });
  assert.deepEqual(kept.habitHistory, { '2026-09-08': [10], '2026-09-09': [20] },
    'the decision writes nothing back into the board it read');
});
