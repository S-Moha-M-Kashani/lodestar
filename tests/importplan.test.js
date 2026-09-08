// tests/importplan.test.js
//
// What an import file means for the board it lands on, decided before anything
// is written. "Add to the board" used to add whatever the file held, so
// re-importing yesterday's export — or a file the assistant built from the board
// it had just read — wrote a second copy of every card, and the two copies then
// drifted apart under separate ledger numbers.
//
// The rules this file pins:
//
//   - **A card IS its words.** Same title and same notes = the same thought,
//     whatever ids either side is using.
//   - Same words, and nothing else differs → the board already has it: **skip**.
//   - Same words, but something differs — a property, or which cards hang off
//     it → **substitute**: the file's version is added and the board's copy is
//     retired to the Trash. A ledger number belongs to a card for life, so the
//     replacement earns a new one and the old number leaves with the old card.
//   - No card with those words → **add**.
//   - **Children are planned before their parents**, because that is the order
//     the review reads them in and because a parent's substitution has to be
//     able to hand the whole family over to the card replacing it.
//   - **A substitution retires exactly one card.** Children are never deleted
//     by one: they move to the card that replaced their parent. So no step of
//     an import can lose a card that is not itself being replaced.
//
// js/core/importplan.js therefore imports nothing — the js/core/merge.js,
// asana.js, plan.js, nesting.js and schedule.js rule. Every other core module
// reaches ui/dom.js sooner or later, and the moment this one does, this file
// stops loading under plain node and the only cover left is an e2e.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyStep, cardWords, planImport } from '../js/core/importplan.js';

/** A sanitized card, as parseState hands one over. */
const card = (id, title, notes = '', extra = {}) => ({
  id, num: 0, title, notes, parentId: '', columnId: 'inbox', type: 'task',
  category: '', importance: '', urgency: '', effort: 'medium', control: 'influence',
  effortSrc: 'default', controlSrc: 'default', deadline: '', plan: '', planSrc: 'auto',
  habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
  tags: [], createdAt: 1, updatedAt: 1, doneAt: 0, ...extra,
});

const ids = () => { let n = 0; return () => `new-${++n}`; };
const titles = (steps) => steps.map((s) => `${s.kind}:${s.card.title}`);

// This is a unit test.
test('an import is planned card by card, children before their parents', () => {
  const board = [
    card('P', 'Plan a long weekend'),
    card('c1', 'Book the hut', '', { parentId: 'P' }),
    card('X', 'Book the dentist', 'Appointment on the 12th'),
    card('Y', 'Learn Blackbird'),
  ];
  const file = [
    // Same words as P, but the family it names is not the same one → out of date.
    card('fP', 'Plan a long weekend'),
    card('fc1', 'Book the hut', '', { parentId: 'fP' }),   // already there, untouched
    card('fc2', 'Wax the skis', '', { parentId: 'fP' }),   // new
    card('fX', 'Book the dentist', 'Appointment on the 12th'), // identical → nothing to do
    card('fY', 'Learn Blackbird', 'Bar 3 is the hard one'),    // different notes → a different card
  ];

  const { steps, skipped } = planImport(file, board, ids());

  // The child is planned first, then the parent that is being replaced. The two
  // cards the board already holds unchanged are not steps at all — a review is a
  // list of decisions, and "nothing to do" is not one.
  assert.deepEqual(titles(steps), [
    'add:Wax the skis',
    'substitute:Plan a long weekend',
    'add:Learn Blackbird',
  ]);
  assert.equal(skipped, 2);

  // The substitution names the board card it retires, which is what the review
  // shows on the left of the arrow and what History names when it is accepted.
  const swap = steps.find((s) => s.kind === 'substitute');
  assert.equal(swap.replaces.id, 'P');
  assert.notEqual(swap.card.id, swap.replaces.id,
    'the replacement is a new card, not an edit of the old one');
  assert.equal(swap.card.num, 0, 'and it earns a ledger number of its own');

  // A board with nothing on it has nothing to skip and nothing to replace.
  const empty = planImport(file, [], ids());
  assert.equal(empty.skipped, 0);
  assert.ok(empty.steps.every((s) => s.kind === 'add'));

  // The words are compared, not the bytes: a file written by hand or by the
  // assistant differs in capitals and stray whitespace, and a card that reads
  // the same is the same card.
  assert.equal(cardWords(card('a', '  BOOK the Dentist ', 'Appointment on the 12th\n')),
    cardWords(card('b', 'Book the dentist', 'Appointment on the 12th')));
});

