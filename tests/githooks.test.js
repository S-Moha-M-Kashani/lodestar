// tests/githooks.test.js
//
// development is the full copy of this project — README-Development.md,
// ROADMAP-Development.md, 125.md and docs/report/ live there and nowhere else,
// so master cannot restore it the way it could restore any other branch. The
// reference-transaction hook exists to make `git branch -D development`
// impossible, and a protection nobody has watched refuse anything is not a
// protection.
//
// The real repository cannot be the subject here: development is checked out,
// which makes git refuse the delete on its own and would pass this test with a
// broken hook. So each case builds a throwaway repo where development is *not*
// checked out, which is the state the hook is the only thing standing in.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, copyFileSync, chmodSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HOOK = join(ROOT, 'scripts', 'git-hooks', 'reference-transaction');

// Returns {ok, output} instead of throwing: a refusal is the expected result in
// half these calls, and its message is what we assert on.
function git(cwd, ...args) {
  try {
    return { ok: true, output: execFileSync('git', args, { cwd, encoding: 'utf8', stdio: 'pipe' }) };
  } catch (e) {
    return { ok: false, output: `${e.stdout ?? ''}${e.stderr ?? ''}` };
  }
}

function repoWithHook() {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-hook-'));
  git(dir, 'init', '-q', '-b', 'master');
  git(dir, 'config', 'user.email', 'test@example.com');
  git(dir, 'config', 'user.name', 'test');
  git(dir, 'commit', '-q', '--allow-empty', '-m', 'root');
  mkdirSync(join(dir, '.git', 'hooks'), { recursive: true });
  copyFileSync(HOOK, join(dir, '.git', 'hooks', 'reference-transaction'));
  chmodSync(join(dir, '.git', 'hooks', 'reference-transaction'), 0o755);
  // development exists but master is checked out, so git itself has no
  // objection to deleting it — only the hook does.
  git(dir, 'branch', 'development');
  return dir;
}

// This is an integration test (spawns real git against a temporary repository).
test('the hook refuses to delete development, and nothing else', (t) => {
  const dir = repoWithHook();
  t.after(() => rmSync(dir, { recursive: true, force: true }));

  const deleted = git(dir, 'branch', '-D', 'development');
  assert.equal(deleted.ok, false, 'git branch -D development must fail');
  assert.match(deleted.output, /development cannot be deleted/);
  assert.equal(git(dir, 'rev-parse', '--verify', 'refs/heads/development').ok, true,
    'development must still exist after the refusal');

  // update-ref -d is the path that skips branch-level guards; the hook sits
  // below it, which is why it is the interesting case rather than a duplicate.
  const forced = git(dir, 'update-ref', '-d', 'refs/heads/development');
  assert.equal(forced.ok, false, 'update-ref -d must be refused too');
  assert.equal(git(dir, 'rev-parse', '--verify', 'refs/heads/development').ok, true);

  // A hook that refused everything would pass every assertion above, so prove
  // ordinary ref work is untouched: another branch deletes, and commits land.
  git(dir, 'branch', 'feature/other');
  assert.equal(git(dir, 'branch', '-D', 'feature/other').ok, true,
    'other branches must still be deletable');
  assert.equal(git(dir, 'commit', '-q', '--allow-empty', '-m', 'second').ok, true,
    'committing must still work — the hook runs on every ref transaction');
});

// This is an integration test (real git, a temporary repo and a bare remote).
test('the hook pushes master and version tags, and refuses everything else', (t) => {
  const dir = repoWithHook();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const remote = mkdtempSync(join(tmpdir(), 'lodestar-remote-'));
  t.after(() => rmSync(remote, { recursive: true, force: true }));
  git(remote, 'init', '-q', '--bare');
  git(dir, 'remote', 'add', 'origin', remote);
  copyFileSync(join(ROOT, 'scripts', 'git-hooks', 'pre-push'), join(dir, '.git', 'hooks', 'pre-push'));
  chmodSync(join(dir, '.git', 'hooks', 'pre-push'), 0o755);

  const pushed = git(dir, 'push', 'origin', 'development');
  assert.equal(pushed.ok, false, 'pushing development must fail');
  assert.match(pushed.output, /only master is published/);

  // The same content under another name is the same disclosure, so the hook
  // reads the local ref rather than what it would be called on the remote.
  assert.equal(git(dir, 'push', 'origin', 'development:master').ok, false,
    'pushing development under another name must fail too');

  // The allowlist is the point: a branch nobody had thought of when the hook
  // was written is refused by default, where a blocklist would wave it through.
  git(dir, 'branch', 'feature/whatever');
  assert.equal(git(dir, 'push', 'origin', 'feature/whatever').ok, false,
    'an unrelated branch must be refused without being named anywhere');

  // An archive tag points into history that is deliberately unpublished, so
  // only version tags are let through.
  git(dir, 'tag', 'archive/something');
  assert.equal(git(dir, 'push', 'origin', 'archive/something').ok, false,
    'a non-version tag must be refused');

  assert.equal(git(dir, 'push', 'origin', 'master').ok, true,
    'master is the branch that gets published — it must still push');
  git(dir, 'tag', 'v9.9');
  assert.equal(git(dir, 'push', 'origin', 'v9.9').ok, true,
    'a version tag travels with the release it names');

  const onRemote = git(remote, 'branch', '--format=%(refname:short)').output;
  assert.match(onRemote, /master/);
  assert.doesNotMatch(onRemote, /development/, 'development must not exist on the remote');
  assert.doesNotMatch(onRemote, /feature/, 'no feature branch may reach the remote');
});

// This is a configuration invariant test.
test('the repository has the hooks installed, and tracks their source', () => {
  for (const hook of ['reference-transaction', 'pre-push']) {
    assert.equal(git(ROOT, 'ls-files', '--error-unmatch', `scripts/git-hooks/${hook}`).ok,
      true, `${hook} must be tracked, or a fresh clone cannot restore it`);
  }
  // The installed copy lives in .git/hooks so it applies whichever branch is
  // checked out — including master, which does not carry the tracked file.
  assert.equal(git(ROOT, 'rev-parse', '--verify', 'refs/heads/development').ok, true);
});
