// The span maths behind the time-range filter — what "this week" is, where a
// typed range starts and stops, and the sentence the control reads back.
//
// This module imports nothing, the js/core/plan.js, js/core/draft.js,
// js/core/merge.js, js/core/nesting.js and js/core/schedule.js rule: the
// predicate that uses these spans lives in core/cards.js, which reaches
// ui/dom.js, so anything left in there is an e2e-only surface. Boundaries are
// exactly what wants a node test — see tests/timespan.test.js.
//
// **Every span is local time, and every end is the end of its day.** This
// board is one person's life in one place: a week that begins at a UTC midnight
// begins on Sunday evening for them, and a range ending today that stops at
// midnight drops what was finished an hour ago — which is the commonest thing
// anyone will ask of this filter.

/** Which date on the card the span is measured against. `created` is
 *  `createdAt`, `done` is `doneAt`; the labels are the control's own words and
 *  the first half of every read-back sentence. */
export const RANGE_FIELDS = [
  { id: 'created', label: 'Captured' },
  { id: 'done', label: 'Finished' },
  // The deadline, read as the local day it names (`dayStart` below). The third
  // date a card carries, and the only one the filter could not measure until
  // the lens row needed it: "what is due" is the question every other one of
  // these is a way of not asking.
  { id: 'due', label: 'Due' },
];

/** The spans on offer. `''` is the off position — a real value, not an absent
 *  one, exactly as the column filter's `''` means "open cards". `custom` is the
 *  only entry that reads the two dates. */
export const RANGE_PRESETS = [
  { id: '', label: 'Any time' },
  // Open at the start on purpose: a card due last Tuesday is today's work, and
  // a span beginning this morning would drop every late card out of the one
  // question that exists to show them.
  { id: 'through-today', label: 'Up to today' },
  { id: 'week', label: 'This week' },
  { id: 'month', label: 'This month' },
  { id: 'days30', label: 'Last 30 days' },
  { id: 'year', label: 'This year' },
  { id: 'custom', label: 'Between…' },
];

const FIELD_IDS = RANGE_FIELDS.map((f) => f.id);
const PRESET_IDS = RANGE_PRESETS.map((p) => p.id);

const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** An ISO calendar date ('YYYY-MM-DD') or ''. The same round-trip
 *  `deadlineVal` uses, and for the same reason: it rejects the shape-valid
 *  impossibilities a half-typed date input produces (2026-02-30, 2026-13-01). */
const dayVal = (v) => {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return '';
  const d = new Date(v + 'T00:00:00Z');
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v ? v : '';
};

/** A range, validated field by field. Everything a select or a stored value
 *  can be wrong about blanks itself, so an unknown preset that survived a
 *  browser upgrade narrows nothing instead of narrowing the board by a rule
 *  nothing on screen can name. The one reader that matters is the localStorage
 *  load: the range is remembered between visits, so a value written by an
 *  older build has to be answerable today. */
export function rangeVal(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  return {
    field: FIELD_IDS.includes(r.field) ? r.field : 'created',
    preset: PRESET_IDS.includes(r.preset) ? r.preset : '',
    from: dayVal(r.from),
    to: dayVal(r.to),
  };
}

// Local day boundaries. `new Date(y, m, d)` is local by construction, which is
// the whole point — parsing 'YYYY-MM-DD' with the Date constructor would give
// UTC midnight and shift every boundary by the reader's offset.
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
const endOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59, 999).getTime();
const dayFrom = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };

/** The span a range asks for, in milliseconds, or `null` when it asks for
 *  nothing. `null` rather than an infinite span so the predicate has one early
 *  return and the filter is genuinely inert while it is off — the default state
 *  of every view in the app.
 *
 *  An absent end of a typed range is ±Infinity, which is what makes "since the
 *  first of March" and "up to the first of March" both sayable. A start after
 *  its end is returned as the empty span it is: the reader typed a
 *  contradiction, and swapping the two would answer a question they did not
 *  ask. */
export function spanOf(range, now = new Date()) {
  const { preset, from, to } = rangeVal(range);
  switch (preset) {
    case 'through-today':
      return { from: -Infinity, to: endOfDay(now) };
    case 'week': {
      // The local Monday, matching the ISO week habitPeriod already counts in.
      // getDay() is 0 for Sunday, so Sunday is six days into the week, not
      // minus one.
      const back = (now.getDay() + 6) % 7;
      const mon = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back);
      return { from: startOfDay(mon), to: endOfDay(new Date(mon.getFullYear(), mon.getMonth(), mon.getDate() + 6)) };
    }
    case 'month':
      return {
        from: new Date(now.getFullYear(), now.getMonth(), 1).getTime(),
        // Day 0 of the next month is the last day of this one, which is how
        // this avoids knowing anything about month lengths or leap years.
        to: endOfDay(new Date(now.getFullYear(), now.getMonth() + 1, 0)),
      };
    case 'year':
      return {
        from: new Date(now.getFullYear(), 0, 1).getTime(),
        to: endOfDay(new Date(now.getFullYear(), 11, 31)),
      };
    case 'days30':
      // Thirty whole days with today the thirtieth, so the span opens
      // twenty-nine days back and closes at the end of today rather than at
      // the moment it was asked — a card finished this evening belongs to a
      // span the reader thinks of as including today.
      return {
        from: startOfDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29)),
        to: endOfDay(now),
      };
    case 'custom': {
      if (!from && !to) return null; // the control is open and nothing has been said with it
      return {
        from: from ? startOfDay(dayFrom(from)) : -Infinity,
        to: to ? endOfDay(dayFrom(to)) : Infinity,
      };
    }
    default:
      return null;
  }
}

const dayWords = (iso) => {
  const [y, m, d] = iso.split('-').map(Number);
  return `${d} ${MONTHS_SHORT[m - 1]} ${y}`;
};

/** A deadline as a moment: the start of the local day it names, or `null`.
 *
 *  A deadline is an ISO *day* where `createdAt` and `doneAt` are timestamps, so
 *  the predicate needs one place that turns the first into the second — local,
 *  like every other boundary here, or "due today" answers with tomorrow's card
 *  for a reader east of Greenwich. `null` and not 0 for an absent or malformed
 *  date: an undated card is outside every due span, where a zero would sweep it
 *  into all of them from the epoch. */
export const dayStart = (iso) => {
  const day = dayVal(typeof iso === 'string' ? iso : '');
  return day ? startOfDay(dayFrom(day)) : null;
};

/** The span in words, at the precision it was given.
 *
 *  The `planWords` lesson: a field, a preset and two date inputs never *look*
 *  wrong even when they say something other than what the filter is applying,
 *  so the control states its own answer in a sentence the reader can check. An
 *  inactive range reads as "Any time" whatever the two dates hold, because the
 *  sentence must never claim a span that is not in force. */
export function rangeText(range) {
  const r = rangeVal(range);
  const span = spanOf(r, new Date());
  if (!span) return 'Any time';
  const who = RANGE_FIELDS.find((f) => f.id === r.field).label;
  if (r.preset === 'custom') {
    if (r.from && r.to) return `${who} ${dayWords(r.from)} – ${dayWords(r.to)}`;
    return r.from ? `${who} since ${dayWords(r.from)}` : `${who} up to ${dayWords(r.to)}`;
  }
  const when = { week: 'this week', month: 'this month', year: 'this year',
    days30: 'in the last 30 days', 'through-today': 'up to today' }[r.preset];
  return `${who} ${when}`;
}
