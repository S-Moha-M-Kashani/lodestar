#!/usr/bin/env node
// Copy the tracked hooks into .git/hooks.
//
// They are copied rather than pointed at with core.hooksPath, because the same
// hooks have to hold whichever branch is checked out — including master, whose
// tree does not carry scripts/git-hooks at all. A hooksPath into the working
// tree would simply vanish there, which is the one moment the guards matter
// most.
//
// pre-commit is installed under three names: git asks pre-commit for an
// ordinary commit and pre-merge-commit for `git merge`, and the same refusal
// answers both.

import { copyFileSync, chmodSync, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'scripts', 'git-hooks');
const DST = join(ROOT, '.git', 'hooks');

const HOOKS = {
  'pre-push': ['pre-push'],
  'reference-transaction': ['reference-transaction'],
  'commit-msg': ['commit-msg'],
  'pre-commit': ['pre-commit', 'pre-merge-commit'],
};

if (!existsSync(DST)) mkdirSync(DST, { recursive: true });

for (const [src, names] of Object.entries(HOOKS)) {
  const from = join(SRC, src);
  if (!existsSync(from)) {
    console.error(`missing: scripts/git-hooks/${src}`);
    process.exit(1);
  }
  for (const name of names) {
    const to = join(DST, name);
    copyFileSync(from, to);
    chmodSync(to, 0o755);
    console.log(`installed .git/hooks/${name}`);
  }
}
