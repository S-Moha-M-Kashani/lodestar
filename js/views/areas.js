import { cardLabel, controlVal } from '../core/cards.js';
import { catColor, catLabel, categories } from '../core/categories.js';
import { CONTROL_LABEL } from '../core/constants.js';
import { filters, state } from '../core/state.js';
import { fetchTrash } from '../core/trash.js';
import { wireCardContext } from '../ui/card-menu.js';
import { announce } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
import { render } from '../ui/render.js';
import { DAY } from './matrix.js';
import { plotEmptyHint } from './plot.js';

// Areas view — one small-multiples tile per life area plus an attention
// wheel, answering "which part of my life is starved?" at a glance. A tile
// click focuses the area: the category filter follows and a category-aware
// detail panel (cooling-off, learning, serenity, staleness) opens below.

const SVGNS = 'http://www.w3.org/2000/svg';
const WEEK = 7 * DAY;
export const isOpen = (c) => c.columnId !== 'answered';

export function humanAge(ms) {
  const d = Math.floor(ms / DAY);
  if (d < 1) return 'today';
  if (d < 14) return `${d} day${d === 1 ? '' : 's'}`;
  if (d < 61) return `${Math.round(d / 7)} weeks`;
  if (d < 365) return `${Math.round(d / 30.4)} months`;
  return `${+(d / 365).toFixed(1)} years`;
}

function areaStats(catId) {
  const cards = state.cards.filter((c) => c.category === catId);
  const open = cards.filter(isOpen);
  const oldest = open.reduce((m, c) => Math.min(m, c.createdAt), Infinity);
  const top = open.slice().sort((a, b) =>
    (b.importance === 'high') - (a.importance === 'high') || a.createdAt - b.createdAt)[0];
  return { cards, open, oldestAge: open.length ? Date.now() - oldest : 0, top };
}

// Area names ring the wheel outside the plot, so the viewport has to reserve
// room for them or a long name gets sliced off at the viewBox edge. Widths come
// from an off-screen twin that inherits the same `.wheel text` type styles, so
// the reservation tracks the real font instead of guessing at glyph advances.
let wheelRuler = null;
function wheelLabelWidth(text) {
  if (!wheelRuler) {
    const svg = document.createElementNS(SVGNS, 'svg');
    // Deliberately NOT class="wheel": that selector is test-stable API for the
    // one real wheel. `.wheel-ruler text` copies the type styles instead.
    svg.setAttribute('class', 'wheel-ruler');
    svg.setAttribute('aria-hidden', 'true');
    wheelRuler = document.createElementNS(SVGNS, 'text');
    svg.append(wheelRuler);
    document.body.append(svg);
  }
  wheelRuler.textContent = text;
  // getComputedTextLength() needs a rendered node; fall back to a mono estimate.
  return wheelRuler.getComputedTextLength() || text.length * 6;
}

