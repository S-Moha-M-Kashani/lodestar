// Local keys were prefixed 'question-board:' until the board's word became
// "card". Several of them hold data that lives nowhere else — the undo
// timeline, Review state, the model picks — so the rename copies the old
// values across once instead of stranding them. Delete migrateStorageKeys and
// LEGACY_* once no browser in use predates the rename.

export const KEY_PREFIX = 'lodestar:';
const LEGACY_PREFIX = 'question-board:';
const LEGACY_SUFFIXES = ['v1', 'theme', 'view', 'history', 'habit-mute',
  'proj', 'matrix', 'reviewed', 'resurface', 'models'];

function migrateStorageKeys() {
  try {
    for (const suffix of LEGACY_SUFFIXES) {
      const old = localStorage.getItem(LEGACY_PREFIX + suffix);
      // Test for null, not falsiness: '' is a real stored value. And skip any
      // key that already exists, or a boot would undo a change made since.
      if (old !== null && localStorage.getItem(KEY_PREFIX + suffix) === null) {
        localStorage.setItem(KEY_PREFIX + suffix, old);
      }
    }
  } catch (_) { /* private mode */ }
}
migrateStorageKeys();

export const STORAGE_KEY = KEY_PREFIX + 'v1';
export const THEME_KEY = KEY_PREFIX + 'theme';
export const VIEW_KEY = KEY_PREFIX + 'view';
export const HISTORY_KEY = KEY_PREFIX + 'history';
export const HISTORY_LIMIT = 50; // snapshots kept; oldest fall off like a rotated log

// The board as of the last time the server and this browser agreed on it:
// { rev, fp }. Owned by js/core/sync.js and written nowhere else. Deliberately
// absent from LEGACY_SUFFIXES — it postdates the rename, and a wrong watermark
// is worse than none: it would claim a sync that never happened.
export const SYNC_KEY = KEY_PREFIX + 'synced';
