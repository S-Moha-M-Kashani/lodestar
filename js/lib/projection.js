import { catLabel } from '../core/categories.js';

// Projection maths — pure functions, no DOM and no app state. A cheap hash
// embedding so the Overview map works with the network off, and PCA to bring
// it down to the two dimensions a screen has.

// --- Embeddings + PCA -----------------------------------------------------
// Each card becomes a vector; PCA projects those vectors to two dimensions
// (PC-1, PC-2).
//
// **This is the offline path, and since 2026-09-07 it is the only one in the
// browser.** The semantic vectors used to come from a HuggingFace model
// (Transformers.js) loaded lazily from a CDN and re-earned on every visit;
// they are now produced once by the brain and stored beside the cards
// (`card_vectors`), and the reduction runs in numpy. What is here is the
// deterministic keyword vector that stands in until those arrive — or for as
// long as the brain is not running — so the map always renders and never needs
// the network. `js/views/overview.js` memoizes both of these by card text, so
// a repaint plots rather than computes.

const EMBED_DIM = 128;

// What a card *is*, as text: its own words plus the labels it was filed
// under. The labels are part of the meaning — two cards that read alike but
// sit in different life areas are not the same thought, and on title+notes
// alone they landed on the same dot. The labels lead because the sentence is
// truncated from the tail: a long note must never be able to push a card's
// category out of its own vector. `catLabel` gives the word the user chose
// ("Health"), not the id, since that is what an embedding model can read —
// and renaming a category changes this text, which re-keys the layout cache in
// views/overview.js and invalidates that card's stored vector, exactly as it
// should. `map_card_text` in brain/retrieval/projection.py is this function
// mirrored, and `textHash` below is mirrored there too — the two ends have to
// agree about which vector is stale and neither can import the other.
export const cardText = (card) => [
  (card.tags || []).join(' '),
  catLabel(card.category),
  card.type,
  card.title,
  card.notes,
].filter(Boolean).join(' ').trim();

export function textHash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
}

function l2normalize(v) {
  let s = 0;
  for (let i = 0; i < v.length; i++) s += v[i] * v[i];
  s = Math.sqrt(s);
  if (s > 0) for (let i = 0; i < v.length; i++) v[i] /= s;
  return v;
}

export function localEmbed(text) {
  const v = new Float64Array(EMBED_DIM);
  const tokens = String(text).toLowerCase().match(/[a-z0-9]+/g) || [];
  for (const tok of tokens) {
    let h = 2166136261;
    for (let k = 0; k < tok.length; k++) { h ^= tok.charCodeAt(k); h = Math.imul(h, 16777619); }
    v[(h >>> 0) % EMBED_DIM] += 1;
  }
  return l2normalize(v);
}

const vdot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };

// Dominant eigenvector of the centred data's covariance, via power iteration;
// pass `deflate` to get the next component orthogonal to the first.
function powerIteration(X, d, deflate) {
  const n = X.length;
  let v = new Float64Array(d);
  for (let j = 0; j < d; j++) v[j] = ((Math.imul(j + 1, 2654435761) >>> 0) % 2000) / 1000 - 1; // deterministic seed
  l2normalize(v);
  for (let iter = 0; iter < 60; iter++) {
    if (deflate) { const p = vdot(v, deflate); for (let j = 0; j < d; j++) v[j] -= p * deflate[j]; }
    const Xv = new Float64Array(n);
    for (let i = 0; i < n; i++) Xv[i] = vdot(X[i], v);
    const w = new Float64Array(d);
    for (let i = 0; i < n; i++) { const row = X[i], c = Xv[i]; for (let j = 0; j < d; j++) w[j] += row[j] * c; }
    if (deflate) { const p = vdot(w, deflate); for (let j = 0; j < d; j++) w[j] -= p * deflate[j]; }
    if (vdot(w, w) === 0) break;
    l2normalize(w);
    v = w;
  }
  return v;
}

export function pca2(vectors) {
  const n = vectors.length, d = vectors[0].length;
  const mean = new Float64Array(d);
  for (const v of vectors) for (let j = 0; j < d; j++) mean[j] += v[j];
  for (let j = 0; j < d; j++) mean[j] /= n;
  const X = vectors.map((v) => { const r = new Float64Array(d); for (let j = 0; j < d; j++) r[j] = v[j] - mean[j]; return r; });
  const pc1 = powerIteration(X, d, null);
  const pc2 = powerIteration(X, d, pc1);
  return X.map((r) => [vdot(r, pc1), vdot(r, pc2)]);
}

// Map raw 2-D points to plotting fractions in [0.06, 0.94], centred so the
// data mean sits at the middle crosshair. A degenerate spread falls back to a ring.
export function normalizePoints(cards, pts) {
  const coords = new Map();
  const n = cards.length;
  let mx = 0, my = 0;
  for (const [x, y] of pts) { mx += x; my += y; }
  mx /= n; my /= n;
  let sx = 0, sy = 0;
  for (const [x, y] of pts) { sx = Math.max(sx, Math.abs(x - mx)); sy = Math.max(sy, Math.abs(y - my)); }
  if (Math.max(sx, sy) < 1e-9) {
    cards.forEach((c, i) => {
      const a = (i / n) * Math.PI * 2;
      coords.set(c.id, { x: 0.5 + 0.32 * Math.cos(a), y: 0.5 + 0.32 * Math.sin(a) });
    });
    return coords;
  }
  cards.forEach((c, i) => {
    const [x, y] = pts[i];
    coords.set(c.id, {
      x: 0.5 + 0.44 * (x - mx) / (sx || 1),
      y: 0.5 - 0.44 * (y - my) / (sy || 1), // invert so PC-2 grows upward
    });
  });
  return coords;
}
