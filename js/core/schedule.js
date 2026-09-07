// A habit's schedule: one slot per repetition, saying when that repetition is
// expected.
//
// A slot is `{ month, weekday, day, time }`, every field an optional string. It
// lives in the field the habit already had — `habitTimes` keeps its name and
// its column, and its entries became objects. That was chosen over a new
// `habitSlots` field because a second column means a migration, a dead column
// left behind (SQLite is the only reader, and dropping one means rebuilding the
// table) and two names for one fact for as long as any board carries the old
// shape. Reusing the field puts the whole migration inside `slotVal`: a plain
// 'HH:MM' string coerces to a slot naming that hour, so every existing reminder
// survives the first read and nothing has to be backfilled. The name still
// describes it — these are the times the habit is expected, now with the day
// they fall on.
//
// This module imports nothing, the js/core/plan.js, js/core/draft.js,
// js/core/merge.js and js/core/nesting.js rule. core/habits.js reaches
// core/state.js for habitCards(), so anything left in there is an e2e-only
// surface; the validator and the expected-by arithmetic are exactly what wants
// a node test. server.js keeps its own copy of the validator rather than
// importing this file — see the comment there.

/** Which of a slot's fields the cadence actually reads. The one place this
 *  rule lives: `momentOf` reads it, the dialog disables its controls from it,
 *  and `slotText` speaks only what it names.
 *
 *  All four keys are *stored* whatever the cadence is — the same rule as
 *  "habit fields are never coupled to the card's type", one level down — so a
 *  habit moved from weekly to monthly and back has its weekday again. Which is
 *  also why the dialog shows the unread fields disabled rather than removing
 *  them: the rule is shown where it applies. */
export const SLOT_READS = {
  daily: { month: false, weekday: false, day: false, time: true },
  weekly: { month: false, weekday: true, day: false, time: true },
  monthly: { month: false, weekday: false, day: true, time: true },
  yearly: { month: true, weekday: false, day: true, time: true },
};

export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];
// Monday first, so weekday '1'…'7' indexes this directly and the week starts
// where habitPeriod's ISO week starts.
export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
  'Saturday', 'Sunday'];

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** A whole number in range, as a fixed-width string, or ''. A select sends a
 *  string and an imported file may send a number; anything else is not a
 *  value. `''` reads as 0, which is below every minimum here, so the blank
 *  option needs no case of its own. */
const numVal = (v, min, max, width) => {
  if (typeof v !== 'string' && typeof v !== 'number') return '';
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) && n >= min && n <= max ? String(n).padStart(width, '0') : '';
};

const timeVal = (v) => (typeof v === 'string' && TIME_RE.test(v) ? v : '');

/** One slot, validated field by field. A bad field blanks itself and the slot
 *  survives — the `category` precedent, because a slot is a row in a form and
 *  losing the row loses the rows numbered after it. */
export function slotVal(raw) {
  // The migration: everything stored before this change was a clock string.
  if (typeof raw === 'string') return { month: '', weekday: '', day: '', time: timeVal(raw) };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { month: '', weekday: '', day: '', time: '' };
  }
  return {
    month: numVal(raw.month, 1, 12, 2),
    weekday: numVal(raw.weekday, 1, 7, 1),
    day: numVal(raw.day, 1, 31, 2),
    time: timeVal(raw.time),
  };
}

/** Exactly `count` slots, in repetition order.
 *
 *  Padded with empty slots and truncated from the end, so the dialog can draw a
 *  row per repetition without asking whether the array is long enough, and
 *  "3× a day with no fixed hour" is three empty slots — which is the same thing
 *  the empty list meant before this change.
 *
 *  **No sorting and no de-duplication**, both of which the old `habitTimesVal`
 *  did. A slot is row *n*, the *n*th repetition: a validator that reorders rows
 *  or drops one as a duplicate is a form rearranging itself under the hand
 *  filling it in, and two repetitions may legitimately fall at one hour. */
export function slotsVal(v, count) {
  const list = Array.isArray(v) ? v : [];
  const out = [];
  for (let i = 0; i < count; i++) out.push(slotVal(list[i]));
  return out;
}

/** The last day of a month, so a slot naming the 31st in a thirty-day month is
 *  read as the 30th rather than skipped — and 29 February in a common year as
 *  the 28th. Day 0 of the next month is that. */
