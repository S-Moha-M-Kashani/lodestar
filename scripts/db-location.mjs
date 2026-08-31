// scripts/db-location.mjs — where the SQLite records live.
//
// Session 7 gave every database one home: databases/. The real/test split then
// gave real and test data separate rooms in it — databases/real/ for the :3000
// stack's records, databases/test/ for the :3001 sandbox — so a test database
// can never sit beside (or be mistaken for) a real one. This module decides
// both records' paths at boot and performs the one-time move from an older
// home — backing the file up first, because a move that goes wrong must never
// be the thing that loses the board.
import { existsSync, mkdirSync, renameSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { runBackup } from './backup-db.mjs';

/** Move `legacy` to `target`, backing it up first and carrying any journal
 *  siblings — a crashed server can leave them holding the last transactions. */
function migrate({ legacy, target, root, env }) {
  runBackup({
    dbPath: legacy,
    backupsDir: env.LODESTAR_BACKUP_DIR || join(root, 'backups'),
    remote: env.LODESTAR_RCLONE_REMOTE || 'gdrive',
    keep: Number(env.LODESTAR_BACKUP_KEEP) || 100,
    keepDays: Number(env.LODESTAR_BACKUP_KEEP_DAYS) || 90,
    rcloneBin: env.LODESTAR_RCLONE_BIN || 'rclone',
  });
  mkdirSync(dirname(target), { recursive: true });
  renameSync(legacy, target);
  for (const ext of ['-journal', '-wal', '-shm']) {
    if (existsSync(legacy + ext)) renameSync(legacy + ext, target + ext);
  }
}

/** Shared resolution: an explicit env path wins verbatim and suppresses the
 *  migration entirely (Docker, the :3001 test board, every test harness —
 *  whoever set the path manages the path). Otherwise the databases/real/
 *  target wins if present; failing that, the newest older home found is
 *  migrated into it. Stale files in other older homes are left for the user,
 *  never merged or deleted. */
function resolveDb({ root, env, envKey, filename, olderHomes }) {
  if (env[envKey]) return env[envKey];
  // An opt-in guard for contexts with no business near real data: scratch
  // scripts, screenshot runs, anything an agent writes to look at the app. The
  // default has to stay the real board — `npm start` must open it with no
  // ceremony — so this cannot be a default. What it removes is the specific
  // failure of 2026-08-13: a script set LODESTAR_DB, a name nothing reads, fell
  // through to this line, and served the user's own board. Setting the path
  // explicitly always wins, which is why the check sits below that branch; it
  // sits above the migration too, so a scratch run can never move a legacy file.
  //
  // The brackets used to be load-bearing rather than a style choice: the
  // invariant test
  // brain/tests/test_config.py::test_env_example_documents_every_variable_the_code_reads
  // scans this directory for env reads, and its pattern recognised the
  // bracket-and-quotes form but not a plain property read — so tidying this
  // into a dot made the read invisible and the .env.example entry below then
  // failed as "in .env.example, read by nothing". On 2026-08-31 that hole was
  // closed (db/backend.mjs reads a parameter by property and lost both its
  // variables to it), so the scanner now sees either shape and this form is
  // simply the one that was here. That scanner reads this file as text, so a
  // comment spelling either form out literally would itself be counted — which
  // is why this one describes the shapes instead of quoting them.
  if (env['LODESTAR_REFUSE_REAL_DB']) {
    throw new Error(
      `refusing to open databases/real/${filename}: LODESTAR_REFUSE_REAL_DB is `
      + `set and no ${envKey} was given. Set ${envKey} to a temporary path, or `
      + 'unset LODESTAR_REFUSE_REAL_DB if real data is genuinely wanted.');
  }
  const target = join(root, 'databases', 'real', filename);
  if (existsSync(target)) return target;
  const legacy = olderHomes.find((p) => existsSync(p));
  if (legacy) migrate({ legacy, target, root, env });
  return target;
}

/** Resolve the board path server.js should open. Older homes, newest first:
 *  the pre-split databases/board.db, then the original root-level board.db. */
export function resolveBoardDb({ root, env = process.env } = {}) {
  return resolveDb({
    root, env, envKey: 'BOARD_DB', filename: 'board.db',
    olderHomes: [join(root, 'databases', 'board.db'), join(root, 'board.db')],
  });
}

/** Resolve the chat record's path. One older home: the pre-split
 *  databases/assistant.db (it never lived at the repo root). */
export function resolveAssistantDb({ root, env = process.env } = {}) {
  return resolveDb({
    root, env, envKey: 'ASSISTANT_DB', filename: 'assistant.db',
    olderHomes: [join(root, 'databases', 'assistant.db')],
  });
}
