// tests/hygiene.test.js — the gate in front of a public push.
//
// Contract under test: scripts/release-hygiene.mjs, which answers one
// question — "is every ref we are about to publish free of private data?" —
// and refuses the release when it is not.
//
// Two kinds of private data, two kinds of check:
//
//   paths     every path name in every commit reachable from the named refs,
//             against a prohibited list (databases/, any SQLite file and its
//             WAL sidecars, backups/). Reachable, not "in the current tree":
//             deleting a database in a later commit leaves the blob sitting in
//             history where anyone who clones can walk to it, which is the
//             whole reason this file exists.
//
//   identifiers  a real person's name in source, tests, docs or commit
//             messages. The names are never a literal in this repository —
//             they arrive in LODESTAR_PRIVATE_NAMES at run time, and the
//             report names the file, never the match, or the check would
//             publish the thing it exists to keep unpublished.
//
// Fail-closed both ways: no names supplied is a refusal, not a silent skip,
// because a release gate that quietly checked half of what it claims is worse
// than no gate at all.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { prohibitedPath } from '../scripts/release-hygiene.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CHECK = join(ROOT, 'scripts', 'release-hygiene.mjs');

// A stand-in identifier, minted per run rather than written down. It has to be
// absent from the repository for the real-repo case below to mean anything —
// and a constant here could not be, because this file is one of the files the
// scan reads. Written as a literal it passed while the file was still
// uncommitted and went red the moment it was released: the gate found its own
// test. So the value never exists as source text.
const FAKE_NAME = `Q${randomUUID().replace(/-/g, '')}`;

function git(cwd, ...args) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: 'pipe' });
}

// Returns {ok, output}: a refusal is the expected result in half these calls,
// and what it printed is exactly what we assert on.
function hygiene({ cwd, refs, names }) {
  const env = { ...process.env };
  if (names === undefined) delete env.LODESTAR_PRIVATE_NAMES;
  else env.LODESTAR_PRIVATE_NAMES = names;
  try {
    return {
      ok: true,
      output: execFileSync(process.execPath, [CHECK, '--repo', cwd, ...refs],
        { encoding: 'utf8', stdio: 'pipe', env }),
    };
  } catch (e) {
    return { ok: false, output: `${e.stdout ?? ''}${e.stderr ?? ''}` };
  }
}

// This is a unit test.
test('the prohibited-path rule flags private data and nothing else', () => {
  // Every shape the incident actually produced.
  for (const path of [
    'databases/real/board.db',
    'databases/test/chroma-data-3001/abc/data_level0.bin',
    'databases',
    'board.db',
    'databases/real/board.db-wal',
    'databases/real/board.db-shm',
    'databases/real/board.db-journal',
    'backups/db/board-20260901.db',
    'backups/json/board-main-20260901.json',
  ]) {
    assert.ok(prohibitedPath(path), `${path} must be prohibited`);
  }

  // And the near misses, or a rule that refused everything would pass the
  // above. `dbbackend.test.js` and `db-location.mjs` are the two that would
  // really be caught by a careless /db/ pattern; `docs/` names the subject of
  // the rule without being an instance of it.
  for (const path of [
    'scripts/db-location.mjs',
    'tests/dbbackend.test.js',
    'tests/databases.test.js',
    'js/core/boards.js',
    'docs/security.md',
    'brain/src/lodestar_brain/retrieval/cards.py',
  ]) {
    assert.equal(prohibitedPath(path), null, `${path} must be allowed`);
  }
});

