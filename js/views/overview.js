import { boardUrl } from '../core/boards.js';
import { matchesFilters } from '../core/cards.js';
import { KEY_PREFIX } from '../core/keys.js';
import { state, view } from '../core/state.js';
import { cardText, localEmbed, normalizePoints, pca2, textHash } from '../lib/projection.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';
import { plotEmptyHint, renderPlotDot, renderPlotLegend } from './plot.js';

// Overview view — the semantic map. Cards are embedded, projected down to two
// dimensions (PCA, or t-SNE for local neighbourhoods) and plotted as dots, so
// thoughts about the same thing land near each other.
//
// **The arithmetic is not here any more (2026-09-07).** This module used to
// pull a MiniLM from a CDN, embed every card serially into a Map a reload threw
// away, and run a 350-iteration t-SNE on the paint thread — all of it inside
// `renderOverview`, so a filter change recomputed the whole layout. A card's
// vector is now a stored fact (`card_vectors` in board.db, produced by the
// brain through `POST /api/rag/vectors`) and the projection is numpy
// (`POST /api/rag/projection`). What is left here asks, caches, plots, and
// falls back.
//
// **The keyword path stays and is what makes the map unblockable.** `localEmbed`
// + `pca2` (js/lib/projection.js) need no network and no model, so the map
// renders for every board state — before the vectors arrive, with the brain
// stopped, and under `window.QBOARD_DISABLE_SEMANTIC`, which is what keeps the
// e2e suite network-free. It is memoized by the same key the brain uses, so
// `render()` plots from a cache instead of computing.

// Where the layout comes from, and therefore what the status line says.
//   keyword   — computed here from the cards' words. Always available.
//   semantic  — the brain's, from the stored vectors.
//   asking    — a request is in flight; the keyword map is on screen meanwhile.
//   offline   — the brain could not be reached. The keyword map is the answer.
let layoutSource = 'keyword';
let layoutNote = '';           // why, when it is not the semantic one
let brainReachable = null;     // null until a request has told us

const PROJ_KEY = KEY_PREFIX + 'proj';
const PROJ_LABEL = { pca: 'PCA', tsne: 't-SNE' };
let projection = 'pca';
try { const p = localStorage.getItem(PROJ_KEY); if (Object.hasOwn(PROJ_LABEL, p)) projection = p; } catch { /* private mode */ }

/** The key a layout is remembered under: the visible cards and the exact text
 *  each of them reads as, plus the projection asked for.
 *
 *  The same shape the brain keys its own cache on, and **sorted** for the same
 *  reason — the same cards in a different order are the same map, so returning
 *  to a filter lands the dots exactly where they were rather than approximately
 *  there. `textHash(cardText(c))` is also the fingerprint the stored vectors
 *  carry, so a card whose words changed re-keys here and re-embeds there
 *  without the two having to agree by convention. */
const layoutKey = (cards, kind, source) =>
  source + ':' + kind + ':' + textHash(
    cards.map((c) => c.id + '␟' + textHash(cardText(c))).sort().join('␞'));

// Layouts, keyed as above. Small on purpose: a layout is cheap once the vectors
// exist, and this exists to make flipping a filter back and forth free rather
// than to be a store. Eight is what the browser's old t-SNE cache held.
const LAYOUT_MAX = 8;
const layouts = new Map();

function rememberLayout(key, coords) {
  layouts.set(key, coords);
  while (layouts.size > LAYOUT_MAX) layouts.delete(layouts.keys().next().value);
}

/** The keyword layout: computed here, from the cards' words alone.
 *
 *  Memoized, which is the whole of what task 7.1 is about — this used to run
 *  inside `renderOverview`, so every repaint (a filter change, a commit, a
 *  keystroke in the search box) paid for a PCA over the whole board. */
function keywordCoords(cards, kind) {
  const key = layoutKey(cards, kind, 'keyword');
  const hit = layouts.get(key);
  if (hit) return hit;
  const coords = cards.length === 1
    ? new Map([[cards[0].id, { x: 0.5, y: 0.5 }]])
    : normalizePoints(cards, pca2(cards.map((c) => localEmbed(cardText(c)))));
  rememberLayout(key, coords);
  return coords;
}

/** Coordinates for the set on screen, from the cache and never computed twice.
 *
 *  Over the **visible** set, which reverses what this module did until
 *  2026-09-07. The old comment read "Lay out ALL cards together so a dot keeps
 *  its place when tag/priority filters hide its neighbours", and it was a
 *  reasonable call when filters were rare. They are how this board is read now,
 *  and twelve cards laid out among themselves use the whole field where the
 *  same twelve inside the whole board's layout are a huddle in one corner. What
 *  pays for the dots moving is the cache above — going back is exact, not
 *  approximate — and the status line, which says what is being laid out. */
function overviewCoords(cards) {
  if (cards.length === 0) return new Map();
  const kind = projection === 'tsne' && cards.length > 3 ? 'tsne' : 'pca';
  return layouts.get(layoutKey(cards, kind, 'semantic')) || keywordCoords(cards, kind);
}

// --- Asking the brain ------------------------------------------------------

const offline = () => Boolean(window.QBOARD_DISABLE_SEMANTIC);

let asking = null; // the key currently in flight, so one filter change asks once

