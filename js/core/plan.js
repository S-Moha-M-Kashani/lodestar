// The plan — when a card is meant to happen.
//
// A plan is a *partial* calendar date: '2027' (some time that year),
// '2027-03' (that month) or '2027-03-04' (that day). One string rather than
// three fields, because the rule the user asked for — a day needs a month, a
// month needs a year — is then structural instead of a validation every writer
// has to remember: '2027-03' cannot carry a stray day, the precision is
// readable off the length, it sorts as text, and a deadline copies into it
// verbatim.
//
// A plan is not a deadline. The deadline is when a thing is due; the plan is
// when you mean to get to it. They are linked in one direction only: while
// nobody has set a plan by hand it mirrors the deadline, and once someone has,
// the deadline never overwrites it again. The one pair the app refuses is a
// plan that *starts* after the deadline — an intention to begin after the
// thing was already due.
//
// Like js/core/merge.js this module imports nothing, so node can test it.

// The rail read one horizon at a time: the near ones accumulate, 'next' and
// 'dream' stand alone (see HORIZON_SECTIONS).
export const PLAN_HORIZONS = [
  { id: 'today', label: 'Today' },
  { id: 'week', label: 'This week' },
  { id: 'month', label: 'This month' },
  { id: 'year', label: 'This year' },
  { id: 'next', label: 'Next year' },
  { id: 'dream', label: 'Life dream' },
];

// The rail read all at once. A card's plan puts it in one of the first five;
// being a dream puts it in the last as well, so a dream with a date is listed
// twice on purpose — it is a want *and* something happening in March.
export const PLAN_SECTIONS = [
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
  { id: 'year', label: 'Year' },
  { id: 'next', label: 'Next year' },
  { id: 'dreams', label: 'Dreams' },
];

// Which sections a horizon shows. The near ones accumulate — "this week"
// includes today, because a week you cannot see today in is not this week.
//
// 'next' does not, and that is the one exception. Accumulating it made the
// picker offer two entries that answered the same question: with nothing
// planned for next year, "Next year" repeated "This year" row for row, and a
// board where most years are empty is the normal case. It is the only frame
// whose whole point is what is NOT in the nearer ones. 'dream' stands alone
// too: it is every dream on the board, dated or not.
const HORIZON_SECTIONS = {
  today: ['day'],
  week: ['day', 'week'],
  month: ['day', 'week', 'month'],
  year: ['day', 'week', 'month', 'year'],
  next: ['next'],
  dream: ['dreams'],
};

const HORIZON_IDS = PLAN_HORIZONS.map((h) => h.id);
export const planHorizonVal = (v) => (HORIZON_IDS.includes(v) ? v : 'today');
export const planHorizonLabel = (id) =>
  PLAN_HORIZONS.find((h) => h.id === planHorizonVal(id)).label;

/** Who set the plan. 'auto' means it mirrors the deadline. */
export const planSrcVal = (v) => (v === 'user' || v === 'ai' ? v : 'auto');

const pad2 = (n) => String(n).padStart(2, '0');
/** A local calendar day, as a deadline and a day-precision plan are stored. */
export const planDay = (date = new Date()) =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;

const daysInMonth = (year, month) => new Date(year, month, 0).getDate();

/** Coerce to a legal plan, dropping only the part that does not hold up: a
 *  30th of February leaves '2027-02', not ''. Losing a year someone typed
 *  because the day was wrong would be the worse trade. */
export function planVal(v) {
  if (typeof v !== 'string') return '';
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$/.exec(v.trim());
  if (!m) return '';
  const year = Number(m[1]);
  if (year < 1900 || year > 2999) return '';
  if (m[2] === undefined) return m[1];
  const month = Number(m[2]);
  if (month < 1 || month > 12) return m[1];
  if (m[3] === undefined) return `${m[1]}-${m[2]}`;
  const day = Number(m[3]);
  if (day < 1 || day > daysInMonth(year, month)) return `${m[1]}-${m[2]}`;
  return `${m[1]}-${m[2]}-${m[3]}`;
}

/** '' | 'year' | 'month' | 'day' — how precisely the plan was made. */
export const planPrecision = (plan) =>
  ({ 4: 'year', 7: 'month', 10: 'day' })[planVal(plan).length] || '';

/** The first day the plan covers. */
export function planStart(plan) {
  const p = planVal(plan);
  if (!p) return '';
  return p.length === 4 ? `${p}-01-01` : p.length === 7 ? `${p}-01` : p;
}

