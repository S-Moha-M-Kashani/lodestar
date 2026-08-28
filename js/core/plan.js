// Plans — the same board, read at five distances.
//
// A plan is not a new kind of card and not a new list: it is the cards whose
// deadline falls inside a horizon. "Plan today" is a deadline of today, and
// the wider horizons are the same question asked of a later last day, so they
// nest — what is due today is also due this week. Nothing is stored for any of
// this, which is why a card pulled forward by a day appears in the plan the
// moment its deadline changes, with no second copy to keep in step.
//
// Overdue cards belong to the *nearest* horizon rather than to none: a date
// that has passed is the first thing a plan for today has to say.
//
// Like js/core/merge.js this module imports nothing, so tests can load it
// under node.

export const PLAN_HORIZONS = [
  { id: 'today', label: 'Today' },
  { id: 'week', label: 'This week' },
  { id: 'month', label: 'This month' },
  { id: 'year', label: 'This year' },
  // The other half of the board: everything that was never pinned to a date.
  { id: 'dream', label: 'Life dream' },
];

const HORIZON_IDS = PLAN_HORIZONS.map((h) => h.id);
export const planHorizonVal = (v) => (HORIZON_IDS.includes(v) ? v : 'today');
export const planHorizonLabel = (id) =>
  PLAN_HORIZONS.find((h) => h.id === planHorizonVal(id)).label;

const pad2 = (n) => String(n).padStart(2, '0');
/** A local calendar day as the ISO date a deadline is stored as. */
export const planDay = (date = new Date()) =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;

/** The last day a horizon covers — '' for the one that has none. Weeks end on
 *  Sunday, matching the Monday-based week habits already count in. */
export function planHorizonEnd(horizon, at = new Date()) {
  const h = planHorizonVal(horizon);
  if (h === 'dream') return '';
  const d = new Date(at.getFullYear(), at.getMonth(), at.getDate());
  if (h === 'week') d.setDate(d.getDate() + ((7 - (d.getDay() || 7)) % 7));
  // Day 0 of the next month is the last day of this one, short months included.
  else if (h === 'month') d.setMonth(d.getMonth() + 1, 0);
  else if (h === 'year') d.setMonth(11, 31);
  return planDay(d);
}

/** The cards a horizon holds: still open, not a habit, earliest date first.
 *  Habits keep their own strip in the rail above and are counted by period,
 *  not finished by a date, so listing them here would say it twice. */
export function planCardsIn(cards, horizon, at = new Date()) {
  const h = planHorizonVal(horizon);
  const end = planHorizonEnd(h, at);
  return cards
    .filter((c) => c.columnId !== 'answered' && c.type !== 'habit'
      && (h === 'dream' ? !c.deadline : c.deadline && c.deadline <= end))
    .sort((a, b) => (a.deadline || '').localeCompare(b.deadline || '') || (a.num || 0) - (b.num || 0));
}