// Attention wheel — spoke length is open-card mass (high importance
// counts double). Purely derived from the board: no scoring ritual to keep up.
function renderWheel(cats) {
  const SIZE = 260, CX = SIZE / 2, CY = SIZE / 2, R = 88, COLLAR = 18, MARGIN = 2;
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'wheel');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Attention wheel — open cards per life area');

  for (const f of [0.5, 1]) {
    const ring = document.createElementNS(SVGNS, 'circle');
    ring.setAttribute('cx', CX); ring.setAttribute('cy', CY); ring.setAttribute('r', R * f);
    ring.setAttribute('class', 'wheel-ring');
    svg.append(ring);
  }

  const masses = cats.map(({ stats }) =>
    stats.open.reduce((s, c) => s + (c.importance === 'high' ? 2 : 1), 0));
  const maxMass = Math.max(1, ...masses);
  const pts = [];
  // The plot box is fixed; the label ink pushes these bounds outward instead.
  let minX = 0, maxX = SIZE;
  cats.forEach(({ cat, stats }, i) => {
    const a = -Math.PI / 2 + (i / cats.length) * Math.PI * 2;
    const frac = 0.1 + 0.9 * (masses[i] / maxMass);
    const x = CX + Math.cos(a) * R * frac, y = CY + Math.sin(a) * R * frac;
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);

    const spoke = document.createElementNS(SVGNS, 'line');
    spoke.setAttribute('x1', CX); spoke.setAttribute('y1', CY);
    spoke.setAttribute('x2', CX + Math.cos(a) * R); spoke.setAttribute('y2', CY + Math.sin(a) * R);
    spoke.setAttribute('class', 'wheel-spoke');
    svg.append(spoke);

    const dot = document.createElementNS(SVGNS, 'circle');
    dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1)); dot.setAttribute('r', 3);
    dot.style.fill = catColor(cat.id);
    svg.append(dot);

    const lx = CX + Math.cos(a) * (R + COLLAR), ly = CY + Math.sin(a) * (R + COLLAR);
    const anchor = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end';
    const label = document.createElementNS(SVGNS, 'text');
    label.setAttribute('x', lx.toFixed(1)); label.setAttribute('y', ly.toFixed(1));
    label.setAttribute('text-anchor', anchor);
    label.setAttribute('dominant-baseline', 'middle');
    label.style.fill = catColor(cat.id);
    label.textContent = `${cat.label} ${stats.open.length}`;
    svg.append(label);

    const w = wheelLabelWidth(label.textContent);
    const left = anchor === 'start' ? lx : anchor === 'end' ? lx - w : lx - w / 2;
    minX = Math.min(minX, left - MARGIN);
    maxX = Math.max(maxX, left + w + MARGIN);
  });

  // Widen the viewport around the untouched plot box, so the ring keeps its
  // size and the names simply get the room they need on either side.
  svg.setAttribute('viewBox', `${minX.toFixed(1)} 0 ${(maxX - minX).toFixed(1)} ${SIZE}`);
  svg.setAttribute('width', Math.ceil(maxX - minX));
  svg.setAttribute('height', SIZE);

  if (cats.length > 1) {
    const poly = document.createElementNS(SVGNS, 'polygon');
    poly.setAttribute('points', pts.join(' '));
    poly.setAttribute('class', 'wheel-shape');
    // insert under the dots/labels so it never obscures them
    svg.insertBefore(poly, svg.children[2]);
  }
  return svg;
}

// 12-week activity sparkline: cards created or touched per week.
function renderSparkline(cards) {
  const W = 120, H = 26, BINS = 12;
  const now = Date.now();
  const bins = new Array(BINS).fill(0);
  for (const c of cards) {
    const wc = Math.floor((now - c.createdAt) / WEEK);
    if (wc >= 0 && wc < BINS) bins[BINS - 1 - wc]++;
    const wu = Math.floor((now - c.updatedAt) / WEEK);
    if (wu >= 0 && wu < BINS && wu !== wc) bins[BINS - 1 - wu]++;
  }
  const max = Math.max(1, ...bins);
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'area-spark');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('aria-hidden', 'true');
  const line = document.createElementNS(SVGNS, 'polyline');
  line.setAttribute('points', bins.map((v, i) =>
    `${(i * W / (BINS - 1)).toFixed(1)},${(H - 3 - (v / max) * (H - 6)).toFixed(1)}`).join(' '));
  svg.append(line);
  return svg;
}

export function renderAreas() {
  const sheet = document.createElement('div');
  sheet.className = 'plot-sheet areas-sheet';

  const inUse = categories
    .map((cat) => ({ cat, stats: areaStats(cat.id) }))
    .filter(({ stats }) => stats.cards.length > 0);
  const openTotal = inUse.reduce((s, { stats }) => s + stats.open.length, 0);

  const head = document.createElement('div');
  head.className = 'plot-head';
  const title = document.createElement('h2');
  title.className = 'plot-title';
  title.textContent = 'Areas';
  const caption = document.createElement('p');
  caption.className = 'plot-caption';
  caption.textContent = 'Every life area side by side — a lopsided wheel means a starved corner. Click an area for a closer look.';
  const status = document.createElement('p');
  status.className = 'plot-status';
  status.textContent = `${openTotal} open across ${inUse.length} area${inUse.length === 1 ? '' : 's'}`;
  head.append(title, caption, status);
  sheet.append(head);

  if (inUse.length === 0) {
    sheet.append(plotEmptyHint('Give a card a category and its life area appears here'));
    return sheet;
  }

  const wheelWrap = document.createElement('div');
  wheelWrap.className = 'wheel-wrap';
  wheelWrap.append(renderWheel(inUse));
  sheet.append(wheelWrap);

  const grid = document.createElement('div');
  grid.className = 'areas-grid';
  for (const { cat, stats } of inUse) {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'area-tile';
    tile.dataset.cat = cat.id;
    tile.style.setProperty('--cat', catColor(cat.id));
    // ink-fade staleness tint: fully saturated at 6 months of carrying
    tile.style.setProperty('--stale', Math.min(1, stats.oldestAge / (180 * DAY)).toFixed(2));
    tile.setAttribute('aria-pressed', String(filters.category === cat.id));

    const th = document.createElement('span');
    th.className = 'area-tile-head';
    const name = document.createElement('span');
    name.className = 'area-tile-name';
    name.textContent = cat.label;
    const count = document.createElement('span');
    count.className = 'area-tile-count';
    count.textContent = `${stats.open.length} open`;
    th.append(name, count);

    const age = document.createElement('span');
    age.className = 'area-tile-age';
    age.textContent = stats.open.length
      ? (stats.oldestAge < DAY ? 'all fresh today' : `carrying ${humanAge(stats.oldestAge)}`)
      : 'all answered';

    tile.append(th, age);
    if (stats.top) {
      const top = document.createElement('span');
      top.className = 'area-tile-top';
      top.textContent = stats.top.title;
      tile.append(top);
    }
    tile.append(renderSparkline(stats.cards));

    tile.addEventListener('click', () => {
      filters.category = filters.category === cat.id ? '' : cat.id;
      render();
      announce(filters.category ? `${cat.label} in focus` : 'Area focus cleared');
    });
    grid.append(tile);
  }
  sheet.append(grid);

  if (filters.category && inUse.some(({ cat }) => cat.id === filters.category)) {
    sheet.append(renderAreaDetail(filters.category));
  }
  return sheet;
}

