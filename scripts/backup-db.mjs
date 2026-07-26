// scripts/backup-db.mjs
import { existsSync, mkdirSync, copyFileSync, readdirSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

export function runBackup({
  dbPath = process.env.BOARD_DB || join(ROOT, 'board.db'),
  backupsDir = join(ROOT, 'backups'),
  remote = process.env.LODESTAR_RCLONE_REMOTE || 'gdrive',
  keep = Number(process.env.LODESTAR_BACKUP_KEEP) || 30,
  now = new Date(),
  rcloneBin = 'rclone',
} = {}) {
  if (!existsSync(dbPath)) {
    return { status: 'no-db', pushed: false, warning: `no DB at ${dbPath} — nothing to back up` };
  }
  mkdirSync(backupsDir, { recursive: true });
  const stamp = now.toISOString().replace(/[:.]/g, '-');
  const localPath = join(backupsDir, `board-${stamp}.db`);
  copyFileSync(dbPath, localPath);

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
