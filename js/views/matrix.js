import { controlVal, effortVal, matchesFilters } from '../core/cards.js';
import { KEY_PREFIX } from '../core/keys.js';
import { state } from '../core/state.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';
import { renderPlotDot, renderPlotLegend } from './plot.js';

// Matrix view — four lenses on the same cards. Importance is always the
// vertical axis; the picker swaps what it is crossed with: urgency
// (Eisenhower), effort (Leverage), control (Serenity) or age since last
// touch (Follow-through). One quadrant grid serves them all.

export const DAY = 86400000;
const ageBucket = (card) => {
  const age = Date.now() - (card.updatedAt || card.createdAt || Date.now());
  return age < 14 * DAY ? 'fresh' : age < 45 * DAY ? 'aging' : 'stale';
};

// Urgent sits on the LEFT (the classic Eisenhower orientation): the eye
// lands on "Answer now" first. Cell keys are `${importance}|${column}`.
const MATRICES = {
  eisenhower: {
    label: 'Eisenhower',
    caption: 'The Eisenhower matrix — importance against urgency, urgent on the left. Set both on a card to place it here.',
    axisX: '← URGENCY',
    cols: ['high', 'low'],
    colOf: (c) => c.urgency,
    placed: (c) => Boolean(c.importance && c.urgency),
    pending: (cards) => cards.filter((c) => !(c.importance && c.urgency)).length,
    awaiting: 'importance & urgency',
    cells: {
      'high|high': { verb: 'Answer now', sub: 'important · urgent', accent: 'var(--high)' },
      'high|low':  { verb: 'Schedule', sub: 'important · not urgent', accent: 'var(--ink-blue)' },
      'low|high':  { verb: 'Delegate', sub: 'not important · urgent', accent: 'var(--ink-amber)' },
      'low|low':   { verb: 'Drop', sub: 'not important · not urgent', accent: 'var(--ink-soft)' },
    },
  },
  leverage: {
    label: 'Leverage',
    caption: 'Importance against effort — where a little work moves a lot, and which chores quietly eat a week.',
    axisX: 'EFFORT →',
    cols: ['low', 'medium', 'high'],
    colOf: (c) => effortVal(c.effort),
    placed: (c) => Boolean(c.importance),
    pending: (cards) => cards.filter((c) => !c.importance).length,
    awaiting: 'importance',
    cells: {
      'high|low':    { verb: 'Quick win', sub: 'important · low effort', accent: 'var(--high)' },
      'high|medium': { verb: 'Solid bet', sub: 'important · medium effort', accent: 'var(--ink-blue)' },
      'high|high':   { verb: 'Big bet', sub: 'important · high effort', accent: 'var(--ink-amber)' },
      'low|low':     { verb: 'Fill-in', sub: 'minor · low effort', accent: 'var(--ink-soft)' },
      'low|medium':  { verb: 'Meh', sub: 'minor · medium effort', accent: 'var(--ink-soft)' },
      'low|high':    { verb: 'Time sink', sub: 'minor · high effort', accent: 'var(--ink-amber)' },
    },
  },
  serenity: {
    label: 'Serenity',
    caption: 'Importance against control — what deserves action, and what you are allowed to put down.',
    axisX: 'CONTROL →',
    cols: ['act', 'influence', 'none'],
    colOf: (c) => controlVal(c.control),
    placed: (c) => Boolean(c.importance),
    pending: (cards) => cards.filter((c) => !c.importance).length,
    awaiting: 'importance',
    cells: {
      'high|act':       { verb: 'Act now', sub: 'important · in your hands', accent: 'var(--high)' },
      'high|influence': { verb: 'Nudge', sub: 'important · can influence', accent: 'var(--ink-blue)' },
      'high|none':      { verb: 'Accept & plan', sub: 'important · out of your hands', accent: 'var(--ink-amber)' },
      'low|act':        { verb: 'Easy win', sub: 'minor · in your hands', accent: 'var(--ink-blue)' },
      'low|influence':  { verb: 'Mention it', sub: 'minor · can influence', accent: 'var(--ink-soft)' },
      'low|none':       { verb: 'Let go', sub: 'minor · out of your hands', accent: 'var(--ink-soft)' },
    },
  },
  followthrough: {
    label: 'Follow-through',
    caption: 'Importance against time since a card was last touched. Answered cards rest; everything else ages.',
    axisX: 'AGE →',
    cols: ['fresh', 'aging', 'stale'],
    colOf: ageBucket,
    placed: (c) => Boolean(c.importance) && c.columnId !== 'answered',
    pending: (cards) => cards.filter((c) => c.columnId !== 'answered' && !c.importance).length,
    awaiting: 'importance',
    cells: {
      'high|fresh': { verb: 'On it', sub: 'important · touched < 2 weeks', accent: 'var(--ink-blue)' },
      'high|aging': { verb: 'Watch', sub: 'important · 2–6 weeks old', accent: 'var(--ink-amber)' },
      'high|stale': { verb: 'Rescue', sub: 'important · > 6 weeks untouched', accent: 'var(--high)' },
      'low|fresh':  { verb: 'Fine', sub: 'minor · recently touched', accent: 'var(--ink-soft)' },
      'low|aging':  { verb: 'Fine', sub: 'minor · 2–6 weeks old', accent: 'var(--ink-soft)' },
      'low|stale':  { verb: 'Let go?', sub: 'minor · > 6 weeks untouched', accent: 'var(--ink-amber)' },
    },
  },
};

