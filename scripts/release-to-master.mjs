#!/usr/bin/env node
// Build a release point on master, but only when there is one worth building.
//
// master is a ladder over development: every commit on it is a merge whose two
// parents are the previous release point and the development commit it
// summarises, and whose tree is that commit's tree exactly. That shape is the
// whole design — it puts the branches side by side in the graph, and it means
// any point on master can be checked out and run.
//
// This script is the only thing that writes there. Before it does, it answers
// the question that is easy to skip: is there actually a presentable set of
// changes since the last release point? A run of typo fixes and doc tweaks is
// not a release, and saying so is more useful than minting a version for it.
//
//   node scripts/release-to-master.mjs --check      report and stop
//   node scripts/release-to-master.mjs -F msg.txt   build the release point
//   node scripts/release-to-master.mjs --anyway …   release past the judgement
//
// Exit codes: 0 releasable (or released), 2 nothing worth releasing, 1 error.

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

// Types that move the product forward. A release made of anything else is
// housekeeping wearing a version number: the reader opens it expecting to see
// what changed for them and finds a lint pass.
export const SUBSTANTIVE = new Set(['feat', 'fix', 'perf', 'refactor']);

export function classify(subjects) {
  const counts = new Map();
  for (const s of subjects) {
    const m = /^(\w+)(\([^)]*\))?!?:/.exec(s);
    const type = m ? m[1] : (s.startsWith('Merge ') ? 'merge' : 'other');
    counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  return counts;
}

// The decision, kept apart from git so it can be read and tested on its own.
// `anyway` overrides the judgement and nothing else. The first two refusals
// stay absolute, because they are facts rather than opinions: a release point
// over an unmoved branch has no changes behind it, and one whose tree matches
// the last is the same files under a new number — neither is describable in any
// words, so an override there could only mint a version whose own note has to
// lie. The third is a heuristic reading commit subjects, and it is wrong in one
// direction: a batch of repository furniture — a changelog, a security policy,
// the forms an issue is filed on — is something a reader gets and carries no
// `feat` anywhere. Overriding is loud, never silent: the caller types the flag
// and the run prints which sentence it was used on.
export function verdict({ ahead, treeChanged, counts, anyway = false }) {
  if (ahead === 0) {
    return { ok: false, why: 'development has not moved since the last release point' };
  }
  if (!treeChanged) {
    return { ok: false, why: 'the files are identical to the last release point' };
  }
  const substantive = [...counts].filter(([t]) => SUBSTANTIVE.has(t));
  if (substantive.length === 0) {
    const kinds = [...counts.keys()].filter((k) => k !== 'merge').sort().join(', ') || 'none';
    const why = `nothing substantive since the last release point — only ${kinds}`;
    if (anyway) return { ok: true, substantive: [], overridden: why };
    return {
      ok: false,
      why,
      hint: 'a release point should be something a reader gets: a feature, a fix, '
        + 'a real refactor. Docs and chores ride along with the next one. '
        + 'Pass --anyway if this is a batch the types cannot see.',
    };
  }
  return { ok: true, substantive };
}

function git(...args) {
  return execFileSync('git', args, { encoding: 'utf8' }).trim();
}

function main(argv) {
  const check = argv.includes('--check');
  const anyway = argv.includes('--anyway');
  const fi = argv.indexOf('-F');
  const mi = argv.indexOf('-m');
  let message = null;
  if (fi !== -1) message = readFileSync(argv[fi + 1], 'utf8');
  else if (mi !== -1) message = argv[mi + 1];

  if (git('status', '--porcelain')) {
    console.error('refused: the working tree is not clean.');
    return 1;
  }

  const dev = git('rev-parse', 'development');
  let base = null;
  let masterTip = null;
  try {
    masterTip = git('rev-parse', 'master');
    const parents = git('rev-list', '--parents', '-1', masterTip).split(/\s+/).slice(1);
    if (parents.length === 2) base = parents[1];
    else if (parents.length === 1) base = parents[0];
    // The invariant that makes a release point runnable, checked rather than
    // trusted: its tree has to be the tree of the development commit it names.
    if (base && git('rev-parse', `${masterTip}^{tree}`) !== git('rev-parse', `${base}^{tree}`)) {
      console.error('refused: master\'s tip does not carry its development commit\'s tree.');
      console.error('The ladder is broken; fix that before adding to it.');
      return 1;
    }
  } catch {
    masterTip = null; // no master yet: this is the first release point
  }

  const range = base ? `${base}..${dev}` : dev;
  const subjects = git('log', '--format=%s', range).split('\n').filter(Boolean);
  const treeChanged = !masterTip
    || git('rev-parse', `${masterTip}^{tree}`) !== git('rev-parse', `${dev}^{tree}`);
  const counts = classify(subjects);
  const v = verdict({ ahead: subjects.length, treeChanged, counts, anyway });

  console.log(`development is ${subjects.length} commit(s) past the last release point.`);
  if (counts.size) {
    console.log('  ' + [...counts].sort((a, b) => b[1] - a[1])
      .map(([t, n]) => `${t}: ${n}`).join('   '));
  }

  if (!v.ok) {
    console.log('');
    console.log(`Not worth a release point: ${v.why}.`);
    if (v.hint) console.log(v.hint);
    return 2;
  }

  console.log('');
  if (v.overridden) {
    console.log(`--anyway: releasing past "${v.overridden}".`);
    console.log('Say in the message what a reader gets, since the types do not.');
  } else {
    console.log('Worth a release point. It would carry:');
    for (const s of subjects.filter((s) => SUBSTANTIVE.has((/^(\w+)/.exec(s) ?? [])[1]))) {
      console.log(`  ${s}`);
    }
  }

  if (check) {
    console.log('');
    console.log('--check only; nothing was written.');
    return 0;
  }
  if (!message || !message.trim()) {
    console.error('');
    console.error('refused: a release point needs a message. Pass -F <file> or -m "<text>".');
    console.error('Describe the feature, not the journey: what it now makes possible.');
    return 1;
  }

  const tree = git('rev-parse', `${dev}^{tree}`);
  const args = ['commit-tree', tree];
  if (masterTip) args.push('-p', masterTip);
  args.push('-p', dev);
  const sha = execFileSync('git', args, { encoding: 'utf8', input: message }).trim();
  git('update-ref', 'refs/heads/master', sha, masterTip ?? '');
  console.log('');
  console.log(`released ${sha.slice(0, 9)} on master, over development ${dev.slice(0, 9)}`);
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main(process.argv.slice(2)));
}