const lastDay = (year, monthIdx) => new Date(year, monthIdx + 1, 0).getDate();
const clampDay = (year, monthIdx, day) =>
  Math.min(Number(day) || 1, lastDay(year, monthIdx));

const hourMin = (time) => (time ? time.split(':').map(Number) : [0, 0]);

/** When inside the current period this slot falls, in local time — the start of
 *  the period with each field the cadence reads overridden by what the slot
 *  names. A slot naming nothing therefore lands on the period's own start,
 *  which is what makes an unscheduled repetition expected from the beginning
 *  with no branch for it. */
function momentOf(freq, slot, at) {
  const [hh, mm] = hourMin(slot.time);
  if (freq === 'weekly') {
    // Monday-based, matching habitPeriod's ISO week: getDay() calls Sunday 0,
    // and Sunday is this week's seventh day rather than next week's first.
    const monday = at.getDate() - ((at.getDay() || 7) - 1);
    return new Date(at.getFullYear(), at.getMonth(),
      monday + ((Number(slot.weekday) || 1) - 1), hh, mm);
  }
  if (freq === 'monthly') {
    return new Date(at.getFullYear(), at.getMonth(),
      clampDay(at.getFullYear(), at.getMonth(), slot.day), hh, mm);
  }
  if (freq === 'yearly') {
    const monthIdx = slot.month ? Number(slot.month) - 1 : 0;
    return new Date(at.getFullYear(), monthIdx,
      clampDay(at.getFullYear(), monthIdx, slot.day), hh, mm);
  }
  return new Date(at.getFullYear(), at.getMonth(), at.getDate(), hh, mm); // daily
}

/** How many repetitions the clock has asked for by `at`.
 *
 *  One sentence: the slots whose moment has already passed inside this period.
 *  An empty slot's moment is the period's start, so it is always among them —
 *  which is how "no times set means the whole target is expected from the
 *  period's start", the behaviour before this change, is reached without a
 *  special case. The trailing term is the same statement for a repetition with
 *  no slot at all, so a short array behaves like a padded one. */
export function expectedBy(freq, slots, count, at = new Date()) {
  const list = (Array.isArray(slots) ? slots : []).slice(0, count);
  const passed = list.filter((s) => momentOf(freq, s, at) <= at).length;
  return passed + Math.max(0, count - list.length);
}

function ordinal(n) {
  const teens = n % 100;
  if (teens >= 11 && teens <= 13) return `${n}th`;
  return n + (['th', 'st', 'nd', 'rd'][n % 10] || 'th');
}

/** One slot in the period's own terms, or '' when it names nothing the period
 *  reads. A field the cadence ignores is never spoken, however it is stored.
 *  Not exported: `slotsText` below is the whole schedule, which is what every
 *  caller and the test actually want. */
function slotText(freq, slot) {
  const reads = SLOT_READS[freq] || SLOT_READS.daily;
  const parts = [];
  if (reads.month && slot.month) parts.push(MONTHS[Number(slot.month) - 1].slice(0, 3));
  if (reads.weekday && slot.weekday) parts.push(WEEKDAYS[Number(slot.weekday) - 1].slice(0, 3));
  if (reads.day && slot.day) {
    // '12 Mar' reads as a date; '12th' on its own reads as a day of the month.
    parts.unshift(reads.month && slot.month ? String(Number(slot.day)) : ordinal(Number(slot.day)));
  }
  if (reads.time && slot.time) parts.push(slot.time);
  return parts.join(' ');
}

/** The whole schedule read back, for the sentence under the dialog's rows —
 *  the one honest check that three dropdowns say what the person meant.
 *
 *  '' when nothing is placed, so the caller can say what that means in the
 *  period's own words. The repetitions nobody has placed are *counted* rather
 *  than left out: a sentence naming one hour would otherwise stand for three
 *  repetitions and describe only one of them. */
export function slotsText(freq, slots, count) {
  const list = slotsVal(slots, count);
  const named = list.map((s) => slotText(freq, s)).filter(Boolean);
  if (!named.length) return '';
  const loose = count - named.length;
  return named.join(', ') + (loose > 0 ? `, +${loose} any time` : '');
}
