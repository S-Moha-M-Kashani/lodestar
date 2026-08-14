// scripts/backup-db.mjs
import { existsSync, mkdirSync, copyFileSync, readdirSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { join, dirname, basename } from 'node:path';
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

/** The timestamp a snapshot filename carries, as a Date. `runBackup` writes
 *  `<name>-<ISO with : and . replaced by ->.db`, so the timestamp arrives as
 *  exactly six hyphen-separated segments — `2026`, `06`, `01T00`, `00`, `00`,
 *  `000Z` — however many the database name itself contributes. Taking the last
 *  six is therefore what makes a hyphenated name work: `brain-checkpoints.db`
 *  is a real record in databases/real/, and a seventh segment would swallow
 *  `checkpoints` into the date and parse nothing. The separators are lost but
 *  their positions are not, so this restores them rather than parsing loosely.
 *  An unreadable name returns the epoch, which prunes on count alone — the old
 *  behaviour, and the safe direction for a file this cannot identify. */
function stampOf(filename) {
  const raw = filename.replace(/\.db$/, '').split('-').slice(-6).join('-');
  const iso = raw.replace(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z$/,
    '$1-$2-$3T$4:$5:$6.$7Z');
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? new Date(0) : at;
}

export function runBackup({
  dbPath = process.env.BOARD_DB,
  databasesDir = join(ROOT, 'databases', 'real'),
  backupsDir = process.env.LODESTAR_BACKUP_DIR || join(ROOT, 'backups'),
  remote = process.env.LODESTAR_RCLONE_REMOTE || 'gdrive',
  keep = Number(process.env.LODESTAR_BACKUP_KEEP) || 100,
  keepDays = Number(process.env.LODESTAR_BACKUP_KEEP_DAYS) || 90,
  now = new Date(),
  rcloneBin = process.env.LODESTAR_RCLONE_BIN || 'rclone',
} = {}) {
  // One explicit file (BOARD_DB, or the server's write-triggered backup), or
  // every .db directly inside databases/real/ — board.db and assistant.db.
  // Two things are deliberately never included: chroma-data/ (derived, the
  // bulk, rebuilds from the two SQLite records) and the databases/test/
  // sibling (the :3001 sandbox is disposable by definition, and backing it up
  // would push throwaway boards to the same Drive folder as the real ones).
  const sources = dbPath ? [dbPath]
    : existsSync(databasesDir)
      ? readdirSync(databasesDir).filter((f) => f.endsWith('.db')).map((f) => join(databasesDir, f)).sort()
      : [];
  const present = sources.filter((p) => existsSync(p));
  if (!present.length) {
    return { status: 'no-db', pushed: false, warning: `no DB at ${dbPath || databasesDir} — nothing to back up` };
  }
  mkdirSync(backupsDir, { recursive: true });
  const stamp = now.toISOString().replace(/[:.]/g, '-');
  const localPaths = [];
  for (const src of present) {
    const name = basename(src, '.db');
    const dest = join(backupsDir, `${name}-${stamp}.db`);
    snapshot(src, dest);
    localPaths.push(dest);

    // Prune per database name — board's snapshots must never be able to evict
    // assistant's. Two rules, and a file must fail both to be deleted: keep the
    // newest `keep`, and keep anything younger than `keepDays` regardless of
    // count. Count alone was the whole rule until 2026-08-13, when a measurement
    // found 100 board snapshots spanning ten days — 49% of them from the last
    // two — because an agent session writes 20-30 a day. That honours the number
    // and loses the point: a backup exists to recover a mistake noticed a week
    // later. A snapshot is ~300 KB, so age costs almost nothing and count is
    // kept only as a ceiling on truly old ones.
    const floor = new Date(now.getTime() - keepDays * 86_400_000);
    const backups = readdirSync(backupsDir)
      .filter((f) => f.startsWith(`${name}-`) && f.endsWith('.db'))
      .sort(); // ISO timestamps sort lexically = chronologically
    const surplus = backups.slice(0, Math.max(0, backups.length - keep));
    for (const f of surplus) {
      if (stampOf(f) >= floor) continue; // inside the age floor: keep it
      rmSync(join(backupsDir, f), { force: true });
    }
  }
  const localPath = localPaths[0];

  // Check rclone exists.
  const probe = spawnSync(rcloneBin, ['version'], { encoding: 'utf8' });
  if (probe.error || probe.status !== 0) {
    return {
      status: 'rclone-missing', localPath, localPaths, pushed: false,
      warning: `rclone not found — kept local backup at ${localPaths.join(', ')}. Install rclone and run \`rclone config\` to enable Google Drive backup.`,
    };
  }
  // Push to Drive.
  for (const p of localPaths) {
    const push = spawnSync(rcloneBin, ['copy', p, `${remote}:lodestar-backups/`], { encoding: 'utf8' });
    if (push.status !== 0) {
      return {
        status: 'rclone-failed', localPath, localPaths, pushed: false,
        warning: `rclone copy to ${remote}:lodestar-backups/ failed — kept local backup at ${localPaths.join(', ')}. Run \`rclone config\` to (re)authorize.`,
      };
    }
  }
  return { status: 'ok', localPath, localPaths, pushed: true };
}

// CLI entry — only when run directly, not when imported by tests.
if (process.argv[1] && process.argv[1].endsWith('backup-db.mjs')) {
  const result = runBackup();
  if (result.warning) console.warn('[backup] ' + result.warning);
  if (result.status === 'ok') console.log(`[backup] backed up locally and to Google Drive (${result.localPaths.join(', ')})`);
  else if (result.status === 'no-db') console.log('[backup] ' + result.warning);
  process.exit(0); // never block tests
}
