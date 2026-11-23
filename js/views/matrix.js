import { cardLabel, controlVal, effortVal, matchesFilters } from '../core/cards.js';
import { commit } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { state } from '../core/state.js';
import { announce, getCard } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
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
    awaiting: 'importance & urgency',
    needs: ['importance', 'urgency'],
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
    awaiting: 'importance',
    needs: ['importance'],
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
    awaiting: 'importance',
    needs: ['importance'],
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
    awaiting: 'importance',
    needs: ['importance'],
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

// --------------------------------------------------------------------------
// The Unknown corner
//
// A card this matrix cannot place yet — importance, or urgency on the
// Eisenhower, not set — used to be a number in the status line and nowhere
// else. It waits in a pocket pinned on the corner where the two axes meet,
// the one spot that belongs to no square, sorted by what it is missing. A card
// that knows its importance but not its urgency is also marked on the edge of
// the row it already belongs to. Pressing any of these opens a small box that
// asks only the missing question; the last answer drops it into its square.

const WORD = {
  importance: [['high', 'important'], ['low', 'not important']],
  urgency: [['high', 'urgent'], ['low', 'not urgent']],
};

/** What this lens still needs to know about a card, in order. */
const missingOf = (lens, card) => lens.needs.filter((k) => !card[k]);

/** The cards this lens considers but cannot place, under the filters. */
const unknownCards = (lens) => state.cards.filter((c) => matchesFilters(c)
  && (matrixLens !== 'followthrough' || c.columnId !== 'answered')
  && missingOf(lens, c).length > 0);

let ask = null; // the open quick-answer box

function closeAsk() {
  ask?.remove();
  ask = null;
}

/** Ask the card's missing questions, beside the dot that was pressed. */
function openAsk(cardId, anchor) {
  closeAsk();
  const card = getCard(cardId);
  const lens = MATRICES[matrixLens];
  if (!card || !anchor) return;
  const missing = missingOf(lens, card);
  if (!missing.length) return;

  ask = document.createElement('div');
  ask.className = 'matrix-ask';
  ask.setAttribute('role', 'dialog');
  ask.setAttribute('aria-label', `Place ${cardLabel(card)} on the matrix`);
  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  const title = document.createElement('p');
  title.className = 'matrix-ask-title';
  title.textContent = card.title;
  ask.append(num, title);

  for (const key of missing) {
    const row = document.createElement('div');
    row.className = 'matrix-ask-row';
    const name = document.createElement('span');
    name.textContent = key;
    row.append(name);
    for (const [value, word] of WORD[key]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'matrix-ask-stamp';
      b.dataset.key = key;
      b.dataset.value = value;
      b.textContent = word;
      b.addEventListener('click', () => {
        card[key] = value;
        card.updatedAt = Date.now();
        closeAsk();
        commit(`Set ${key} on ${cardLabel(card)}`);
        const still = missingOf(MATRICES[matrixLens], card);
        if (still.length) openAsk(card.id, document.querySelector(`.matrix-unknown .plot-dot[data-id="${card.id}"]`));
        else announce(`${cardLabel(card)} placed`);
      });
      row.append(b);
    }
    ask.append(row);
  }
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'matrix-ask-open';
  open.textContent = 'Open the card';
  open.addEventListener('click', () => { closeAsk(); openDialog(card.id); });
  ask.append(open);

  document.body.append(ask);
  // Fixed, from the dot's rectangle, kept inside the window.
  const r = anchor.getBoundingClientRect();
  const w = ask.offsetWidth;
  const h = ask.offsetHeight;
  ask.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8))}px`;
  ask.style.top = `${r.bottom + 8 + h > innerHeight ? Math.max(8, r.top - h - 8) : r.bottom + 8}px`;
  ask.querySelector('button').focus();
}

document.addEventListener('click', (e) => {
  if (ask && !e.target.closest('.matrix-ask, .matrix-unknown .plot-dot, .matrix-edge .plot-dot')) closeAsk();
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && ask) closeAsk();
});

/** A dot for a card the matrix cannot place: hollow, and a press asks. */
function unknownDot(card, lens) {
  const dot = renderPlotDot(card);
  dot.classList.add('plot-dot-unknown');
  dot.setAttribute('aria-label', `${dot.getAttribute('aria-label')}, ${missingOf(lens, card).join(' and ')} unknown`);
  // Replace the dot's own click (open the editor) with the quick answer.
  dot.addEventListener('click', (e) => {
    e.stopImmediatePropagation();
    openAsk(card.id, dot);
  }, { capture: true });
  return dot;
}

function renderUnknown(lens, cards) {
  const pocket = document.createElement('aside');
  pocket.className = 'matrix-unknown';
  pocket.setAttribute('aria-label', 'Cards this matrix cannot place yet');
  const head = document.createElement('div');
  head.className = 'matrix-unknown-head';
  const label = document.createElement('span');
  label.className = 'matrix-unknown-label';
  label.textContent = 'Unknown';
  const count = document.createElement('span');
  count.className = 'matrix-quad-count';
  count.textContent = cards.length;
  head.append(label, count);
  pocket.append(head);

  const lanes = lens.needs.length === 2
    ? [['both unknown', (c) => !c.importance && !c.urgency],
      ['importance ?', (c) => !c.importance && c.urgency],
      ['urgency ?', (c) => c.importance && !c.urgency]]
    : [['importance ?', () => true]];
  for (const [name, test] of lanes) {
    const these = cards.filter(test);
    if (!these.length) continue;
    const lane = document.createElement('div');
    lane.className = 'matrix-unknown-lane';
    const what = document.createElement('span');
    what.className = 'matrix-unknown-what';
    what.textContent = `${name} · ${these.length}`;
    const dots = document.createElement('div');
    dots.className = 'matrix-unknown-dots';
    for (const c of these) dots.append(unknownDot(c, lens));
    lane.append(what, dots);
    pocket.append(lane);
  }
  if (!cards.length) {
    const done = document.createElement('p');
    done.className = 'matrix-unknown-empty';
    done.textContent = 'Every card is placed.';
    pocket.append(done);
  }
  return pocket;
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
  const unknown = unknownCards(lens);
  status.textContent = `${placed.length} placed`
    + (unknown.length ? ` · ${unknown.length} unknown, waiting in the corner for ${lens.awaiting}` : '');
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
      // Half-known: importance set, urgency not. Marked on the edge of the
      // row it already belongs to; the corner holds it too.
      if (x === 0 && lens.needs.includes('urgency')) {
        const half = unknown.filter((c) => c.importance === imp && !c.urgency);
        if (half.length) {
          const edge = document.createElement('div');
          edge.className = 'matrix-edge';
          edge.title = 'Importance known, urgency not yet';
          for (const c of half) edge.append(unknownDot(c, lens));
          cell.append(edge);
        }
      }
      grid.append(cell);
    });
  }

  const pocket = renderUnknown(lens, unknown);
  pocket.style.gridRow = '4';
  grid.append(pocket);
  sheet.append(grid);
  return sheet;
}