/** Fill any missing vectors, fetch the layout for the visible set, and slide
 *  the dots when it lands.
 *
 *  Two requests and not one, deliberately: the set that needs embedding is the
 *  whole board and the set that needs laying out is what the filters left, so
 *  joining them would make changing a filter ask for embeddings. Every failure
 *  degrades to the keyword layout — the network is never required. */
async function askForLayout(cards, kind) {
  const key = layoutKey(cards, kind, 'semantic');
  if (offline() || asking === key || layouts.has(key)) return;
  asking = key;
  layoutSource = 'asking';
  updateOverviewStatus();
  try {
    // Fire and forget the fill: the layout below answers with whatever vectors
    // exist, so a board still embedding gets a partial map now rather than a
    // spinner until every card is done.
    await fetch(boardUrl('/api/rag/vectors'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const res = await fetch(boardUrl('/api/rag/projection'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: cards.map((c) => c.id), kind }),
    });
    if (!res.ok) throw new Error(`projection ${res.status}`);
    const body = await res.json();
    brainReachable = true;
    const coords = new Map(Object.entries(body.coords || {})
      .map(([id, [x, y]]) => [id, { x, y }]));
    // Every visible card or none: a map where some dots are semantic and the
    // rest are keyword positions is two layouts in one field, and the distance
    // between two dots would mean nothing.
    if (coords.size === cards.length) {
      rememberLayout(key, coords);
      layoutSource = 'semantic';
      layoutNote = '';
    } else {
      layoutSource = 'keyword';
      layoutNote = `${cards.length - coords.size} of ${cards.length} cards not read yet`;
    }
  } catch (err) {
    console.warn('Semantic layout unavailable — keeping the keyword-overlap map.', err);
    brainReachable = false;
    layoutSource = 'offline';
    layoutNote = '';
  } finally {
    asking = null;
    updateOverviewPlot(); // dots transition to wherever they now belong
  }
}

// --- What the reader is told ----------------------------------------------

/** Which map is on screen, in words. The reader has to be able to tell a
 *  semantic layout from a keyword one — they answer different questions about
 *  two dots being close — and the one thing this must never do is claim the
 *  semantic map while showing the other. */
function overviewStatusText() {
  const kind = projection === 'tsne' && state.cards.filter(matchesFilters).length > 3
    ? PROJ_LABEL.tsne : PROJ_LABEL.pca;
  switch (layoutSource) {
    case 'semantic': return `positioned by meaning · ${kind} of the stored card vectors`;
    case 'asking': return 'positioned by keyword overlap — reading the cards…';
    case 'offline': return 'positioned by keyword overlap — the assistant service is not running';
    default: return 'positioned by keyword overlap'
      + (layoutNote ? ` — ${layoutNote}` : '');
  }
}

function updateOverviewStatus() {
  const s = document.querySelector('#board .plot-status');
  if (s) s.textContent = overviewStatusText();
  const t = document.querySelector('#board .plot-proj-toggle [data-proj="tsne"]');
  if (t) paintTsneButton(t);
}

/** t-SNE is the brain's alone now — the browser's 350-iteration loop is gone —
 *  so the control says why it cannot be used rather than being silently absent
 *  or silently doing nothing. Offered whenever the brain has not been shown to
 *  be unreachable: it is disabled by evidence, never by a guess. */
function paintTsneButton(btn) {
  const reason = offline() ? 'the semantic map is switched off for this session'
    : brainReachable === false ? 'the assistant service is not running'
      : '';
  btn.disabled = Boolean(reason);
  btn.title = reason ? `t-SNE needs the assistant service — ${reason}` : 'Local neighbourhoods';
}

// Reposition the existing dots (they CSS-transition to their new spots)
// rather than re-render, so the shift to the semantic layout animates.
function updateOverviewPlot() {
  if (view !== 'overview') return;
  const field = document.querySelector('#board .plot-field');
  if (!field) return;
  const coords = overviewCoords(state.cards.filter(matchesFilters));
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
    if (p === 'tsne') paintTsneButton(b);
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
  const visible = state.cards.filter(matchesFilters);
  const tsneAxes = projection === 'tsne' && visible.length > 3;
  field.append(tsneAxes ? buildCrosshair('t-SNE-1', 't-SNE-2') : buildCrosshair('PC-1', 'PC-2'));

  if (state.cards.length === 0) {
    field.append(plotEmptyHint('Add a card and it will appear on the map'));
    sheet.append(field);
    return sheet;
  }

  // From the cache, or computed once and cached — never in the render path a
  // second time. The keyword positions are on screen immediately, on a cold
  // board and a warm one alike; the semantic ones slide in when they land.
  const coords = overviewCoords(visible);
  for (const card of visible) {
    const c = coords.get(card.id);
    if (c) field.append(renderPlotDot(card, c.x * 100, c.y * 100));
  }
  if (visible.length === 0) field.append(plotEmptyHint('No cards match'));

  sheet.append(field);
  // Run after this sheet is attached to #board so status/position updates land.
  if (visible.length > 1) {
    const kind = projection === 'tsne' && visible.length > 3 ? 'tsne' : 'pca';
    Promise.resolve().then(() => askForLayout(visible, kind));
  }
  return sheet;
}
