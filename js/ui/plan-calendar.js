import { WEEKDAYS, monthGrid } from '../core/calendar.js';

// The plan, chosen on a calendar — one month at a time, pressed once.
//
// The plan is a *partial* date (`2027`, `2027-03`, `2027-03-04`), and the three
// dropdowns beside this grid are what make that sayable: a calendar can only
// ever name a whole day. So this is a second way in and never a replacement —
// press a day to say all three at once, and use the two controls at the foot to
// take the day back off, or the month with it. Both of those write through the
// dropdowns' own values, exactly as `js/ui/filter-menu.js` presses a facet by
// dispatching the row's own `change`: one writer of the plan, two surfaces.
//
// It holds no date maths. `monthGrid` in js/core/calendar.js — which imports
// nothing and is pinned under plain node — already owns whole weeks, the
// neighbouring days and the month-length arithmetic that a second renderer
// would be a second place to get February wrong in.

// Which month is on show. Not the plan: a reader who pages to April and then
// clears the day has not left April, so the grid stays where they put it.
let month = '';
// The plan's own month the last time the grid was seeded from it. Without it,
// "does the plan name a month the grid is not showing" is true for the whole
// time the reader is paging — so `›` would move the grid and the next repaint
// would put it straight back, and the two step buttons would do nothing at all.
// With it, the grid follows the plan when the *plan* moves and stays put
// otherwise.
let seeded = '';

/** Forget which month was last on show. Called when the dialog opens on another
 *  card — a grid still showing last card's June is a calendar answering a
 *  question nobody asked. */
export function resetPlanCalendar() { month = ''; seeded = ''; }

/** Paint the grid into `host` for `plan`, and hand a new plan to `onPick`.
 *
 *  `onPick` receives the plan as a string at the precision the press meant:
 *  a whole day from the grid, `YYYY-MM` from *the day cleared*, `YYYY` from the
 *  month cleared. The caller repaints, which is why the pressed day is focused
 *  after the call and not before — the button under the pointer is gone by
 *  then, and a grid that rebuilds without handing focus back is a calendar the
 *  keyboard falls out of. */
export function renderPlanCalendar(host, plan, onPick) {
  const [year = '', mon = '', day = ''] = (plan || '').split('-');
  const chosen = year && mon && day ? plan : '';
  // Seeded from the plan the first time, and again whenever the plan's own
  // month changes — the dropdowns are the other way in, and a grid ignoring
  // them would disagree with the row above it. A plan that loses its month
  // (`Clear the month`) leaves the grid where it stands rather than sending it
  // back to today: nothing was said about where to go.
  const seed = year && mon ? `${year}-${mon}-01` : '';
  if (!month || seed !== seeded) month = seed || month;
  seeded = seed;

  const grid = monthGrid(month || chosen || '');
  month = `${grid.year}-${String(grid.month).padStart(2, '0')}-01`;
  host.replaceChildren();

  const head = document.createElement('div');
  head.className = 'plan-cal-head';
  for (const [dir, glyph, to] of [['prev', '‹', grid.prev], ['next', '›', grid.next]]) {
    const step = document.createElement('button');
    step.type = 'button';
    step.className = 'plan-cal-step';
    step.textContent = glyph;
    step.setAttribute('aria-label', dir === 'prev' ? 'The month before' : 'The month after');
    step.addEventListener('click', () => {
      month = to;
      renderPlanCalendar(host, plan, onPick);
      host.querySelector('.plan-cal-step')?.focus();
    });
    head.append(step);
  }
  const name = document.createElement('span');
  name.className = 'plan-cal-month';
  name.textContent = grid.label;
  head.insertBefore(name, head.lastChild);
  host.append(head);

  const names = document.createElement('div');
  names.className = 'plan-cal-week';
  // aria-hidden: every day button carries its own full date, so seven initials
  // read before them are a header said twice.
  names.setAttribute('aria-hidden', 'true');
  for (const d of WEEKDAYS) {
    const cell = document.createElement('span');
    cell.textContent = d.slice(0, 1);
    names.append(cell);
  }
  host.append(names);

  const days = document.createElement('div');
  days.className = 'plan-cal-grid';
  days.setAttribute('role', 'group');
  days.setAttribute('aria-label', 'Plan this card for a day');
  for (const cell of grid.days) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'plan-day';
    btn.dataset.day = cell.iso;
    btn.textContent = String(cell.day);
    if (!cell.inMonth) btn.classList.add('outside');
    if (cell.iso === chosen) btn.classList.add('chosen');
    btn.setAttribute('aria-pressed', String(cell.iso === chosen));
    btn.title = cell.iso;
    btn.setAttribute('aria-label', cell.iso);
    btn.addEventListener('click', () => {
      onPick(cell.iso);
      host.querySelector(`.plan-day[data-day="${cell.iso}"]`)?.focus();
    });
    days.append(btn);
  }
  host.append(days);

  // Taking precision back off, which is the one thing a calendar cannot say.
  // Clearing the month takes the day with it, because a plan cannot name the
  // 4th of nothing — said in the control's own words rather than left as a
  // surprise.
  const less = document.createElement('div');
  less.className = 'plan-cal-less';
  for (const [label, hint, enabled, next] of [
    ['Clear the day', 'Plan it for the month instead of the day', Boolean(day), `${year}-${mon}`],
    ['Clear the month', 'Plan it for the year — the day goes with the month', Boolean(mon), year],
  ]) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'plan-cal-clear';
    btn.textContent = label;
    btn.title = hint;
    btn.disabled = !enabled;
    btn.addEventListener('click', () => onPick(next));
    less.append(btn);
  }
  host.append(less);
}