const MATRIX_KEY = KEY_PREFIX + 'matrix';
let matrixLens = 'eisenhower';
try {
  const m = localStorage.getItem(MATRIX_KEY);
  if (Object.hasOwn(MATRICES, m)) matrixLens = m;
} catch (_) { /* private mode */ }

function matrixAxis(className, text) {
  const el = document.createElement('span');
  el.className = className;
  el.textContent = text;
  return el;
}

export function renderMatrix() {
  const lens = MATRICES[matrixLens];
  const sheet = document.createElement('div');
  sheet.className = 'plot-sheet matrix-plate';

  const head = document.createElement('div');
  head.className = 'plot-head';
  const title = document.createElement('h2');
  title.className = 'plot-title';
  title.textContent = 'Matrix';

  const pick = document.createElement('div');
  pick.className = 'matrix-switch';
  pick.setAttribute('role', 'group');
  pick.setAttribute('aria-label', 'Choose a matrix');
  for (const [id, m] of Object.entries(MATRICES)) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.matrix = id;
    b.textContent = m.label;
    b.setAttribute('aria-pressed', String(id === matrixLens));
    b.addEventListener('click', () => {
      if (matrixLens === id) return;
      matrixLens = id;
      try { localStorage.setItem(MATRIX_KEY, id); } catch (_) { /* private mode */ }
      render();
      announce(`${m.label} matrix`);
    });
    pick.append(b);
  }

  const caption = document.createElement('p');
  caption.className = 'plot-caption';
  caption.textContent = lens.caption;
  const status = document.createElement('p');
  status.className = 'plot-status';
  const placed = state.cards.filter((c) => lens.placed(c) && matchesFilters(c));
  const awaiting = lens.pending(state.cards);
  status.textContent = `${placed.length} placed`
    + (awaiting ? ` · ${awaiting} awaiting ${lens.awaiting}` : '');
  head.append(title, pick, caption, status);
  sheet.append(head, renderPlotLegend());

  const grid = document.createElement('div');
  grid.className = 'matrix-grid';
  grid.style.gridTemplateColumns = `24px repeat(${lens.cols.length}, 1fr)`;
  grid.append(matrixAxis('matrix-axis-imp', 'IMPORTANCE ↑'));
  const axisX = matrixAxis('matrix-axis-urg', lens.axisX);
  axisX.style.gridColumn = `2 / span ${lens.cols.length}`;
  axisX.style.gridRow = '3';
  grid.append(axisX);

  for (const imp of ['high', 'low']) {
    lens.cols.forEach((col, x) => {
      const q = lens.cells[`${imp}|${col}`];
      const cell = document.createElement('section');
      cell.className = 'matrix-quad';
      cell.dataset.imp = imp;
      cell.dataset.x = col;
      if (matrixLens === 'eisenhower') cell.dataset.urg = col; // test-stable selector
      cell.style.gridColumn = String(2 + x);
      cell.style.gridRow = imp === 'high' ? '1' : '2';
      cell.style.setProperty('--quad', q.accent);
      cell.setAttribute('aria-label', `${q.verb} — ${q.sub}`);

      const qhead = document.createElement('div');
      qhead.className = 'matrix-quad-head';
      const verb = document.createElement('span');
      verb.className = 'matrix-quad-verb';
      verb.textContent = q.verb;
      const sub = document.createElement('span');
      sub.className = 'matrix-quad-sub';
      sub.textContent = q.sub;
      qhead.append(verb, sub);

      const cards = placed.filter((c) => c.importance === imp && lens.colOf(c) === col);
      const count = document.createElement('span');
      count.className = 'matrix-quad-count';
      count.textContent = cards.length;
      qhead.append(count);
      cell.append(qhead);

      const dots = document.createElement('div');
      dots.className = 'matrix-quad-dots';
      for (const card of cards) dots.append(renderPlotDot(card));
      cell.append(dots);
      grid.append(cell);
    });
  }

  sheet.append(grid);
  return sheet;
}
