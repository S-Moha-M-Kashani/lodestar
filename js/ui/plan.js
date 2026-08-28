import { cardLabel, moveCard } from '../core/cards.js';
import { catColor } from '../core/categories.js';
import { KEY_PREFIX } from '../core/keys.js';
import { PLAN_HORIZONS, planCardsIn, planDay, planHorizonLabel, planHorizonVal } from '../core/plan.js';
import { state } from '../core/state.js';
import { announce } from './dom.js';
import { openDialog } from './edit-dialog.js';
import { render } from './render.js';

// The plan section of the rail — the day's shortlist, under the habits.
//
// It borrows the habit strip's box on purpose: on this board a box you tick is
// one thing done, whether it is the fourth glass of water or the tax return.
// The difference is what the tick means. A habit's box records a repetition and
// the card stays; a plan's box finishes the card, so it moves to Done and
// leaves the list. That is the only feedback the click needs.

const HORIZON_KEY = KEY_PREFIX + 'plan-horizon';
let horizon = planHorizonVal(localStorage.getItem(HORIZON_KEY));

function setHorizon(next) {
  horizon = planHorizonVal(next);
  try { localStorage.setItem(HORIZON_KEY, horizon); } catch (_) { /* private mode */ }
  render();
  announce(`Plan: ${planHorizonLabel(horizon)}`);
}

const EMPTY = {
  today: 'Nothing due today.',
  week: 'Nothing due this week.',
  month: 'Nothing due this month.',
  year: 'Nothing due this year.',
  dream: 'Every card carries a date.',
};

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

  // The date, only where it says something the heading does not: overdue in
  // red, and any deadline past the horizon's own name.
  if (card.deadline && card.deadline !== today) {
    const when = document.createElement('span');
    when.className = 'plan-rail-when' + (card.deadline < today ? ' late' : '');
    when.textContent = card.deadline.slice(5); // MM-DD; the year is the horizon's job
    when.title = card.deadline < today ? `Overdue — was due ${card.deadline}` : `Due ${card.deadline}`;
    row.append(when);
  }
  return row;
}

/** The plan, beside the board. Always present, even empty: unlike the habit
 *  rail it is where a card is *put*, so it has to be somewhere to aim at. */
export function renderPlanRail() {
  const today = planDay();
  const cards = planCardsIn(state.cards, horizon);

  const section = document.createElement('section');
  section.className = 'plan-rail';
  section.setAttribute('aria-label', 'Plan');

  const head = document.createElement('div');
  head.className = 'plan-rail-head';
  const title = document.createElement('h2');
  title.className = 'plan-rail-title';
  title.textContent = 'Plan';
  const sub = document.createElement('p');
  sub.className = 'plan-rail-sub';
  sub.textContent = horizon === 'dream'
    ? `${cards.length} dreams`
    : `${cards.length} planned`;
  head.append(title, horizonPicker(), sub);
  section.append(head);

  if (!cards.length) {
    const empty = document.createElement('p');
    empty.className = 'plan-rail-empty';
    empty.textContent = EMPTY[horizon];
    section.append(empty);
    return section;
  }

  for (const card of cards) section.append(planRow(card, today));
  return section;
}
