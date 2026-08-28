import { cardLabel, matchesFilters, moveCard } from '../core/cards.js';
import { catColor } from '../core/categories.js';
import { KEY_PREFIX } from '../core/keys.js';
import {
  PLAN_HORIZONS, planCardsIn, planConflict, planDay, planGroups, planHorizonLabel,
  planHorizonVal, planPrecision,
} from '../core/plan.js';
import { state } from '../core/state.js';
import { $, announce } from './dom.js';
import { openDialog } from './edit-dialog.js';
import { render } from './render.js';

// The plan section of the rail — what you mean to do, under the habits.
//
// It borrows the habit strip's box on purpose: on this board a box you tick is
// one thing done, whether it is the fourth glass of water or the tax return.
// The difference is what the tick means. A habit's box records a repetition and
// the card stays; a plan's box finishes the card, so it moves to Done and
// leaves the list.
//
// Two layouts, because the same list answers two different questions. Stacked
// shows day, week, month, year and dreams at once — the whole horizon, each
// card in exactly one place. Dropdown shows one horizon at a time, and then it
// accumulates: "this week" includes today. Which one you get is a setting in
// the ⚙ menu, not a guess.
//
// And the block is outside the board's filters by default. The point of a plan
// is to see the day whole; a category tab left on an hour ago would quietly
// hide half of it. `apply board filters` is the one button that opts in.

const LAYOUT_KEY = KEY_PREFIX + 'plan-layout';
const HORIZON_KEY = KEY_PREFIX + 'plan-horizon';
const FILTERS_KEY = KEY_PREFIX + 'plan-filters';

const layoutVal = (v) => (v === 'dropdown' ? 'dropdown' : 'stacked');
export let planLayout = layoutVal(localStorage.getItem(LAYOUT_KEY));
let horizon = planHorizonVal(localStorage.getItem(HORIZON_KEY));
let useFilters = localStorage.getItem(FILTERS_KEY) === '1';

const remember = (key, value) => {
  try { localStorage.setItem(key, value); } catch (_) { /* private mode */ }
};

/** Stacked or one at a time. Wired from main.js, like the habit chime. */
export function setPlanLayout(next) {
  planLayout = layoutVal(next);
  remember(LAYOUT_KEY, planLayout);
}

/** Mark the chosen layout in the ⚙ menu's Plan submenu. */
export function syncPlanLayoutPicker() {
  for (const name of ['stacked', 'dropdown']) {
    $(`#plan-${name}`)?.setAttribute('aria-checked', String(name === planLayout));
  }
}

function setHorizon(next) {
  horizon = planHorizonVal(next);
  remember(HORIZON_KEY, horizon);
  render();
  announce(`Plan: ${planHorizonLabel(horizon)}`);
}

function setUseFilters(on) {
  useFilters = on;
  remember(FILTERS_KEY, on ? '1' : '0');
  render();
  announce(on ? 'Plan follows the board filters' : 'Plan shows everything planned');
}

// --- the pieces -------------------------------------------------------------

function horizonPicker() {
  const pick = document.createElement('select');
  pick.className = 'plan-rail-pick';
  pick.setAttribute('aria-label', 'How far ahead to plan');
  pick.title = 'How far ahead to plan';
  for (const h of PLAN_HORIZONS) {
    const opt = document.createElement('option');
    opt.value = h.id;
    opt.textContent = h.id === 'today' ? 'Plan today' : h.label;
    opt.selected = h.id === horizon;
    pick.append(opt);
  }
  pick.addEventListener('change', () => setHorizon(pick.value));
  return pick;
}

function filterToggle() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'plan-filter-toggle' + (useFilters ? ' on' : '');
  btn.textContent = 'apply board filters';
  btn.setAttribute('aria-pressed', String(useFilters));
  btn.title = useFilters
    ? 'The plan is showing only cards the board filters keep'
    : 'Narrow the plan with the board’s search, tabs and filters';
  btn.addEventListener('click', () => setUseFilters(!useFilters));
  return btn;
}