/** The last day the plan covers — the end of the year, of the month (short
 *  and leap ones included), or the day itself. */
export function planEnd(plan) {
  const p = planVal(plan);
  if (!p) return '';
  if (p.length === 4) return `${p}-12-31`;
  if (p.length === 7) {
    const [y, m] = p.split('-').map(Number);
    return `${p}-${pad2(daysInMonth(y, m))}`;
  }
  return p;
}

/** The plan a card should actually carry: the deadline while nobody has set
 *  one by hand, and otherwise exactly what was set — an empty plan on a dated
 *  card included, which is a real state ("dated, undecided"). */
export function resolvePlan({ plan, planSrc, deadline }) {
  return planSrcVal(planSrc) === 'auto' ? planVal(deadline) : planVal(plan);
}

/** True when the plan could not possibly be kept: its window opens after the
 *  card was due. The *start* is what counts, so "in 2026, due 1 September
 *  2026" stays legal. */
export function planConflict(plan, deadline) {
  const start = planStart(plan);
  return Boolean(start && deadline) && start > deadline;
}

/** Whether a card can be planned at all: habits repeat on a calendar of their
 *  own and carry their own strip, and a finished card is finished. */
const plannable = (card) => card.columnId !== 'answered' && card.type !== 'habit';

/** True for a dream — the Dreams list, whatever its dates. A type, not a life
 *  area: a dream still belongs to travel or love, and the rail should not have
 *  to spend a card's one category on saying "this is a want". */
export const isDream = (card) => plannable(card) && card.type === 'dream';

/** The dated section a card belongs in — the nearest calendar frame its plan
 *  fits inside, or '' when there is no plan to place. Overdue is folded into
 *  the day whatever precision it was planned at: a plan whose window has
 *  closed is today's problem. The type plays no part here beyond habits: a
 *  dream is filed by its date like anything else, and *also* listed as a
 *  dream. */
export function planSection(card, at = new Date()) {
  if (!plannable(card)) return '';

  const plan = resolvePlan(card);
  if (!plan) return '';

  const today = planDay(at);
  if (planEnd(plan) < today) return 'day'; // overdue
  const precision = planPrecision(plan);
  const year = today.slice(0, 4);
  const month = today.slice(0, 7);
  // 'next' is next year *and* the years after it. Their rows carry their own
  // dates, so a plan for 2031 says 2031 under a heading that says next.
  const far = plan.slice(0, 4) > year ? 'next' : 'year';

  if (precision === 'year') return far;
  if (precision === 'month') return plan === month ? 'month' : far;

  if (plan === today) return 'day';
  // The ISO week the habits already count in: Monday to Sunday.
  const sunday = new Date(at.getFullYear(), at.getMonth(), at.getDate());
  sunday.setDate(sunday.getDate() + ((7 - (sunday.getDay() || 7)) % 7));
  if (plan <= planDay(sunday)) return 'week';
  return plan <= planEnd(month) ? 'month' : far;
}

// Nearest date first, then the ledger number — the order a plan is read in.
const byWhen = (a, b) =>
  (planStart(resolvePlan(a)) || '9999').localeCompare(planStart(resolvePlan(b)) || '9999')
  || (a.num || 0) - (b.num || 0);

/** Every section with its cards, for the stacked layout. `pass` is the caller's
 *  own filter — the board's, when the rail is asked to apply it.
 *
 *  Dreams is the one list a card can be in *as well as* another: a dream
 *  planned for this month belongs to the month, and is still a dream. Every
 *  other card has exactly one home. */
export function planGroups(cards, at = new Date(), pass = () => true) {
  const bins = Object.fromEntries(PLAN_SECTIONS.map((s) => [s.id, []]));
  for (const card of cards) {
    if (!pass(card)) continue;
    const section = planSection(card, at);
    if (section) bins[section].push(card);
    if (isDream(card)) bins.dreams.push(card);
  }
  return PLAN_SECTIONS.map((s) => ({ ...s, cards: bins[s.id].sort(byWhen) }));
}

/** One horizon's cards, for the dropdown layout — the nearer sections come
 *  with it. The dreams horizon is the life area itself, dated or not. */
export function planCardsIn(cards, horizon, at = new Date(), pass = () => true) {
  const id = planHorizonVal(horizon);
  const wanted = new Set(HORIZON_SECTIONS[id]);
  const inHorizon = id === 'dream'
    ? isDream
    : (c) => wanted.has(planSection(c, at));
  return cards.filter((c) => pass(c) && inHorizon(c)).sort(byWhen);
}
