// Life areas — the coloured celluloid tabs of the ledger. Each category is an
// oklch hue; every theme sets --cat-l/--cat-c once, so every ink stays
// legible on Morning/Day/Dusk/Night without per-theme colour tables.
// The set is the user's own: categories can be added and removed (✎ on the
// rail), and the registry is saved with the board and synced to the server.

const DEFAULT_CATEGORIES = [
  { id: 'work',   label: 'Work',   h: 255 },
  { id: 'love',   label: 'Love',   h: 15 },
  { id: 'family', label: 'Family', h: 60 },
  { id: 'health', label: 'Health', h: 150 },
  { id: 'mind',   label: 'Mind',   h: 295 },
  { id: 'music',  label: 'Music',  h: 340 },
  { id: 'travel', label: 'Travel', h: 200 },
  { id: 'home',   label: 'Home',   h: 90 },
  { id: 'money',  label: 'Money',  h: 40 },
];
export const CAT_LIMIT = 24;
// Hues a new category can be inked in, spread around the oklch wheel.
export const HUE_CHOICES = [15, 40, 60, 90, 120, 150, 180, 200, 230, 255, 285, 310, 340];

export let categories = DEFAULT_CATEGORIES.map((c) => ({ ...c }));

// The registry is replaced wholesale — by a load, a server adopt, an import, or
// the ✎ editor — and an imported binding is read-only in every module but this
// one, so replacing it is a call rather than an assignment. Readers keep using
// `categories` directly: an ES module export is a live binding, so they see the
// new registry without a re-import or a getter.
export function setCategories(next) {
  categories = next;
}

export const catSlug = (s) =>
  String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
export const catById = (id) => categories.find((c) => c.id === id);
export const catColor = (id) => {
  const c = catById(id);
  return c ? `oklch(var(--cat-l) var(--cat-c) ${c.h})` : 'var(--ink-soft)';
};
export const catLabel = (id) => (catById(id) || { label: '' }).label;

export function sanitizeCategories(raw) {
  if (!Array.isArray(raw)) return null;
  const seen = new Set();
  const out = [];
  for (const c of raw) {
    if (!c || typeof c !== 'object') continue;
    const id = typeof c.id === 'string' ? catSlug(c.id) : '';
    const label = typeof c.label === 'string' && c.label.trim() ? c.label.trim().slice(0, 24) : '';
    const h = Number.isFinite(c.h) ? ((Math.round(c.h) % 360) + 360) % 360 : null;
    if (!id || !label || h === null || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, label, h });
    if (out.length >= CAT_LIMIT) break;
  }
  return out.length ? out : null;
}