export function detailPanel(heading, hint) {
  const panel = document.createElement('section');
  panel.className = 'area-panel';
  const h = document.createElement('h4');
  h.textContent = heading;
  panel.append(h);
  if (hint) {
    const p = document.createElement('p');
    p.className = 'panel-hint';
    p.textContent = hint;
    panel.append(p);
  }
  return panel;
}

export function areaRow(card) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'area-row';
  b.dataset.id = card.id;
  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  const t = document.createElement('span');
  t.className = 'area-row-title';
  t.textContent = card.title;
  b.append(num, t);
  b.addEventListener('click', () => openDialog(card.id));
  // Review builds its rows from here too, so both views gain the menu at once.
  wireCardContext(b, card.id);
  return b;
}

function renderAreaDetail(catId) {
  const wrap = document.createElement('section');
  wrap.className = 'area-detail';
  wrap.style.setProperty('--cat', catColor(catId));
  const h = document.createElement('h3');
  h.textContent = `${catLabel(catId)} — a closer look`;
  wrap.append(h);

  const cards = state.cards.filter((c) => c.category === catId);
  const open = cards.filter(isOpen);

  const purchases = open.filter((c) => c.tags.includes('purchase'));
  if (purchases.length) wrap.append(renderCooloffPanel(catId, purchases));
  if (catId === 'mind' && cards.some((c) => c.tags.length)) wrap.append(renderLearningPanel(cards));
  const problems = open.filter((c) => c.type === 'problem');
  if (problems.length) wrap.append(renderSerenityStrip(problems));
  wrap.append(renderStalenessPanel(open));
  return wrap;
}

// 30-day rule: a wanted thing waits a month; still wanted when the window
// matures ("decide now") is a real want, everything let go in Trash counts
// as money left unspent.
function renderCooloffPanel(catId, purchases) {
  const panel = detailPanel('Cooling-off', 'The 30-day rule — want it just as much a month later? Then decide.');
  panel.classList.add('area-cooloff');
  const list = document.createElement('div');
  list.className = 'cooloff-list';
  for (const c of purchases.slice().sort((a, b) => a.createdAt - b.createdAt)) {
    const row = areaRow(c);
    row.classList.add('cooloff-row');
    const left = 30 - Math.floor((Date.now() - c.createdAt) / DAY);
    const days = document.createElement('span');
    days.className = 'cooloff-days';
    if (left > 0) days.textContent = `${left} d left`;
    else { days.textContent = 'decide now'; days.dataset.due = 'true'; }
    row.append(days);
    list.append(row);
  }
  panel.append(list);

  const resisted = document.createElement('p');
  resisted.className = 'cooloff-resisted';
  resisted.hidden = true;
  panel.append(resisted);
  fetchTrash().then((trash) => {
    const n = trash.filter((c) => c.category === catId && c.tags.includes('purchase')).length;
    if (n > 0) {
      resisted.textContent = `${n} resisted — sent to Trash unbought.`;
      resisted.hidden = false;
    }
  });
  return panel;
}

