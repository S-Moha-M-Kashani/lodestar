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
