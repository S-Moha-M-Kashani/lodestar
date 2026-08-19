// scripts/backup-db.mjs
import { existsSync, mkdirSync, copyFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
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

/** The timestamp a backup filename carries, as a Date. `runBackup` writes
 *  `<name>-<ISO with : and . replaced by ->.db` (and `.json` for the export
 *  twin), so the timestamp arrives as exactly six hyphen-separated segments —
 *  `2026`, `06`, `01T00`, `00`, `00`, `000Z` — however many the database name
 *  itself contributes. Taking the last six is therefore what makes a
 *  hyphenated name work: `brain-checkpoints.db` is a real record in
 *  databases/real/, and a seventh segment would swallow `checkpoints` into the
 *  date and parse nothing. The separators are lost but their positions are
 *  not, so this restores them rather than parsing loosely. An unreadable name
 *  returns the epoch, which prunes on count alone — the old behaviour, and the
 *  safe direction for a file this cannot identify. */
function stampOf(filename) {
  const raw = filename.replace(/\.(db|json)$/, '').split('-').slice(-6).join('-');
  const iso = raw.replace(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z$/,
    '$1-$2-$3T$4:$5:$6.$7Z');
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? new Date(0) : at;
}

/**
 * Write an Import-JSON-ready export of a board snapshot to `destPath`.
 *
 * The point of the json/ twin: a .db snapshot needs a working `node:sqlite` to
 * be worth anything, while a JSON file pastes straight into the board's Import
 * JSON dialog — a recovery path that works from any machine with a browser.
 *
 * Reads the just-written snapshot (readOnly), never the live database, so the
 * export is exactly as consistent as the snapshot it sits beside. Only a real
 * SQLite file with a `cards` table qualifies — assistant.db and a byte-copied
 * non-database simply return false, never throw: the .db snapshot already
 * exists at that point and a broken export must not fail the backup.
 *
 * The shape is what `parseState` (js/core/cards.js) accepts and what the
 * server's `rowToCard` produces: `{ version: 1, cards, categories }`, keys
 * camelCased, JSON-string columns parsed. Two deliberate simplifications:
 * values are exported raw rather than re-validated (sanitizeCard on import is
 * the validator, and duplicating its rules here would let them drift), and
 * `board_id` is ignored — every board's live cards land in one file, because
 * the Import dialog merges into one board anyway and an export that silently
 * dropped the non-default boards would be a backup with holes in it.
 */
function exportJson(snapPath, destPath) {
  let db;
  try {
    db = new DatabaseSync(snapPath, { readOnly: true });
  } catch {
    return false; // not a SQLite file at all
  }
  try {
    const table = (name) => db.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?").get(name);
    if (!table('cards')) return false;
    const parsed = (s, fallback) => {
      try { return JSON.parse(s) ?? fallback; } catch { return fallback; }
    };
    // Filtered in JS, not SQL, so a pre-migration file missing deleted_at or
    // pending still exports (an absent column reads as undefined = live).
    const cards = db.prepare('SELECT * FROM cards').all()
      .filter((r) => r.deleted_at == null && !r.pending)
      .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
      .map((r) => ({
        id: r.id,
        columnId: r.column_id,
        title: r.title,
        notes: r.notes,
        type: r.type,
        category: r.category,
        importance: r.importance,
        urgency: r.urgency,
        effort: r.effort,
        control: r.control,
        effortSrc: r.effort_src,
        controlSrc: r.control_src,
        deadline: r.deadline,
        habitFreq: r.habit_freq,
        habitCount: r.habit_count,
        habitTimes: parsed(r.habit_times, []),
        habitHistory: parsed(r.habit_history, {}),
        num: r.num,
        tags: parsed(r.tags, []),
        createdAt: r.created_at,
        updatedAt: r.updated_at,
      }));
    const categories = table('categories')
      ? db.prepare('SELECT id, label, h FROM categories ORDER BY position ASC').all()
        .map((r) => ({ id: r.id, label: r.label, h: r.h }))
      : [];
    mkdirSync(dirname(destPath), { recursive: true });
    writeFileSync(destPath, JSON.stringify({ version: 1, cards, categories }, null, 2));
    return true;
  } catch {
    return false; // a cards table this cannot read is not a board
  } finally {
    db.close();
  }
}

/** Prune one backup folder for one database name: keep the newest `keep`, and
 *  keep anything younger than `floor` regardless of count — a file must fail
 *  both rules to be deleted. Per name, so board's backups can never evict
 *  assistant's; per folder, so db/ and json/ retention never interact. */
function prune(dir, name, ext, keep, floor) {
  if (!existsSync(dir)) return;
  const files = readdirSync(dir)
    .filter((f) => f.startsWith(`${name}-`) && f.endsWith(ext))
    .sort(); // ISO timestamps sort lexically = chronologically
  for (const f of files.slice(0, Math.max(0, files.length - keep))) {
    if (stampOf(f) >= floor) continue; // inside the age floor: keep it
    rmSync(join(dir, f), { force: true });
  }
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
    return { status: 'no-db', pushed: false, jsonPaths: [], warning: `no DB at ${dbPath || databasesDir} — nothing to back up` };
  }
  // Two subfolders: db/ holds the SQLite snapshots, json/ the Import-JSON
  // exports. json/ is only created when a board actually exports — an empty
  // folder beside real backups would look like a backup that lost its files.
  const dbDir = join(backupsDir, 'db');
  const jsonDir = join(backupsDir, 'json');
  mkdirSync(dbDir, { recursive: true });
  const stamp = now.toISOString().replace(/[:.]/g, '-');
  // Prune with two rules, and a file must fail both to be deleted: keep the
  // newest `keep`, and keep anything younger than `keepDays` regardless of
  // count. Count alone was the whole rule until 2026-08-13, when a measurement
  // found 100 board snapshots spanning ten days — 49% of them from the last
  // two — because an agent session writes 20-30 a day. That honours the number
  // and loses the point: a backup exists to recover a mistake noticed a week
  // later. A snapshot is ~300 KB, so age costs almost nothing and count is
  // kept only as a ceiling on truly old ones.
  const floor = new Date(now.getTime() - keepDays * 86_400_000);
  const localPaths = [];
  const jsonPaths = [];
  for (const src of present) {
    const name = basename(src, '.db');
    const dest = join(dbDir, `${name}-${stamp}.db`);
    snapshot(src, dest);
    localPaths.push(dest);
    // The json twin, cut from the snapshot just written so both files agree.
    const jsonDest = join(jsonDir, `${name}-${stamp}.json`);
    if (exportJson(dest, jsonDest)) jsonPaths.push(jsonDest);
    prune(dbDir, name, '.db', keep, floor);
    prune(jsonDir, name, '.json', keep, floor);
  }
  const localPath = localPaths[0];

  // Check rclone exists.
  const probe = spawnSync(rcloneBin, ['version'], { encoding: 'utf8' });
  if (probe.error || probe.status !== 0) {
    return {
      status: 'rclone-missing', localPath, localPaths, jsonPaths, pushed: false,
      warning: `rclone not found — kept local backup at ${localPaths.join(', ')}. Install rclone and run \`rclone config\` to enable Google Drive backup.`,
    };
  }
  // Push to Drive — the two subfolders are mirrored there, db/ beside json/.
  const pushes = [
    ...localPaths.map((p) => [p, `${remote}:lodestar-backups/db/`]),
    ...jsonPaths.map((p) => [p, `${remote}:lodestar-backups/json/`]),
  ];
  for (const [p, target] of pushes) {
    const push = spawnSync(rcloneBin, ['copy', p, target], { encoding: 'utf8' });
    if (push.status !== 0) {
      return {
        status: 'rclone-failed', localPath, localPaths, jsonPaths, pushed: false,
        warning: `rclone copy to ${target} failed — kept local backup at ${localPaths.join(', ')}. Run \`rclone config\` to (re)authorize.`,
      };
    }
  }
  return { status: 'ok', localPath, localPaths, jsonPaths, pushed: true };
}

// CLI entry — only when run directly, not when imported by tests.
if (process.argv[1] && process.argv[1].endsWith('backup-db.mjs')) {
  const result = runBackup();
  if (result.warning) console.warn('[backup] ' + result.warning);
  if (result.status === 'ok') console.log(`[backup] backed up locally and to Google Drive (${result.localPaths.join(', ')})`);
  else if (result.status === 'no-db') console.log('[backup] ' + result.warning);
  process.exit(0); // never block tests
}
