import { expectedBy, slotsText, slotsVal } from './schedule.js';
import { state } from './state.js';

// Habits — the one card type that is not finished but repeated.
//
// `habitCount` times per `habitFreq` calendar period; `habitTimes` holds one
// schedule slot per repetition, saying when that repetition is expected —
// never how many there are, which is why "2× per year" needs no clock at all.
// The slot shape and its arithmetic live in core/schedule.js, which imports
// nothing and is therefore unit-testable; what is left here is the habit as a
// *card*. `habitHistory` maps a period id to the instants it was done. None of
// it is cleared when the card changes type: a mis-stamp must not cost a year of
// completions.

const HABIT_FREQS = ['daily', 'weekly', 'monthly', 'yearly'];
const HABIT_MAX_COUNT = 99;
const HABIT_MAX_PERIODS = 400;
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
// The schedule is one slot per repetition, validated in core/schedule.js.
// Re-exported under the field's own name so the card's validators read as a
// list of fields, and so the one place that knows the slot shape stays the one
// place: `slotsVal` neither sorts nor de-duplicates, unlike the clock-string
// validator this replaced — a slot is row n now, not a member of a set.
export const habitTimesVal = slotsVal;
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

const cadenceBase = (card) => `${card.habitCount}× per ${HABIT_EVERY[card.habitFreq]}`;
export const cadenceText = (card) => {
  const when = slotsText(card.habitFreq, card.habitTimes, card.habitCount);
  return when ? `${cadenceBase(card)} · ${when}` : cadenceBase(card);
};

/** The schedule read back in plain words, under the dialog's rows — the one
 *  honest check that three dropdowns per row say what the person meant. An
 *  unplaced schedule says what that means rather than saying nothing. */
export const scheduleSentence = (card) => {
  const when = slotsText(card.habitFreq, card.habitTimes, card.habitCount);
  const every = HABIT_EVERY[card.habitFreq];
  return when
    ? `${cadenceBase(card)} — ${when}.`
    : `${cadenceBase(card)}, due from the start of the ${every}.`;
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

/** How many repetitions the clock has asked for so far, for this card.
 *
 *  The arithmetic is core/schedule.js's; what is here is the card-shaped call,
 *  beside the rest of the habit domain. It read the clock and compared 'HH:MM'
 *  strings until 2026-09-07, which is why a habit could only ever say how often
 *  and never on which day. */
export const habitExpectedBy = (card, at = new Date()) =>
  expectedBy(card.habitFreq, card.habitTimes, card.habitCount, at);

/** Is this habit asking to be reminded of, right now? Due, and behind what the
 *  clock has asked for. */
export const habitReminding = (card, at = new Date()) =>
  habitDue(card) && habitDoneNow(card) < habitExpectedBy(card, at);