// This is a unit test.
test('a substitution retires one card and hands its family to the replacement', () => {
  const board = [
    card('P', 'Plan a long weekend', '', { category: 'travel' }),
    card('c1', 'Book the hut', '', { parentId: 'P' }),
  ];
  const file = [
    card('fP', 'Plan a long weekend', '', { category: 'home' }),
    card('fc2', 'Wax the skis', '', { parentId: 'fP' }),
  ];

  const { steps } = planImport(file, board, ids());
  const after = steps.reduce(applyStep, board);

  // Exactly one card left the board, and it is the one the step named.
  assert.equal(after.filter((c) => c.title === 'Plan a long weekend').length, 1);
  const parent = after.find((c) => c.title === 'Plan a long weekend');
  assert.notEqual(parent.id, 'P', 'the replacement is the card on the board');
  assert.equal(parent.category, 'home', 'and it is the file’s version, not the old one');
  assert.ok(!after.some((c) => c.id === 'P'), 'the old card is off the board (soft-deleted, in the Trash)');

  // Neither child was deleted, and both hang off the replacement: the board's
  // own child moved across, and the file's new one was already pointed at it.
  const kids = after.filter((c) => c.parentId === parent.id).map((c) => c.title).sort();
  assert.deepEqual(kids, ['Book the hut', 'Wax the skis']);

  // A habit's completions are a record of what actually happened, so they ride
  // across to the replacement — a file cannot know them and must not erase them.
  const habit = (id, extra) => card(id, 'Swim', '', {
    type: 'habit', habitFreq: 'weekly', habitCount: 2, ...extra });
  const swim = planImport([habit('fh', { habitCount: 3 })],
    [habit('h', { habitHistory: { '2026-W31': [111] } })], ids());
  assert.equal(swim.steps.length, 1, 'a changed cadence makes it out of date');
  assert.deepEqual(swim.steps[0].card.habitHistory, { '2026-W31': [111] });
});

// This is a unit test.
test('every link in a plan points at a card the board will actually have', () => {
  // 1. A family the board has never seen: the child points at the parent's own
  //    new id, which the plan minted before it ordered the steps.
  const fresh = planImport(
    [card('fP', 'Ski trip'), card('fc', 'Wax the skis', '', { parentId: 'fP' })], [], ids());
  const [child, parent] = fresh.steps;
  assert.equal(child.card.parentId, parent.card.id);

  // 2. A family the board already holds, exactly as the file has it: no steps
  //    at all, and therefore no links to point anywhere.
  const board = [card('P', 'Ski trip'), card('c', 'Wax the skis', '', { parentId: 'P' })];
  const held = planImport(
    [card('fP', 'Ski trip'), card('fc', 'Wax the skis', '', { parentId: 'fP' })], board, ids());
  assert.deepEqual(held.steps, []);
  assert.equal(held.skipped, 2);

  // 3. A parent that is being replaced — the file gives it a child the board's
  //    copy does not have, which is what "different children" means. The child is
  //    planned first and points at
  //    the OLD card, because that is the parent the board has while the child is
  //    being accepted. The substitution moves it — which is why the order is not
  //    cosmetic, and why applying the steps in order is the whole test.
  const lone = [card('P', 'Ski trip')];   // the same parent, without that child
  const swapped = planImport([
    card('fP', 'Ski trip'),
    card('fc', 'Wax the skis', '', { parentId: 'fP' }),
  ], lone, ids());
  assert.deepEqual(titles(swapped.steps), ['add:Wax the skis', 'substitute:Ski trip']);
  assert.equal(swapped.steps[0].card.parentId, 'P');

  const after = swapped.steps.reduce(applyStep, lone);
  const newParent = after.find((c) => c.title === 'Ski trip');
  assert.equal(after.find((c) => c.title === 'Wax the skis').parentId, newParent.id);

  // 4. Accepting only some of the steps must leave nothing dangling either: a
  //    reviewer who keeps the old parent and takes only the new child gets a
  //    child hanging off the card that is still there.
  const partial = applyStep(lone, swapped.steps[0]);
  assert.equal(partial.find((c) => c.title === 'Wax the skis').parentId, 'P');
  assert.ok(partial.some((c) => c.id === 'P'), 'the old parent stays when its step is skipped');
});
