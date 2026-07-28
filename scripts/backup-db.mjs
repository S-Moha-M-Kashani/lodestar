// scripts/backup-db.mjs
import { existsSync, mkdirSync, copyFileSync, readdirSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * Snapshot the database to `destPath`.
 *
 * `VACUUM INTO` is preferred because backups are now triggered by live writes
 * (see server.js), so a copy can land while the server is mid-transaction — and
 * a plain file copy of a database being written to can capture a torn page.
 * VACUUM INTO is atomic and always yields a consistent database.
 *
 * The byte-copy fallback covers anything SQLite refuses to open: a board file
 * that is not a database, a destination that already exists, an older SQLite.
 * Backing something up imperfectly always beats backing nothing up.
 */
function snapshot(dbPath, destPath) {
  try {
    // readOnly so the source can never be modified by its own backup.
    const db = new DatabaseSync(dbPath, { readOnly: true });
    try {
      db.prepare('VACUUM INTO ?').run(destPath);
      return 'vacuum';
    } finally {
      db.close();
    }
  } catch {
    copyFileSync(dbPath, destPath);
    return 'copy';
  }
}

export function runBackup({
  dbPath = process.env.BOARD_DB || join(ROOT, 'board.db'),
  backupsDir = process.env.LODESTAR_BACKUP_DIR || join(ROOT, 'backups'),
  remote = process.env.LODESTAR_RCLONE_REMOTE || 'gdrive',
  keep = Number(process.env.LODESTAR_BACKUP_KEEP) || 100,
  now = new Date(),
  rcloneBin = process.env.LODESTAR_RCLONE_BIN || 'rclone',
} = {}) {
  if (!existsSync(dbPath)) {
    return { status: 'no-db', pushed: false, warning: `no DB at ${dbPath} — nothing to back up` };
  }
  mkdirSync(backupsDir, { recursive: true });
  const stamp = now.toISOString().replace(/[:.]/g, '-');
  const localPath = join(backupsDir, `board-${stamp}.db`);
  snapshot(dbPath, localPath);

  // Prune: keep the newest `keep` board-*.db files.
  const backups = readdirSync(backupsDir)
    .filter((f) => f.startsWith('board-') && f.endsWith('.db'))
    .sort(); // ISO timestamps sort lexically = chronologically
  for (const f of backups.slice(0, Math.max(0, backups.length - keep))) {
    rmSync(join(backupsDir, f), { force: true });
  }

  // Check rclone exists.
  const probe = spawnSync(rcloneBin, ['version'], { encoding: 'utf8' });
  if (probe.error || probe.status !== 0) {
    return {
      status: 'rclone-missing', localPath, pushed: false,
      warning: `rclone not found — kept local backup at ${localPath}. Install rclone and run \`rclone config\` to enable Google Drive backup.`,
    };
  }
  // Push to Drive.
  const push = spawnSync(rcloneBin, ['copy', localPath, `${remote}:lodestar-backups/`], { encoding: 'utf8' });
  if (push.status !== 0) {
    return {
      status: 'rclone-failed', localPath, pushed: false,
      warning: `rclone copy to ${remote}:lodestar-backups/ failed — kept local backup at ${localPath}. Run \`rclone config\` to (re)authorize.`,
    };
  }
  return { status: 'ok', localPath, pushed: true };
}

// CLI entry — only when run directly, not when imported by tests.
if (process.argv[1] && process.argv[1].endsWith('backup-db.mjs')) {
  const result = runBackup();
  if (result.warning) console.warn('[backup] ' + result.warning);
  if (result.status === 'ok') console.log(`[backup] board.db backed up locally and to Google Drive (${result.localPath})`);
  else if (result.status === 'no-db') console.log('[backup] ' + result.warning);
  process.exit(0); // never block tests
}
