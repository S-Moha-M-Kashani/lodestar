// tests/merge.test.js
//
// The load-time merge. On 2026-08-22 a second machine opened this board and
// 24 cards (C-042..C-067) were archived within six minutes: initServerSync's
// rule was "a browser that already has its own board wins on load", so a
// browser holding a stale localStorage copy pushed that whole list, and the
// server's whole-board sweep archived every card the copy lacked. They came
// back only because another browser happened to push them again.
//
// The rule the merge replaces: local-wins-wholesale. The rule it installs: a
// card is never dropped because one side had not heard of it, and where both
// sides know a card the newer edit wins. Resurrecting a card an offline delete
// removed is the deliberate cost — this board's promise is that a thought is
// never lost, and archiving 24 of someone else's cards is the other direction.
//
// js/core/merge.js therefore imports nothing, like js/core/asana.js: every
// other core module reaches ui/dom.js sooner or later, and the moment this one
// does, this file stops loading under node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeCardLists } from '../js/core/merge.js';

const card = (id, updatedAt, title = id) => ({ id, num: 1, title, updatedAt });

// This is a unit test.
test('a merge never drops a card either side has, and the newer edit wins', () => {
  // The incident, in miniature: local is the stale copy, the server holds a
  // card it has never seen (`fresh`) and an edit of one it has (`shared`).
  const local = [card('a', 100, 'a local'), card('shared', 100, 'stale title')];
  const server = [card('a', 100, 'a local'), card('shared', 200, 'newer title'), card('fresh', 300)];

  const merged = mergeCardLists(local, server);

  // The card only the server had is the one the old rule archived.
  assert.deepEqual(merged.map((c) => c.id), ['a', 'shared', 'fresh']);
  assert.equal(merged.find((c) => c.id === 'shared').title, 'newer title');

  // And the other direction: an unsynced local edit is not overwritten by the
  // server's older copy, which is what the old rule got right and must keep.
  const other = mergeCardLists(
    [card('shared', 500, 'local edit')],
    [card('shared', 200, 'server copy')],
  );
  assert.equal(other.length, 1);
  assert.equal(other[0].title, 'local edit');

  // The exception that keeps deletion working at all. A local-only card is an
  // addition — unless the server has it in its Trash, in which case it was
  // deleted on the other machine and bringing it back would mean two machines
  // can never delete anything.
  const deletedElsewhere = card('gone', 100);
  const mine = card('mine', 100);
  assert.deepEqual(
    mergeCardLists([deletedElsewhere, mine], [], new Set(['gone'])).map((c) => c.id),
    ['mine'],
  );
  // A tombstone for a card the server still has live is not a deletion — the
  // live list wins, or restoring a card from the Trash would never stick.
  assert.deepEqual(
    mergeCardLists([mine], [mine], new Set(['mine'])).map((c) => c.id),
    ['mine'],
  );
});

// This is a unit test.
test('an empty side merges to the other, and the order is local then server-only', () => {
  const server = [card('s1', 10), card('s2', 20)];
  assert.deepEqual(mergeCardLists([], server).map((c) => c.id), ['s1', 's2']);

  const local = [card('l1', 10), card('l2', 20)];
  assert.deepEqual(mergeCardLists(local, []).map((c) => c.id), ['l1', 'l2']);

  // Local order is what the user has been looking at, so it leads; the cards
  // only the server knows are appended in its own order rather than sorted in,
  // because a save reorders by array index and a sort would shuffle the board.
  assert.deepEqual(
    mergeCardLists([card('l1', 10), card('l2', 20)], [card('s1', 5), card('l2', 1)])
      .map((c) => c.id),
    ['l1', 'l2', 's1'],
  );

  // A card with no timestamp on either side must not throw or vanish: absent
  // is treated as oldest, so the side that has a timestamp wins.
  const noStamp = mergeCardLists([{ id: 'x', title: 'no stamp' }], [card('x', 50, 'stamped')]);
  assert.equal(noStamp.length, 1);
  assert.equal(noStamp[0].title, 'stamped');
});