// This is an integration test (spawns real git against a temporary repository).
test('a database and a private name are caught in history, not just in the tree', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-hygiene-'));
  try {
    git(dir, 'init', '-q', '-b', 'master');
    git(dir, 'config', 'user.email', 'test@example.com');
    git(dir, 'config', 'user.name', 'test');

    mkdirSync(join(dir, 'databases'), { recursive: true });
    writeFileSync(join(dir, 'databases', 'board.db'), 'SQLite format 3\0rows');
    writeFileSync(join(dir, 'notes.md'), `A card about ${FAKE_NAME}.\n`);
    git(dir, 'add', '-A', '-f');
    git(dir, 'commit', '-q', '-m', 'the incident');

    const caught = hygiene({ cwd: dir, refs: ['master'], names: FAKE_NAME });
    assert.equal(caught.ok, false, 'a ref holding private data must not pass');
    assert.match(caught.output, /databases\/board\.db/,
      'the report must name the offending path');
    assert.match(caught.output, /notes\.md/,
      'the report must name the file holding the identifier');
    assert.match(caught.output, /master/, 'the report must name the ref');
    assert.ok(!caught.output.includes(FAKE_NAME),
      'the report must never print the identifier it was given — a gate that '
      + 'echoes the private name has published it into every CI log');

    // Deleting them in a later commit is what a careless remediation does,
    // and it changes nothing: the blobs are still reachable from master.
    rmSync(join(dir, 'databases'), { recursive: true, force: true });
    writeFileSync(join(dir, 'notes.md'), 'A card about somebody.\n');
    git(dir, 'add', '-A');
    git(dir, 'commit', '-q', '-m', 'tidy up');
    assert.equal(hygiene({ cwd: dir, refs: ['master'], names: FAKE_NAME }).ok,
      false, 'deleting private data in a later commit must not pass the gate');

    // Only a history that never held them passes — which is what the rewrite
    // in this change produces.
    git(dir, 'checkout', '-q', '--orphan', 'clean');
    git(dir, 'rm', '-r', '-q', '--cached', '.');
    rmSync(join(dir, 'notes.md'), { force: true });
    writeFileSync(join(dir, 'README.md'), 'A board.\n');
    git(dir, 'add', '-A');
    git(dir, 'commit', '-q', '-m', 'a clean start');
    const clean = hygiene({ cwd: dir, refs: ['clean'], names: FAKE_NAME });
    assert.equal(clean.ok, true, `a rewritten ref must pass:\n${clean.output}`);
    assert.match(clean.output, /clean/, 'a pass must record which refs it checked');

    // A mistyped tag is the likeliest way an operator calls this wrongly, and
    // a stack trace on the way to a release reads like the check ran and
    // something else broke.
    const typo = hygiene({ cwd: dir, refs: ['v9.9.9'], names: FAKE_NAME });
    assert.equal(typo.ok, false, 'an unknown ref must refuse');
    assert.match(typo.output, /is not a ref/, 'it must say the ref is unknown');
    assert.doesNotMatch(typo.output, /at .*release-hygiene\.mjs/,
      'it must refuse cleanly, not with a stack trace');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// This is a configuration invariant.
test('the approved release set of this repository is publishable', () => {
  // No names supplied is a refusal. The identifier half of the gate is the
  // half that cannot be inferred from the repository, so forgetting it has to
  // stop the release rather than report a green it never measured.
  const silent = hygiene({ cwd: ROOT, refs: ['master'], names: undefined });
  assert.equal(silent.ok, false, 'a run with no identifiers must refuse');
  assert.match(silent.output, /LODESTAR_PRIVATE_NAMES/,
    'the refusal must say what to supply');

  // master and the v* tags reachable from it are the entire public release
  // set; `main`, `v1.1` and every `archive/*` tag are school-submission or
  // local-only refs and are deliberately not in it.
  const tags = git(ROOT, 'tag', '--list', 'v*', '--merged', 'master')
    .split('\n').map((t) => t.trim()).filter(Boolean);
  assert.ok(tags.length > 0, 'master must carry version tags to publish');

  const run = hygiene({ cwd: ROOT, refs: ['master', ...tags], names: FAKE_NAME });
  assert.equal(run.ok, true,
    `the approved release set must be free of private data:\n${run.output}`);
});
