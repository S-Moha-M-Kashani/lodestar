import { cardLabel, filtersActive, matchesFilters, moveCard } from '../core/cards.js';
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
// shows the whole horizon — day, week, month, year, next year and dreams, in
// that order, as ONE list. Dropdown shows one horizon at a time, and the near
// ones accumulate: "this week" includes today. Next year does not, because a
// frame that repeated this year's rows was a second entry answering the first
// one's question. Which layout you get is a setting in Settings, not a guess.
//
// The stacked layout was six named sections, each behind a rotated spine in its
// own ink, in the one panel whose whole job is to be the board's narrow margin.
// It is one list now, in the same order the sections were read in, so the rows
// a reader saw at the top are still at the top. One consequence is worth
// stating: a dream with a date used to be listed twice, once for when it
// happens and once for what it is. In one list that is one card printed twice,
// so `planList` keeps the first sighting and drops the second.
//
// **And the list follows the board's filters.** It was outside them, behind an
// opt-in switch on the PLAN heading — so a board narrowed to one question
// carried a margin answering a different one, and the switch was a control for
// a state nobody chose. The habits deliberately do NOT follow them: a habit
// hidden from the rail is a repetition nobody records, and the rail is the only
// place a habit can be punched.

const LAYOUT_KEY = KEY_PREFIX + 'plan-layout';
const HORIZON_KEY = KEY_PREFIX + 'plan-horizon';

const layoutVal = (v) => (v === 'dropdown' ? 'dropdown' : 'stacked');
export let planLayout = layoutVal(localStorage.getItem(LAYOUT_KEY));
let horizon = planHorizonVal(localStorage.getItem(HORIZON_KEY));

const remember = (key, value) => {
  try { localStorage.setItem(key, value); } catch (_) { /* private mode */ }
};

/** Stacked or one at a time. Wired from main.js, like the habit chime. */
export function setPlanLayout(next) {
  planLayout = layoutVal(next);
  remember(LAYOUT_KEY, planLayout);
}

/** Mark the chosen layout in the Settings dialog's Plan row. */
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

/** The heading. It was a button that opted the plan into the board's filters;
 *  the plan follows them now whatever it says, so what is left is the word.
 *  `plan-rail-title` stays on it — the class is what the e2e reads to find the
 *  name of a rail list, and the rule there is add, never rename. */
function planHeading() {
  const heading = document.createElement('h2');
  heading.className = 'plan-rail-heading plan-rail-title';
  heading.textContent = 'Plan';
  return heading;
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
  // The row is one line and the name is clipped to fit it, so the full title
  // has to be reachable another way — by hover and by screen reader alike. It
  // read "Open this card", which is what every one of these does and therefore
  // the one thing the tooltip need not say.
  name.title = card.title;
  name.setAttribute('aria-label', `Open ${cardLabel(card)} ${card.title}`);
  name.addEventListener('click', () => openDialog(card.id));

  row.append(box, name);
  const when = whenChip(card, today);
  if (when) row.append(when);
  return row;
}

/** The one-horizon layout's own heading, naming the horizon and counting it.
 *  Its classes lost the word `group` with the groups themselves: there is one
 *  section here and it is the whole list. */
function sectionHead(label, n) {
  const head = document.createElement('div');
  head.className = 'plan-rail-section-head';
  const title = document.createElement('h3');
  title.className = 'plan-rail-section-title';
  title.textContent = label;
  const count = document.createElement('span');
  count.className = 'plan-rail-section-count';
  count.textContent = String(n);
  head.append(title, count);
  return head;
}

/** The whole plan as one list, in the sections' own order — shortest horizon
 *  first, and `byWhen` inside each, which is exactly the order the six stacked
 *  sections were read in.
 *
 *  De-duplicated by card id, which is the one thing flattening has to decide: a
 *  dream with a date belongs to two sections, and two sections could print it
 *  twice legibly where one list would print the same row twice for no reason a
 *  reader could see. The first sighting wins, so a dated dream keeps the place
 *  its date gives it. */
function planList(cards, at, pass) {
  const out = [];
  const seen = new Set();
  for (const group of planGroups(cards, at, pass)) {
    for (const card of group.cards) {
      if (seen.has(card.id)) continue;
      seen.add(card.id);
      out.push(card);
    }
  }
  return out;
}

/** The plan, beside the board. Always present, even empty: unlike the habit
 *  rail it is where a card is *put*, so it has to be somewhere to aim at. */
export function renderPlanRail() {
  const today = planDay();
  const section = document.createElement('section');
  section.className = 'plan-rail';
  section.setAttribute('aria-label', 'Plan');

  const head = document.createElement('div');
  head.className = 'plan-rail-head';
  head.append(planHeading());
  if (planLayout === 'dropdown') head.append(horizonPicker());
  section.append(head);

  const body = document.createElement('div');
  body.className = 'plan-rail-body';

  // `matchesFilters` and not a rule of its own: it is the predicate the Board,
  // the Backlog, the map and the Matrix already read the same cards through, so
  // the margin and the columns beside it cannot disagree about what the board
  // is being asked. The rail already lists open cards only and `matchesColumn`
  // is inert on the Board, so the hide-Done default cannot surprise it.
  const cards = planLayout === 'stacked'
    ? planList(state.cards, new Date(), matchesFilters)
    : planCardsIn(state.cards, horizon, new Date(), matchesFilters);
  if (planLayout === 'dropdown' && cards.length) {
    body.append(sectionHead(planHorizonLabel(horizon), cards.length));
  }
  for (const card of cards) body.append(planRow(card, today));

  if (!cards.length) {
    // One line rather than an empty list, the `renderDirectionLine` note idiom:
    // a panel that simply goes blank reads as a bug, and a plan narrowed by the
    // filters must say so rather than imply the board holds no plan at all.
    const empty = document.createElement('p');
    empty.className = 'plan-rail-empty';
    empty.textContent = filtersActive()
      ? 'No planned card matches the filters in force.'
      : planLayout !== 'dropdown'
        ? 'Nothing planned yet. Give a card a plan from its ＋ menu.'
        : horizon === 'dream'
          ? 'No dreams yet. Stamp a card ☾ dream.'
          : `Nothing planned for ${planHorizonLabel(horizon).toLowerCase()}.`;
    body.append(empty);
  }

  section.append(body);
  return section;
}