// Learning progress — per co-tag answered-vs-open bars plus a burn-up of
// cards captured vs answered over time.
function renderLearningPanel(cards) {
  const panel = detailPanel('Learning progress', 'Per topic: answered vs still open.');
  panel.classList.add('area-learning');

  const byTag = new Map();
  for (const c of cards) for (const t of c.tags) {
    const e = byTag.get(t) || { open: 0, done: 0 };
    if (isOpen(c)) e.open++; else e.done++;
    byTag.set(t, e);
  }
  const bars = document.createElement('div');
  bars.className = 'learn-bars';
  const ranked = [...byTag].sort((a, b) => (b[1].open + b[1].done) - (a[1].open + a[1].done));
  for (const [tag, e] of ranked) {
    const total = e.open + e.done;
    const row = document.createElement('div');
    row.className = 'learn-row';
    const name = document.createElement('span');
    name.className = 'learn-tag';
    name.textContent = tag;
    const bar = document.createElement('div');
    bar.className = 'learn-bar';
    const done = document.createElement('span');
    done.className = 'learn-done';
    done.style.width = `${(e.done / total) * 100}%`;
    const openSeg = document.createElement('span');
    openSeg.className = 'learn-open';
    openSeg.style.width = `${(e.open / total) * 100}%`;
    bar.append(done, openSeg);
    const n = document.createElement('span');
    n.className = 'learn-count';
    n.textContent = `${e.done}/${total}`;
    row.append(name, bar, n);
    bars.append(row);
  }
  panel.append(bars, renderBurnup(cards));
  return panel;
}

// Burn-up: cumulative asked (soft ink) vs cumulative answered (category ink).
function renderBurnup(cards) {
  const W = 260, H = 60, PAD = 4;
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'learn-burnup');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('aria-hidden', 'true');
  const t0 = Math.min(...cards.map((c) => c.createdAt));
  const span = Math.max(1, Date.now() - t0);
  const created = cards.map((c) => c.createdAt).sort((a, b) => a - b);
  const answered = cards.filter((c) => !isOpen(c)).map((c) => c.updatedAt).sort((a, b) => a - b);
  const total = created.length;
  const lineFor = (events, cls) => {
    const line = document.createElementNS(SVGNS, 'polyline');
    line.setAttribute('class', cls);
    const pts = [];
    const STEPS = 24;
    for (let s = 0; s <= STEPS; s++) {
      const t = t0 + (span * s) / STEPS;
      let n = 0;
      while (n < events.length && events[n] <= t) n++;
      pts.push(`${(PAD + ((W - 2 * PAD) * s) / STEPS).toFixed(1)},${(H - PAD - ((H - 2 * PAD) * n) / total).toFixed(1)}`);
    }
    line.setAttribute('points', pts.join(' '));
    return line;
  };
  svg.append(lineFor(created, 'burnup-asked'), lineFor(answered, 'burnup-answered'));
  return svg;
}

// CBT worry triage: what you can act on, what you can only nudge, and what
// is out of your hands — the last pile is for the weekly worry window.
function renderSerenityStrip(problems) {
  const panel = detailPanel('Serenity check',
    'Problems sorted by what you can do about them. Visit the “out of my hands” pile once, in a weekly worry window — on schedule, not on loop.');
  panel.classList.add('serenity-strip');
  const groups = document.createElement('div');
  groups.className = 'serenity-groups';
  for (const ctl of ['act', 'influence', 'none']) {
    const group = document.createElement('div');
    group.className = 'serenity-group';
    group.dataset.control = ctl;
    const gh = document.createElement('h5');
    gh.textContent = CONTROL_LABEL[ctl];
    group.append(gh);
    for (const c of problems.filter((p) => controlVal(p.control) === ctl)) {
      group.append(areaRow(c));
    }
    groups.append(group);
  }
  panel.append(groups);
  return panel;
}

// Personal-CRM "last touched": the open cards this area is silently dropping.
function renderStalenessPanel(open) {
  const panel = detailPanel('Last touched', 'Oldest first — what this area might be silently dropping.');
  panel.classList.add('area-stale');
  if (!open.length) {
    const p = document.createElement('p');
    p.className = 'panel-hint';
    p.textContent = 'Nothing open here — clean desk.';
    panel.append(p);
    return panel;
  }
  const list = document.createElement('div');
  list.className = 'stale-list';
  for (const c of open.slice().sort((a, b) => a.updatedAt - b.updatedAt)) {
    const row = areaRow(c);
    row.classList.add('stale-row');
    const age = document.createElement('span');
    age.className = 'stale-age';
    const ms = Date.now() - c.updatedAt;
    age.textContent = ms < DAY ? 'today' : `${humanAge(ms)} ago`;
    row.append(age);
    list.append(row);
  }
  panel.append(list);
  return panel;
}