/** The date a row shows, at the precision it was planned at. Today's own date
 *  is left off — the section says it — and a plan that has slipped, or that
 *  starts after the deadline, is marked. */
function whenChip(card, today) {
  const plan = card.plan;
  if (!plan || plan === today) return null;
  const chip = document.createElement('span');
  const late = plan < today;
  chip.className = 'plan-rail-when' + (late ? ' late' : '')
    + (planConflict(plan, card.deadline) ? ' conflict' : '');
  // A day this year needs no year; a month or a whole year says itself.
  chip.textContent = planPrecision(plan) === 'day' && plan.slice(0, 4) === today.slice(0, 4)
    ? plan.slice(5) : plan;
  chip.title = planConflict(plan, card.deadline)
    ? `Planned ${plan}, but due ${card.deadline}`
    : late ? `Planned for ${plan} — that has passed` : `Planned for ${plan}`;
  return chip;
}

function planRow(card, today) {
  const row = document.createElement('div');
  row.className = 'plan-rail-row';
  row.style.setProperty('--cat', catColor(card.category));
  if (card.category) row.classList.add('categorized');

  const box = document.createElement('button');
  box.type = 'button';
  box.className = 'punch-box plan-box';
  box.textContent = '✓';
  box.title = 'Finish this — moves it to Done';
  box.setAttribute('aria-label', `Finish ${cardLabel(card)} ${card.title}`);
  box.addEventListener('click', () => {
    moveCard(card.id, 'answered'); // writes its own timeline entry, and repaints
    announce(`Done: ${card.title}`);
  });

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'plan-rail-open';
  name.textContent = card.title;
  name.title = 'Open this card';
  name.addEventListener('click', () => openDialog(card.id));

  row.append(box, name);
  const when = whenChip(card, today);
  if (when) row.append(when);
  return row;
}

function sectionHead(label, n) {
  const head = document.createElement('div');
  head.className = 'plan-group-head';
  const title = document.createElement('h3');
  title.className = 'plan-group-title';
  title.textContent = label;
  const count = document.createElement('span');
  count.className = 'plan-group-count';
  count.textContent = String(n);
  head.append(title, count);
  return head;
}

/** The plan, beside the board. Always present, even empty: unlike the habit
 *  rail it is where a card is *put*, so it has to be somewhere to aim at. */
export function renderPlanRail() {
  const today = planDay();
  const pass = useFilters ? matchesFilters : () => true;

  const section = document.createElement('section');
  section.className = 'plan-rail';
  section.setAttribute('aria-label', 'Plan');

  const head = document.createElement('div');
  head.className = 'plan-rail-head';
  const title = document.createElement('h2');
  title.className = 'plan-rail-title';
  title.textContent = 'Plan';
  head.append(title);
  if (planLayout === 'dropdown') head.append(horizonPicker());
  head.append(filterToggle());
  section.append(head);

  const body = document.createElement('div');
  body.className = 'plan-rail-body';

  let shown = 0;
  if (planLayout === 'stacked') {
    for (const group of planGroups(state.cards, new Date(), pass)) {
      if (!group.cards.length) continue;
      shown += group.cards.length;
      body.append(sectionHead(group.label, group.cards.length));
      for (const card of group.cards) body.append(planRow(card, today));
    }
  } else {
    const cards = planCardsIn(state.cards, horizon, new Date(), pass);
    shown = cards.length;
    if (cards.length) body.append(sectionHead(planHorizonLabel(horizon), cards.length));
    for (const card of cards) body.append(planRow(card, today));
  }

  if (!shown) {
    const empty = document.createElement('p');
    empty.className = 'plan-rail-empty';
    empty.textContent = useFilters
      ? 'Nothing planned matches the board filters.'
      : planLayout === 'dropdown'
        ? `Nothing planned for ${planHorizonLabel(horizon).toLowerCase()}.`
        : 'Nothing planned yet. Give a card a plan from its ＋ menu.';
    body.append(empty);
  }

  section.append(body);
  return section;
}
