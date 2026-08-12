import { matchesFilters } from '../core/cards.js';
import { KEY_PREFIX } from '../core/keys.js';
import { state, view } from '../core/state.js';
import { cardText, localEmbed, normalizePoints, pca2, textHash } from '../lib/projection.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';
import { plotEmptyHint, renderPlotDot, renderPlotLegend } from './plot.js';

// Overview view — the semantic map. Cards are embedded, projected down to two
// dimensions (PCA, or t-SNE for local neighbourhoods) and plotted as dots, so
// thoughts about the same thing land near each other. The maths is in
// lib/projection.js; this module is the state and the screen around it.

let semanticState = 'idle'; // idle | loading | ready | unavailable
const semanticCache = new Map(); // textHash -> Float32Array
let extractorPromise = null;

// --- Projection toggle: PCA (fast, global axes) or t-SNE (local
// neighbourhoods — clusters of related thoughts pull together). t-SNE is
// computed from the same vectors, seeded so the same cards always land in
// the same spots, and cached per exact set of vectors.
const PROJ_KEY = KEY_PREFIX + 'proj';
const PROJ_LABEL = { pca: 'PCA', tsne: 't-SNE' };
let projection = 'pca';
try { const p = localStorage.getItem(PROJ_KEY); if (Object.hasOwn(PROJ_LABEL, p)) projection = p; } catch { /* private mode */ }

export function mulberry32(seed) {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

let tsneRun = 0; // bumping this aborts any in-flight gradient loop
let tsneBusyKey = null;
const tsneCache = new Map(); // vector-set hash -> Map(cardId -> [x, y])

const tsneKey = (cards, useSemantic) =>
  textHash(cards.map((c) => textHash(cardText(c))).join('|')) + (useSemantic ? ':s' : ':k');

const tsneReady = (cards, key) => {
  const cached = tsneCache.get(key);
  return Boolean(cached) && cards.every((c) => cached.has(c.id));
};

// Exact t-SNE — O(n²) per iteration is fine at personal-board sizes. Runs in
// chunks (yields to the browser every 40 iterations) so the tab never
// freezes; when it converges the dots slide over via updateOverviewPlot.
async function computeTsne(cards, vecs, initPts, key) {
  if (tsneBusyKey === key) return;
  tsneBusyKey = key;
  const runId = ++tsneRun;
  try {
    const n = vecs.length;
    const perplexity = Math.max(1, Math.min(15, Math.floor((n - 1) / 3)));
    const D = new Float64Array(n * n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = vecs[i], b = vecs[j];
      let s = 0;
      for (let k = 0; k < a.length; k++) { const d = a[k] - b[k]; s += d * d; }
      D[i * n + j] = s; D[j * n + i] = s;
    }
    // Per-point gaussian bandwidth matched to the target perplexity.
    const P = new Float64Array(n * n);
    const logU = Math.log(perplexity);
    const row = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      let beta = 1, betaMin = -Infinity, betaMax = Infinity;
      for (let t = 0; t < 50; t++) {
        let sum = 0;
        for (let j = 0; j < n; j++) { row[j] = j === i ? 0 : Math.exp(-D[i * n + j] * beta); sum += row[j]; }
        if (sum <= 0) sum = 1e-12;
        let H = 0;
        for (let j = 0; j < n; j++) if (row[j] > 0) { const p = row[j] / sum; H -= p * Math.log(p); }
        const diff = H - logU;
        if (Math.abs(diff) < 1e-5) break;
        if (diff > 0) { betaMin = beta; beta = betaMax === Infinity ? beta * 2 : (beta + betaMax) / 2; }
        else { betaMax = beta; beta = betaMin === -Infinity ? beta / 2 : (beta + betaMin) / 2; }
      }
      let sum = 0;
      for (let j = 0; j < n; j++) sum += row[j];
      if (sum <= 0) sum = 1e-12;
      for (let j = 0; j < n; j++) P[i * n + j] = row[j] / sum;
    }
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const p = Math.max((P[i * n + j] + P[j * n + i]) / (2 * n), 1e-12);
      P[i * n + j] = p; P[j * n + i] = p;
    }
    // Init from the PCA layout (scaled down, seeded whisper of noise to
    // break ties) so t-SNE refines the map instead of reshuffling it.
    const rand = mulberry32(0x10de57a2 ^ n);
    let spread = 0;
    for (const [x, y] of initPts) spread = Math.max(spread, Math.abs(x), Math.abs(y));
    const scale = spread > 0 ? 1e-2 / spread : 1e-2;
    const Y = new Float64Array(n * 2);
    const dY = new Float64Array(n * 2);
    const gains = new Float64Array(n * 2).fill(1);
    for (let i = 0; i < n; i++) {
      Y[i * 2] = initPts[i][0] * scale + (rand() - 0.5) * 1e-4;
      Y[i * 2 + 1] = initPts[i][1] * scale + (rand() - 0.5) * 1e-4;
    }
    const ITER = 350, EXAG_UNTIL = 100, ETA = 150;
    const Qnum = new Float64Array(n * n);
    for (let it = 0; it < ITER; it++) {
      if (runId !== tsneRun) return; // superseded by a newer vector set
      const exag = it < EXAG_UNTIL ? 12 : 1;
      const momentum = it < EXAG_UNTIL ? 0.5 : 0.8;
      let qsum = 0;
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const dx = Y[i * 2] - Y[j * 2], dy = Y[i * 2 + 1] - Y[j * 2 + 1];
        const q = 1 / (1 + dx * dx + dy * dy);
        Qnum[i * n + j] = q; Qnum[j * n + i] = q; qsum += 2 * q;
      }
      for (let i = 0; i < n; i++) {
        let gx = 0, gy = 0;
        for (let j = 0; j < n; j++) {
          if (i === j) continue;
          const q = Qnum[i * n + j];
          const mult = (exag * P[i * n + j] - Math.max(q / qsum, 1e-12)) * q;
          gx += 4 * mult * (Y[i * 2] - Y[j * 2]);
          gy += 4 * mult * (Y[i * 2 + 1] - Y[j * 2 + 1]);
        }
        const k = i * 2;
        gains[k] = Math.max(0.01, (gx > 0) === (dY[k] > 0) ? gains[k] * 0.8 : gains[k] + 0.2);
        gains[k + 1] = Math.max(0.01, (gy > 0) === (dY[k + 1] > 0) ? gains[k + 1] * 0.8 : gains[k + 1] + 0.2);
        dY[k] = momentum * dY[k] - ETA * gains[k] * gx;
        dY[k + 1] = momentum * dY[k + 1] - ETA * gains[k + 1] * gy;
      }
      let cx = 0, cy = 0;
      for (let i = 0; i < n; i++) { Y[i * 2] += dY[i * 2]; Y[i * 2 + 1] += dY[i * 2 + 1]; cx += Y[i * 2]; cy += Y[i * 2 + 1]; }
      cx /= n; cy /= n;
      for (let i = 0; i < n; i++) { Y[i * 2] -= cx; Y[i * 2 + 1] -= cy; }
      if (it % 40 === 39) await new Promise((r) => setTimeout(r, 0));
    }
    if (runId !== tsneRun) return;
    const pts = new Map();
    cards.forEach((c, i) => pts.set(c.id, [Y[i * 2], Y[i * 2 + 1]]));
    tsneCache.set(key, pts);
    if (tsneCache.size > 8) tsneCache.delete(tsneCache.keys().next().value);
    updateOverviewPlot(); // dots transition to their t-SNE spots
  } finally {
    if (tsneBusyKey === key) tsneBusyKey = null;
  }
}

