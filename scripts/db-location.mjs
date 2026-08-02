// scripts/db-location.mjs — where the board database lives.
//
// Session 7 gave every database one home: databases/. This module decides the
// board's path at boot and performs the one-time move of a legacy root-level
// board.db into that folder — backing it up first, because a move that goes
// wrong must never be the thing that loses the board.
import { existsSync, mkdirSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { runBackup } from './backup-db.mjs';

/**
 * Resolve the path server.js should open, migrating a legacy board.db if —
 * and only if — the databases/ target does not exist yet.
 *
 * An explicit BOARD_DB (Docker's /data/board.db, the :3001 test board, every
 * test harness) is returned verbatim and suppresses the migration entirely:
 * whoever set the path manages the path.
 */
export function resolveBoardDb({ root, env = process.env } = {}) {
  if (env.BOARD_DB) return env.BOARD_DB;
  const target = join(root, 'databases', 'board.db');
  if (existsSync(target)) return target;

  const legacy = join(root, 'board.db');
  if (existsSync(legacy)) {
    // Back up FIRST, honouring the same knobs the backup CLI reads.
    runBackup({
      dbPath: legacy,
      backupsDir: env.LODESTAR_BACKUP_DIR || join(root, 'backups'),
      remote: env.LODESTAR_RCLONE_REMOTE || 'gdrive',
      keep: Number(env.LODESTAR_BACKUP_KEEP) || 100,
      rcloneBin: env.LODESTAR_RCLONE_BIN || 'rclone',
    });
    mkdirSync(join(root, 'databases'), { recursive: true });
    renameSync(legacy, target);
    // A crashed server can leave journal siblings; they carry the last
    // transactions and must travel with the database they belong to.
    for (const ext of ['-journal', '-wal', '-shm']) {
      if (existsSync(legacy + ext)) renameSync(legacy + ext, target + ext);
    }
  }
  return target;
}
