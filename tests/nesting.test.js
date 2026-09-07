// tests/nesting.test.js
//
// The lineage: a card names at most one parent, and the link lives on the
// child. Four things here can go quietly wrong and take a card with them.
//
//   1. A broken link must blank and never refuse. A child outlives its parent
//      every time two machines merge, a half board is imported, or a parent is
//      purged — and `cleanCard` already answers that shape of question the
//      right way for `category`: an unknown value blanks, the card stays.
//      Dropping the card instead loses somebody's work to a dangling pointer.
//   2. One level, enforced simultaneously rather than in reading order. A
//      chain a→b→c must leave exactly one link standing whichever order the
//      cards arrive in, and a two-link cycle must leave none — with both cards
//      still on the board.
//   3. A generous middle. If b's own link is broken and dropped, b is not a
//      child any more, so a→b is a legitimate one-level link and must survive.
//      That is why the resolution is two phases over one snapshot and not one
//      pass over the raw claims.
//   4. A habit takes no part in either end. Its progress is the punch strip in
//      the rail; a habit hidden inside another card's fold is a habit nobody
//      punches.
//
// js/core/nesting.js imports nothing, like js/core/plan.js, js/core/draft.js
// and js/core/merge.js: the moment it reaches ui/dom.js this file stops
// loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  canHaveChildren, canHaveParent, childrenOf, parentIdVal, resolveLineage,
} from '../js/core/nesting.js';

// The fields the lineage reads and nothing else — a card here is whatever
// these five functions actually look at.
const card = (id, parentId = '', over = {}) =>
  ({ id, parentId, type: 'task', habitFreq: '', columnId: 'inbox', ...over });
const habit = (id, parentId = '') =>
  card(id, parentId, { type: 'habit', habitFreq: 'daily' });
// What the resolution left standing, as a plain map — every assert below is
// about which links survive, so comparing whole cards would only add noise.
const links = (cards) => Object.fromEntries(cards.map((c) => [c.id, c.parentId]));

// This is a unit test.
test('parentIdVal keeps an id and blanks everything that is not one', () => {
  assert.equal(parentIdVal('abc'), 'abc');
  assert.equal(parentIdVal('  abc  '), 'abc');
  assert.equal(parentIdVal(''), '');
  assert.equal(parentIdVal('   '), '');
  // Absent, and the shapes a hand-written import or a stale client can send.
  for (const v of [undefined, null, 0, 7, true, {}, [], ['abc']]) {
    assert.equal(parentIdVal(v), '', `${JSON.stringify(v)} is not an id`);
  }
});

// This is a unit test.
test('childrenOf lists a parent’s children in board order', () => {
  const cards = [card('c'), card('b', 'a'), card('a'), card('d', 'a'), card('e', 'b')];
  // Board order, not the order the parents were declared in: a card's place in
  // state.cards is what decides where it is drawn, here as everywhere else.
  assert.deepEqual(childrenOf(cards, 'a').map((c) => c.id), ['b', 'd']);
  assert.deepEqual(childrenOf(cards, 'b').map((c) => c.id), ['e']);
  assert.deepEqual(childrenOf(cards, 'c'), []);
  // A blank id is not "every top-level card": asking for the children of
  // nothing must not hand back every unparented card on the board.
  assert.deepEqual(childrenOf(cards, ''), []);
  assert.deepEqual(childrenOf(cards, undefined), []);
});

// This is a unit test.
test('one level, and a habit at neither end', () => {
  const parent = card('p');
  const child = card('k', 'p');
  const lone = card('l');
  const cards = [parent, child, lone, habit('h')];

  // A card with a parent takes no children; a card with children takes no
  // parent. Either way round the answer is the same rule read from one end.
  assert.equal(canHaveChildren(parent), true);
  assert.equal(canHaveParent(parent, cards), false);
  assert.equal(canHaveChildren(child), false);
  assert.equal(canHaveParent(child, cards), true);
  // A card at neither end can go either way.
  assert.equal(canHaveChildren(lone), true);
  assert.equal(canHaveParent(lone, cards), true);

  // A habit is neither, whatever else is true of it.
  assert.equal(canHaveChildren(habit('h')), false);
  assert.equal(canHaveParent(habit('h'), cards), false);
  // A card stamped habit but carrying no cadence is not a habit yet — the same
  // reading isHabit gives it everywhere else.
  assert.equal(canHaveParent(card('n', '', { type: 'habit' }), cards), true);
});

// A fresh board each time: resolveLineage writes the blanked link onto the
// card, so a second call over the same objects would be reading its own answer.
const board = () => [
  card('a', 'gone'),              // a parent the board does not hold
  card('b', 'b'),                 // itself
  card('c', 'd'), card('d', 'c'), // a two-link cycle
  card('e', 'f'), card('f', 'g'), card('g'), // a chain: one link may stand
  habit('h', 'g'),                // a habit cannot be a child
  card('i', 'h'),                 // nor a parent
  card('j', 'k'), card('k', 'nowhere'), // k's link breaks, so j→k is fine
];

// This is a unit test.
test('resolveLineage blanks a broken link and never loses a card', () => {
  const out = resolveLineage(board());

  // Nothing is refused, hidden or destroyed because of the link it carried.
  assert.deepEqual(out.map((c) => c.id),
    ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k']);

  assert.deepEqual(links(out), {
    a: '',                    // the parent is gone
    b: '',                    // a card is not its own parent
    c: '', d: '',             // both halves of the cycle, both cards kept
    e: '', f: 'g', g: '',     // one level: the deeper claim is the one dropped
    h: '', i: '',             // a habit at either end
    j: 'k', k: '',            // k stopped being a child, so j→k stands
  });

  // The answer cannot depend on the order the cards arrive in: same board,
  // reversed, same surviving links.
  assert.deepEqual(links(resolveLineage(board().reverse())), links(out));
});