function projectionSuffix() {
  if (projection !== 'tsne') return '';
  const cards = state.cards;
  if (cards.length <= 3) return ' · too few cards for t-SNE — PCA layout';
  const useSemantic = semanticState === 'ready' && haveSemanticFor(cards);
  return tsneReady(cards, tsneKey(cards, useSemantic)) ? ' · t-SNE layout' : ' · t-SNE settling…';
}

function overviewStatusText() {
  let base;
  switch (semanticState) {
    case 'ready': base = 'positioned by meaning · MiniLM sentence embeddings'; break;
    case 'loading': base = 'positioned by keyword overlap — reading the cards…'; break;
    case 'unavailable': base = 'positioned by keyword overlap — language model offline'; break;
    default: base = 'positioned by keyword overlap';
  }
  return base + projectionSuffix();
}

const haveSemanticFor = (cards) =>
  cards.length > 0 && cards.every((c) => semanticCache.has(textHash(cardText(c))));

// Lay out ALL cards together so a dot keeps its place when tag/priority
// filters hide its neighbours.
function overviewCoords(cards) {
  if (cards.length === 0) return new Map();
  if (cards.length === 1) return new Map([[cards[0].id, { x: 0.5, y: 0.5 }]]);
  const useSemantic = semanticState === 'ready' && haveSemanticFor(cards);
  const vecs = useSemantic
    ? cards.map((c) => semanticCache.get(textHash(cardText(c))))
    : cards.map((c) => localEmbed(cardText(c)));
  const pcaPts = pca2(vecs);
  if (projection === 'tsne' && cards.length > 3) { // 2-3 dots: t-SNE degenerates, PCA stands in
    const key = tsneKey(cards, useSemantic);
    if (tsneReady(cards, key)) {
      const cached = tsneCache.get(key);
      return normalizePoints(cards, cards.map((c) => cached.get(c.id)));
    }
    Promise.resolve().then(() => computeTsne(cards, vecs, pcaPts, key));
  }
  return normalizePoints(cards, pcaPts);
}

function getExtractor() {
  if (extractorPromise) return extractorPromise;
  extractorPromise = (async () => {
    const mod = await import('https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2');
    mod.env.allowLocalModels = false; // fetch weights from the HuggingFace hub
    return mod.pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2');
  })();
  return extractorPromise;
}

