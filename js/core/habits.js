import { state } from './state.js';

// Habits — the one card type that is not finished but repeated.
//
// `habitCount` times per `habitFreq` calendar period; `habitTimes` are
// optional clock slots that decide when the reminder fires, never the target
// (which is why "2× per year" needs no clock at all). `habitHistory` maps a
// period id to the instants it was done. None of it is cleared when the card
// changes type: a mis-stamp must not cost a year of completions.

const HABIT_FREQS = ['daily', 'weekly', 'monthly', 'yearly'];
const HABIT_MAX_COUNT = 99;
const HABIT_MAX_PERIODS = 400;
const HABIT_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const HABIT_PERIOD_RE =
  /^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?|-W(0[1-9]|[1-4]\d|5[0-3]))?$/;
// "2× per day" reads better than "2× per daily"; "0/2 today" better than
// "0/2 this day".
export const HABIT_EVERY = { daily: 'day', weekly: 'week', monthly: 'month', yearly: 'year' };
export const HABIT_NOW = { daily: 'today', weekly: 'this week', monthly: 'this month', yearly: 'this year' };

export const habitFreqVal = (v) => (HABIT_FREQS.includes(v) ? v : '');
export const habitCountVal = (v) => {
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) ? Math.min(HABIT_MAX_COUNT, Math.max(1, n)) : 1;
};
export const habitTimesVal = (v, count) => {
  if (!Array.isArray(v)) return [];
  const seen = new Set();
  for (const t of v) if (typeof t === 'string' && HABIT_TIME_RE.test(t)) seen.add(t);
  return [...seen].sort().slice(0, count);
};
export function habitHistoryVal(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
  const kept = [];
  for (const [period, stamps] of Object.entries(v)) {
    if (!HABIT_PERIOD_RE.test(period) || !Array.isArray(stamps)) continue;
    const clean = stamps.filter((t) => Number.isFinite(t)).sort((a, b) => a - b);
    if (clean.length) kept.push([period, clean]);
  }
  kept.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return Object.fromEntries(kept.slice(-HABIT_MAX_PERIODS));
}

export const pad2 = (n) => String(n).padStart(2, '0');

/** ISO week (Monday-based; the Thursday in the week decides the year). */
function isoWeek(date) {
  const t = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  const jan1 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return { year: t.getUTCFullYear(), week: Math.ceil(((t - jan1) / 86400000 + 1) / 7) };
}

/** The calendar period a moment falls in — local time, always. */
export function habitPeriod(freq, date = new Date()) {
  const y = date.getFullYear();
  if (freq === 'yearly') return String(y);
  if (freq === 'monthly') return `${y}-${pad2(date.getMonth() + 1)}`;
  if (freq === 'weekly') {
    const { year, week } = isoWeek(date);
    return `${year}-W${pad2(week)}`;
  }
  return `${y}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/** The last `n` period ids, oldest first, ending with the one we are in. */
export function habitPeriodsBack(freq, n, from = new Date()) {
  // Monthly and yearly steps walk from the 1st, so stepping back from the
  // 31st cannot skip a short month.
  const byDay = freq === 'daily' || freq === 'weekly';
  const d = new Date(from.getFullYear(), from.getMonth(), byDay ? from.getDate() : 1);
  const out = [];
  for (let i = 0; i < n; i++) {
    out.unshift(habitPeriod(freq, d));
    if (freq === 'yearly') d.setFullYear(d.getFullYear() - 1);
    else if (freq === 'monthly') d.setMonth(d.getMonth() - 1);
    else if (freq === 'weekly') d.setDate(d.getDate() - 7);
    else d.setDate(d.getDate() - 1);
  }
  return out;
}

export const isHabit = (card) => card.type === 'habit' && Boolean(card.habitFreq);
export const habitDoneIn = (card, period) => (card.habitHistory?.[period] || []).length;
export const habitDoneNow = (card) => habitDoneIn(card, habitPeriod(card.habitFreq));
/** Retired: a habit parked in Done stops counting and stops reminding. */
export const habitRetired = (card) => card.columnId === 'answered';
export const habitDue = (card) =>
  isHabit(card) && !habitRetired(card) && habitDoneNow(card) < card.habitCount;
export const habitCards = () => state.cards.filter(isHabit);

export const cadenceText = (card) => {
  const base = `${card.habitCount}× per ${HABIT_EVERY[card.habitFreq]}`;
  return card.habitTimes.length ? `${base} · ${card.habitTimes.join(', ')}` : base;
};
export const habitTally = (card) =>
  `${habitDoneNow(card)}/${card.habitCount} ${HABIT_NOW[card.habitFreq]}`;

/** Record one repetition, now. Returns false when the period is already full. */
export function punchHabit(card) {
  const period = habitPeriod(card.habitFreq);
  const stamps = (card.habitHistory[period] || []).slice();
  if (stamps.length >= card.habitCount) return false;
  stamps.push(Date.now());
  card.habitHistory = { ...card.habitHistory, [period]: stamps };
  card.updatedAt = Date.now();
  return true;
}

/** Take the newest repetition back — a mis-tap must be undoable, or the
 *  history stops being a record of what actually happened. */
export function unpunchHabit(card) {
  const period = habitPeriod(card.habitFreq);
  const stamps = (card.habitHistory[period] || []).slice();
  if (!stamps.length) return false;
  stamps.pop();
  const next = { ...card.habitHistory };
  if (stamps.length) next[period] = stamps;
  else delete next[period];
  card.habitHistory = next;
  card.updatedAt = Date.now();
  return true;
}
