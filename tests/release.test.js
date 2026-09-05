// tests/release.test.js — the gate in front of master.
//
// Contract under test: `verdict` in scripts/release-to-master.mjs, which
// decides whether development has accumulated something worth putting on
// master. The point of the gate is the refusals, so that is what is asserted:
// a release point minted for a run of doc tweaks is worse than no release
// point, because a reader opens it expecting to find what changed for them.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, verdict, SUBSTANTIVE } from '../scripts/release-to-master.mjs';

const V = (subjects, treeChanged = true, anyway = false) =>
  verdict({ ahead: subjects.length, treeChanged, counts: classify(subjects), anyway });

// This is a unit test.
test('a release point needs something a reader actually gets', () => {
  // The refusals, which are the reason this file exists.
  assert.equal(V([]).ok, false, 'no commits is not a release');
  assert.match(V([]).why, /has not moved/);

  assert.equal(V(['feat: a thing'], false).ok, false,
    'commits that changed no file are not a release');
  assert.match(V(['feat: a thing'], false).why, /identical/);

  const chores = V(['docs: fix a typo', 'chore: bump a dep', 'test: cover a branch']);
  assert.equal(chores.ok, false, 'docs, chores and tests alone are not a release');
  assert.match(chores.why, /nothing substantive/);
  assert.match(chores.why, /chore, docs, test/, 'it must say what it did find');
  assert.ok(chores.hint, 'a refusal has to say what would change the answer');

  // And the acceptances, or a gate that refused everything would pass the above.
  assert.equal(V(['feat: a real feature', 'docs: describe it']).ok, true);
  assert.equal(V(['fix: a real bug']).ok, true);
  assert.equal(V(['perf: make it quicker']).ok, true);
  assert.equal(V(['refactor: move it somewhere honest']).ok, true);

  // A merge commit is a wrapper, never the substance: a run of merges of
  // doc branches is still a run of doc changes.
  const merged = V(["Merge branch 'docs/typo'", 'docs: fix a typo']);
  assert.equal(merged.ok, false, 'a merge does not make a chore substantive');
});

// This is a unit test.
test('--anyway overrides the judgement and neither of the two facts', () => {
  // The judgement is a heuristic over commit subjects, and a batch of
  // repository furniture is a release it cannot see. Overriding it is allowed,
  // and the reason it overrode comes back so the run can print it.
  const furniture = V(['docs: a changelog', 'chore(repo): issue forms'], true, true);
  assert.equal(furniture.ok, true);
  assert.match(furniture.overridden, /nothing substantive/,
    'an override has to say what it overrode, or it is a silent one');

  // The other two refusals are facts, not opinions: there is nothing to
  // describe, so no message could honestly describe it.
  assert.equal(V([], true, true).ok, false, 'an unmoved branch is not overridable');
  assert.equal(V(['docs: x'], false, true).ok, false, 'an identical tree is not overridable');

  // And a real release is not marked as an override it never needed.
  assert.equal(V(['feat: a real feature'], true, true).overridden, undefined);
});

// This is a unit test.
test('classify reads the conventional type, and copes without one', () => {
  const c = classify(['feat(ui): x', 'feat: y', 'fix!: z', "Merge branch 'a'", 'whatever']);
  assert.equal(c.get('feat'), 2);
  assert.equal(c.get('fix'), 1, 'a breaking-change marker is still its own type');
  assert.equal(c.get('merge'), 1);
  assert.equal(c.get('other'), 1, 'an unconventional subject is counted, never dropped');
  assert.deepEqual([...SUBSTANTIVE].sort(), ['feat', 'fix', 'perf', 'refactor']);
});