// Load the model once, embed any not-yet-embedded cards, then slide the
// dots to their semantic positions. Every failure degrades to the keyword
// layout — the network is never required. Set window.QBOARD_DISABLE_SEMANTIC
// to force the offline path (the e2e suite does this to stay network-free).
async function ensureSemanticLayout() {
  if (semanticState === 'unavailable') return;
  if (window.QBOARD_DISABLE_SEMANTIC) { semanticState = 'unavailable'; updateOverviewStatus(); return; }
  const cards = state.cards.slice();
  if (haveSemanticFor(cards)) {
    if (semanticState !== 'ready') { semanticState = 'ready'; updateOverviewPlot(); }
    return;
  }
  if (semanticState === 'loading') return;
  semanticState = 'loading';
  updateOverviewStatus();
  try {
    const extractor = await getExtractor();
    for (const c of cards) {
      const key = textHash(cardText(c));
      if (semanticCache.has(key)) continue;
      const out = await extractor(cardText(c) || ' ', { pooling: 'mean', normalize: true });
      semanticCache.set(key, Float32Array.from(out.data));
    }
    semanticState = 'ready';
  } catch (err) {
    console.warn('Semantic layout unavailable — keeping the keyword-overlap map.', err);
    semanticState = 'unavailable';
  }
  updateOverviewPlot();
}

function updateOverviewStatus() {
  const s = document.querySelector('#board .plot-status');
  if (s) s.textContent = overviewStatusText();
}

// Reposition the existing dots (they CSS-transition to their new spots)
// rather than re-render, so the shift to the semantic layout animates.
function updateOverviewPlot() {
  if (view !== 'overview') return;
  const field = document.querySelector('#board .plot-field');
  if (!field) return;
  const coords = overviewCoords(state.cards);
  for (const d of field.querySelectorAll('.plot-dot')) {
    const c = coords.get(d.dataset.id);
    if (c) { d.style.left = `${c.x * 100}%`; d.style.top = `${c.y * 100}%`; }
  }
  updateOverviewStatus();
}

function buildCrosshair(xLabel, yLabel) {
  const cross = document.createElement('div');
  cross.className = 'plot-cross';
  const vx = document.createElement('span'); vx.className = 'plot-cross-x';
  const vy = document.createElement('span'); vy.className = 'plot-cross-y';
  const xl = document.createElement('span'); xl.className = 'plot-axis-x'; xl.textContent = `${xLabel} →`;
  const yl = document.createElement('span'); yl.className = 'plot-axis-y'; yl.textContent = `${yLabel} ↑`;
  cross.append(vx, vy, xl, yl);
  return cross;
}

export function renderOverview() {
  const sheet = document.createElement('div');
  sheet.className = 'plot-sheet';

  const head = document.createElement('div');
  head.className = 'plot-head';
  const title = document.createElement('h2');
  title.className = 'plot-title';
  title.textContent = 'Overview';
  const caption = document.createElement('p');
  caption.className = 'plot-caption';
  caption.textContent = 'Everything on your mind, mapped by meaning — the closer two dots sit, the more alike they read.';
  const status = document.createElement('p');
  status.className = 'plot-status';
  status.textContent = overviewStatusText();

  const projToggle = document.createElement('div');
  projToggle.className = 'plot-proj-toggle';
  projToggle.setAttribute('role', 'group');
  projToggle.setAttribute('aria-label', 'Map projection');
  for (const p of Object.keys(PROJ_LABEL)) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.proj = p;
    b.textContent = PROJ_LABEL[p];
    b.setAttribute('aria-pressed', String(projection === p));
    b.addEventListener('click', () => {
      if (projection === p) return;
      projection = p;
      try { localStorage.setItem(PROJ_KEY, p); } catch { /* private mode */ }
      render();
      announce(`Overview projection: ${PROJ_LABEL[p]}`);
    });
    projToggle.append(b);
  }
  head.append(title, caption, status, projToggle);
  sheet.append(head, renderPlotLegend());

  const field = document.createElement('div');
  field.className = 'plot-field';
  const tsneAxes = projection === 'tsne' && state.cards.length > 3;
  field.append(tsneAxes ? buildCrosshair('t-SNE-1', 't-SNE-2') : buildCrosshair('PC-1', 'PC-2'));

  const all = state.cards;
  if (all.length === 0) {
    field.append(plotEmptyHint('Add a card and it will appear on the map'));
    sheet.append(field);
    return sheet;
  }

  const visible = all.filter(matchesFilters);
  const coords = overviewCoords(all);
  for (const card of visible) {
    const c = coords.get(card.id);
    if (c) field.append(renderPlotDot(card, c.x * 100, c.y * 100));
  }
  if (visible.length === 0) field.append(plotEmptyHint('No cards match'));

  sheet.append(field);
  // Run after this sheet is attached to #board so status/position updates land.
  Promise.resolve().then(ensureSemanticLayout); // upgrade to semantic positions in the background
  return sheet;
}
