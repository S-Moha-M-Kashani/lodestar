#!/usr/bin/env node
// scripts/release-hygiene.mjs — the gate in front of a public push.
//
//   node scripts/release-hygiene.mjs [--repo <dir>] <ref>...
//   LODESTAR_PRIVATE_NAMES='Name,نام' node scripts/release-hygiene.mjs master v1.4.0
//
// Answers one question about the refs it is given — "is everything a stranger
// could clone from these free of private data?" — and exits non-zero when it
// is not. Two kinds of private data, two kinds of check.
//
// **Paths, over history rather than the current tree.** Deleting a database in
// a later commit changes nothing: the blob stays reachable from the branch and
// anyone who clones can walk to it. So the scan is `git rev-list --objects`
// per ref, which enumerates every path name in every commit that ref can
// reach. That is the whole reason this file exists — the tidy-up commit is
// what people do instead of a rewrite, and it looks identical from the outside.
//
// **Identifiers, supplied at run time and never printed.** A real person's
// name must not be a literal in a public repository, and a checker that
// hard-coded one would only have moved the problem into itself. The names come
// from LODESTAR_PRIVATE_NAMES, and a finding reports the file, never the
// match, or the gate would publish the name into every log that ran it.
//
// Fail-closed on both: no names supplied is a refusal (exit 2), not a silent
// skip. A gate that quietly checked half of what it claims is worse than none,
// because the green it prints is the thing the operator acts on.
import { execFileSync } from 'node:child_process';

// A path segment, not a substring: `databases/` is private wholesale, while
// `tests/databases.test.js` and `scripts/db-location.mjs` are the code that
// manages it and belong in the open.
const PRIVATE_DIRS = new Set(['databases', 'backups']);
// SQLite and the two sidecars WAL adds; `.db-journal` is the pre-WAL one.
const DB_FILE = /\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$/i;

/** Why this path may not be published, or null if it may. */
export function prohibitedPath(path) {
  const segments = path.split('/');
  for (const segment of segments) {
    if (PRIVATE_DIRS.has(segment)) return `under ${segment}/`;
  }
  if (DB_FILE.test(segments[segments.length - 1])) return 'a database file';
  return null;
}

/** The names to look for, from the operator's environment. */
export function privateNames(env = process.env) {
  return (env.LODESTAR_PRIVATE_NAMES ?? '')
    .split(/[,\n]/).map((n) => n.trim()).filter(Boolean);
}

const git = (cwd, ...args) =>
  execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 1 << 28 });

// `git grep` takes many tree-ishes but the command line is not unbounded.
const BATCH = 40;
const lines = (text) => text.split('\n').map((l) => l.trim()).filter(Boolean);

/** Every path name in every commit reachable from `ref`. */
function historyPaths(cwd, ref) {
  const out = new Set();
  for (const line of lines(git(cwd, 'rev-list', '--objects', ref))) {
    const space = line.indexOf(' ');
    if (space > 0) out.add(line.slice(space + 1));
  }
  return out;
}

/** Files whose content matches any supplied name, as "<short sha>:<path>". */
function contentMatches(cwd, commits, names) {
  const needles = names.flatMap((n) => ['-e', n]);
  const found = new Set();
  for (let i = 0; i < commits.length; i += BATCH) {
    const batch = commits.slice(i, i + BATCH);
    let out = '';
    try {
      // -I skips binaries, -l names files rather than echoing the match, -F
      // keeps a name with regex characters in it a literal. Exit 1 means no
      // match, which is the good case and not an error.
      out = git(cwd, 'grep', '-I', '-l', '-F', '-i', ...needles, ...batch);
    } catch (e) {
      if (e.status !== 1) throw e;
    }
    for (const line of lines(out)) {
      const colon = line.indexOf(':');
      found.add(`${line.slice(0, Math.min(colon, 9))}:${line.slice(colon + 1)}`);
    }
  }
  return found;
}

/** Commits whose *message* names somebody — a rewrite has to reach these too. */
function messageMatches(cwd, refs, names) {
  const found = new Set();
  const log = git(cwd, 'log', '--format=%H%x1f%B%x1e', ...refs);
  for (const record of log.split('\x1e')) {
    const [sha, body = ''] = record.split('\x1f');
    if (!sha?.trim()) continue;
    const hay = body.toLowerCase();
    if (names.some((n) => hay.includes(n.toLowerCase()))) {
      found.add(`${sha.trim().slice(0, 9)} (commit message)`);
    }
  }
  return found;
}

export function check({ cwd, refs, names }) {
  const findings = [];
  for (const ref of refs) {
    for (const path of historyPaths(cwd, ref)) {
      const why = prohibitedPath(path);
      if (why) findings.push(`${ref}: ${path} — ${why}`);
      if (names.some((n) => path.toLowerCase().includes(n.toLowerCase()))) {
        findings.push(`${ref}: ${path} — a path names a supplied identifier`);
      }
    }
  }

  // The working tree's tracked files, which is what a clone of the checkout
  // would carry even before anyone walks the history.
  for (const path of lines(git(cwd, 'ls-files'))) {
    const why = prohibitedPath(path);
    if (why) findings.push(`working tree: ${path} — ${why}`);
  }

  const commits = lines(git(cwd, 'rev-list', ...refs));
  for (const hit of contentMatches(cwd, commits, names)) {
    findings.push(`${hit} — a file holds a supplied identifier`);
  }
  for (const hit of messageMatches(cwd, refs, names)) {
    findings.push(`${hit} — holds a supplied identifier`);
  }

  return { ok: findings.length === 0, findings: [...new Set(findings)].sort(), refs, commits: commits.length };
}

function main(argv) {
  let cwd = process.cwd();
  const refs = [];
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--repo') { cwd = argv[i + 1]; i += 1; } else refs.push(argv[i]);
  }
  if (refs.length === 0) {
    console.error('usage: release-hygiene.mjs [--repo <dir>] <ref>...');
    return 2;
  }
  // A ref that does not resolve must refuse, not crash: an operator who
  // mistypes a tag is the likeliest caller, and a stack trace on the way to a
  // release reads like the check ran and something else broke.
  for (const ref of refs) {
    try {
      git(cwd, 'rev-parse', '--verify', '--quiet', `${ref}^{commit}`);
    } catch {
      console.error(`refused: '${ref}' is not a ref in ${cwd}.`);
      return 2;
    }
  }

  const names = privateNames();
  if (names.length === 0) {
    console.error('refused: set LODESTAR_PRIVATE_NAMES to the identifiers this '
      + 'release must not contain (comma-separated).');
    console.error('The path checks alone are not this gate; a run that skipped '
      + 'the identifier scan would report a green it never measured.');
    return 2;
  }

  const { ok, findings, commits } = check({ cwd, refs, names });
  console.log(`checked ${refs.length} ref(s) over ${commits} commit(s): ${refs.join(' ')}`);
  console.log(`identifiers supplied: ${names.length} (not printed, by design)`);
  if (ok) {
    console.log('release hygiene: clean — no private data on the refs above.');
    return 0;
  }
  console.error(`release hygiene: REFUSED — ${findings.length} finding(s).`);
  for (const finding of findings) console.error(`  ${finding}`);
  console.error('\nDo not push. Rewrite the history, then run this again.');
  return 1;
}

if (import.meta.url === `file://${process.argv[1]}`) process.exit(main(process.argv.slice(2)));
