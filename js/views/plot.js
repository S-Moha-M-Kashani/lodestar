import { cardLabel, iuVal } from '../core/cards.js';
import { catColor, catLabel, categories } from '../core/categories.js';
import { state } from '../core/state.js';
import { cardAria, typeBadge } from '../ui/board.js';
import { wireCardContext } from '../ui/card-menu.js';
import { columnTitle } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';

// Plotted views — shared "stamped card dots" on the engineering grid.
// Overview (a semantic map) and the Matrix both place cards as dots that
// reveal an index-card tooltip on hover and open the full editor on click.
// Each dot is inked in its category's colour, so the map reads by life area.

const IU_LABEL = { high: 'High', low: 'Low', '': 'not set' };

function dotAriaLabel(card) {
  let s = cardAria(card);
  if (card.importance || card.urgency) {
    s += `, importance ${IU_LABEL[iuVal(card.importance)]}, urgency ${IU_LABEL[iuVal(card.urgency)]}`;
  }
  return s;
}

// One shared tooltip, moved to whichever dot is hovered or focused.
let plotTip = null;
function ensurePlotTip() {
  if (plotTip) return plotTip;
  plotTip = document.createElement('div');
  plotTip.className = 'plot-tip';
  plotTip.hidden = true;
  document.body.append(plotTip);
  return plotTip;
}

function showPlotTip(card, dotEl) {
  const tip = ensurePlotTip();
  tip.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'plot-tip-head';
  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  head.append(num, typeBadge(card));

  const title = document.createElement('p');
  title.className = 'plot-tip-title';
  title.textContent = card.title;

  const meta = document.createElement('p');
  meta.className = 'plot-tip-meta';
  meta.textContent = `in ${columnTitle(card.columnId)}`;
  if (card.category) meta.textContent += ` · ${catLabel(card.category)}`;
  if (card.importance || card.urgency) {
    meta.textContent += ` · importance ${IU_LABEL[iuVal(card.importance)]} · urgency ${IU_LABEL[iuVal(card.urgency)]}`;
  }

  tip.append(head, title, meta);

  if (card.notes.trim()) {
    const notes = document.createElement('p');
    notes.className = 'plot-tip-notes';
    notes.textContent = card.notes.trim();
    tip.append(notes);
  }
  if (card.tags.length) {
    const tags = document.createElement('div');
    tags.className = 'card-tags';
    for (const t of card.tags) {
      const chip = document.createElement('span');
      chip.className = 'card-tag';
      chip.textContent = t;
      tags.append(chip);
    }
    tip.append(tags);
  }

  tip.hidden = false;
  positionPlotTip(dotEl);
}

function positionPlotTip(dotEl) {
  if (!plotTip || plotTip.hidden) return;
  const r = dotEl.getBoundingClientRect();
  const t = plotTip.getBoundingClientRect();
  let left = r.left + r.width / 2 - t.width / 2;
  let top = r.top - t.height - 10;
  if (top < 8) top = r.bottom + 10; // flip below when there's no room above
  left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
  plotTip.style.left = `${left}px`;
  plotTip.style.top = `${top}px`;
}

export const hidePlotTip = () => { if (plotTip) plotTip.hidden = true; };

export function renderPlotDot(card, leftPct, topPct) {
  const dot = document.createElement('button');
  dot.type = 'button';
  dot.className = 'plot-dot';
  dot.dataset.id = card.id;
  dot.dataset.col = card.columnId;
  // Overview passes fractions to place dots absolutely; the Matrix omits them
  // and lets the dots flow inside their quadrant (positioned via CSS).
  if (leftPct != null) dot.style.left = `${leftPct}%`;
  if (topPct != null) dot.style.top = `${topPct}%`;
  dot.style.setProperty('--dot', catColor(card.category));
  dot.setAttribute('aria-label', dotAriaLabel(card));

  const n = document.createElement('span');
  n.className = 'plot-dot-num';
  n.textContent = String(card.num);
  dot.append(n);

  dot.addEventListener('click', () => openDialog(card.id));
  // A dot is the same card at its smallest, and it answers the same right-click.
  // The panel cannot hang inside it — a dot is itself a button — so this is the
  // case the floating copy exists for. The tooltip goes first: the pointer is
  // still on the dot, so it would sit over the menu it just summoned.
  dot.addEventListener('contextmenu', hidePlotTip);
  wireCardContext(dot, card.id);
  dot.addEventListener('mouseenter', () => showPlotTip(card, dot));
  dot.addEventListener('mouseleave', hidePlotTip);
  dot.addEventListener('focus', () => showPlotTip(card, dot));
  dot.addEventListener('blur', hidePlotTip);
  return dot;
}

export function renderPlotLegend() {
  const legend = document.createElement('div');
  legend.className = 'plot-legend';
  const inUse = new Set(state.cards.map((c) => c.category));
  const entries = categories.filter((c) => inUse.has(c.id)).map((c) => [c.id, c.label]);
  if (inUse.has('')) entries.push(['', 'Uncategorized']);
  for (const [id, label] of entries) {
    const item = document.createElement('span');
    item.className = 'plot-legend-item';
    item.style.setProperty('--dot', catColor(id));
    item.textContent = label;
    legend.append(item);
  }
  return legend;
}

export function plotEmptyHint(text) {
  const hint = document.createElement('div');
  hint.className = 'empty-hint plot-empty';
  hint.textContent = text;
  return hint;
}
