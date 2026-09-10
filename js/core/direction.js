// The board's own direction: what to do next, and why, read off the cards.
//
// The widget was a chat window that opened empty and waited to be asked, and
// every answer cost a model turn. Most of what "give me direction" means is
// arithmetic — dates, counts and one grouping — so it is done here, in the
// browser, over the cards already in memory, before anything is asked of
// anybody. The model's job is the sentence, not the selection.
//
// **Five rules, consulted in this order, and the first one to claim a card is
// the one that names it.** That order *is* the ordering requirement: a
// committed, time-bound card outranks a merely available one. Each figure is
// what the rule found on the owner's real board on 2026-09-10 (58 live cards —
// see the change's measurements.md):
//
//   1. overdue                      — deadline in the past          · 5 cards
//   2. a habit behind its cadence   — fewer done than expected      · 7 of 7
//   3. due soon                     — deadline inside a week        · 0 cards
//   4. planned for now, not started — hand-set plan names now       · 13 cards
//   5. a neglected life area        — nothing touched for a month   · 3 areas
//
// Two of the six signals first imagined were judged against those figures.
// *"In progress and unchanged for some time"* was **dropped**: it fires on zero
// cards, because all 13 In Progress cards were touched within 1.7 days, and its
// staleness threshold would have been a number nobody could defend. *"Due
// soon"* also finds nothing today and was **kept**, because it is literally
// `overdue` with the comparison reversed — the two rules below differ in one
// operator and their wording and share `dayGap`, so it costs no new field, no
// new threshold and no new concept, and it speaks the day a deadline is set.
//
// **No score, no weights, no ranking number.** Where two cards share a reason,
// the tie breaks on the two judgements the board is actually rich in —
// `importance` and `urgency`, set on 83–85% of cards — read exactly as the
// Matrix reads them. They are a tie-break inside a reason and never a reason of
// their own: a number the owner cannot explain to himself is not direction.
//
// **This module imports nothing** — the js/core/merge.js, asana.js, plan.js,
// nesting.js, schedule.js, timespan.js and importplan.js rule. Every other
// module under core/ reaches ui/dom.js sooner or later, and the moment this one
// does, tests/direction.test.js stops loading under plain node and the only
// cover left for the honesty rules is an e2e. That is also why the habit
// arithmetic is **passed in** rather than imported: it lives in
// core/schedule.js (`expectedBy`) and core/habits.js (`habitPeriod`, for the
// ISO-week period id), and habits.js reaches core/state.js. `planImport` takes
// `newId` for the same reason.

/** How many items the direction names.
 *
 *  Measured, not guessed: the widget's `.chat-log` is 343 px tall, and the
 *  eight suggestion chips this replaces occupied 361 px inside it — the opener
 *  list was taller than the box holding it and had to be scrolled. Five
 *  one-line items plus the encouragement line fit without scrolling. It is
 *  also about as much as anyone can hold in their head walking away from a
 *  screen, which is the difference between a direction and a list. */
export const DIRECTION_MAX = 5;

/** How many items any one rule may contribute. Not exported: the caps are the
 *  direction's own shape, and the only thing outside this file that needs a
 *  figure is how many items to make room for.
 *
 *  The board holds 5 overdue cards, 7 habits behind their cadence and 13 cards
 *  planned for this month. Uncapped, the whole direction would be overdue
 *  cards every morning — the guilt machine the design names as its first risk —
 *  and the sharpest true statement the board can currently make (every habit
 *  on every board is behind, three never kept once) would never be said. Two
 *  leaves room for four of the five rules to be heard at once. */
const PER_REASON_MAX = 2;

/** Days of silence that make a life area neglected.
 *
 *  The board's own gap decides it: every area but three was touched within 2.3
 *  days, and those three at 41.4, 41.4 and 45.5 days. Any threshold between 3
 *  and 41 picks exactly the same three areas, so the number is chosen for being
 *  the one a person would use — a month — and it matches the board's own
 *  staleness bucket (30–90 days, 7 cards). */
const NEGLECT_DAYS = 30;

/** How far ahead "due soon" looks, and how far back the record is read for
 *  encouragement. A week, in both cases, because that is the span the owner
 *  plans in — the plan's own horizons are today, this week, this month. */
const SOON_DAYS = 7;
const PRAISE_DAYS = 7;

const DONE = 'answered';
const INBOX = 'inbox';
const DAY_MS = 86400000;

// Written out rather than imported: `cardLabel` lives in core/cards.js, which
// reaches core/state.js. The same trade core/plan.js makes with the month
// names — and a ledger number's shape is not a rule two copies can disagree
// about, unlike the cadence arithmetic above, which is passed in.
const labelOf = (card) => 'C-' + String(card.num || 0).padStart(3, '0');

// core/habits.js's HABIT_NOW, four words, for the same reason.
const NOW_WORD = { daily: 'today', weekly: 'this week', monthly: 'this month', yearly: 'this year' };

const pad2 = (n) => String(n).padStart(2, '0');
/** A local calendar day, as a deadline and a day-precision plan are stored. */
const dayOf = (at) => `${at.getFullYear()}-${pad2(at.getMonth() + 1)}-${pad2(at.getDate())}`;
const midnight = (at) => new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime();

/** Whole days from today to a stored 'YYYY-MM-DD' — negative in the past.
 *  Local time and midnight to midnight, the core/timespan.js rule: a deadline
 *  is a day in the owner's own place, not an instant. */
function dayGap(iso, at) {
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!y || !m || !d) return NaN;
  return Math.round((new Date(y, m - 1, d).getTime() - midnight(at)) / DAY_MS);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

const isHabit = (card) => card.type === 'habit' && Boolean(card.habitFreq);
/** Habits repeat on a calendar of their own and carry their own strip, so the
 *  three date rules pass over them — core/plan.js's `plannable` rule. */
const dated = (card) => !isHabit(card) && card.columnId !== DONE;

/** The Matrix's own reading of the two judgements, as a sort key: urgent and
 *  important, urgent, important, neither — and unjudged last, which is the one
 *  difference from `priorityOf` (that returns 0, which would sort first). */
function keen(card) {
  if (!card.importance || !card.urgency) return 5;
  if (card.urgency === 'high') return card.importance === 'high' ? 1 : 2;
  return card.importance === 'high' ? 3 : 4;
}

const item = (rule, card, reason) => ({ rule, cardId: card.id, label: labelOf(card), reason });

/** Sort by the rule's own natural key, then by the two judgements. */
const by = (key) => (a, b) => key(a) - key(b) || keen(a.card) - keen(b.card);

/** The dated cards a deadline rule keeps, nearest date first. `keep` is the
 *  one thing overdue and due-soon differ by besides their wording — which is
 *  the whole argument for keeping the second rule after it was measured at
 *  zero cards: it is this call with the comparison reversed, not a concept. */
function byDeadline(cards, at, keep) {
  return cards.filter((c) => dated(c) && c.deadline)
    .map((card) => ({ card, gap: dayGap(card.deadline, at) }))
    .filter(({ gap }) => keep(gap))
    .sort(by((x) => x.gap));
}

const overdueItems = (cards, at) => byDeadline(cards, at, (g) => g < 0)
  .map(({ card, gap }) => item('overdue', card, `overdue by ${plural(-gap, 'day')}`));

const dueSoonItems = (cards, at) => byDeadline(cards, at, (g) => g >= 0 && g <= SOON_DAYS)
  .map(({ card, gap }) => item('due-soon', card,
    gap === 0 ? 'due today' : gap === 1 ? 'due tomorrow' : `due in ${plural(gap, 'day')}`));

/** Habits behind what the clock has already asked for.
 *
 *  One condition, not two: `expectedBy` never exceeds the target, so a habit
 *  short of the expectation is short of the target as well — which is what
 *  core/habits.js's `habitReminding` says in two terms. A habit parked in Done
 *  is retired and stops asking. */
function habitItems(cards, at, habitBehind) {
  if (!habitBehind) return [];
  return cards.filter((c) => isHabit(c) && c.columnId !== DONE)
    .map((card) => ({ card, ...habitBehind(card, at) }))
    .filter((x) => x.done < x.expected)
    .sort(by((x) => x.done - x.expected))
    .map(({ card, done }) => item('habit-behind', card,
      `behind its cadence — ${done} of ${card.habitCount} done ${NOW_WORD[card.habitFreq]}`));
}

/** A plan naming the period we are in, on a card nobody has picked up.
 *
 *  A plan is a partial date, so "names the current period" is one comparison:
 *  '2026', '2026-09' and '2026-09-10' are all prefixes of today. Only a
 *  **hand-set** plan counts — an automatic one mirrors the deadline, and the
 *  two date rules above have already named that card. Still in the Inbox is
 *  what "not started" means; a card in In Progress has been started. */
function plannedItems(cards, at) {
  const today = dayOf(at);
  const said = { 4: 'this year', 7: 'this month', 10: 'today' };
  return cards.filter((c) => dated(c) && c.columnId === INBOX
      && c.plan && (c.planSrc === 'user' || c.planSrc === 'ai') && said[c.plan.length]
      && today.startsWith(c.plan))
    // The more precise plan first — a card someone put a day on is a nearer
    // promise than one that names only the year.
    .sort((a, b) => b.plan.length - a.plan.length || keen(a) - keen(b))
    .map((card) => item('planned-now', card,
      `planned for ${said[card.plan.length]}, not started`));
}

/** A life area in which nothing has moved.
 *
 *  The area's age is the last time *anything* open in it was touched; the card
 *  named is the one that has waited longest, because that is the one to pick
 *  up. An area holding no open card is not neglected — it is finished or
 *  unused, and two of this board's nine registered areas hold no card at all.
 *
 *  The reason names the area by its id, which is the lowercase of its own
 *  label for every area on this board; `area` rides on the item so a renderer
 *  can substitute the registry's label without this module having to be handed
 *  the registry. */
function areaItems(cards, at) {
  const open = cards.filter((c) => c.columnId !== DONE && c.category);
  const areas = new Map();
  for (const card of open) {
    const seen = areas.get(card.category) || { touched: 0, stalest: card };
    if ((card.updatedAt || 0) > seen.touched) seen.touched = card.updatedAt || 0;
    if ((card.updatedAt || 0) < (seen.stalest.updatedAt || 0)) seen.stalest = card;
    areas.set(card.category, seen);
  }
  return [...areas.entries()]
    .map(([area, { touched, stalest }]) =>
      ({ area, card: stalest, days: Math.floor((at.getTime() - touched) / DAY_MS) }))
    .filter((x) => x.days >= NEGLECT_DAYS)
    .sort(by((x) => -x.days))
    .map(({ area, card, days }) => ({
      ...item('neglected-area', card, `nothing has moved in ${area} for ${plural(days, 'day')}`),
      area,
    }));
}

/** What the record can prove about the last week — and, when it can prove
 *  nothing, what it actually shows.
 *
 *  This will say almost nothing on the owner's board today and that is honest
 *  rather than broken: `doneAt` landed on 2026-09-07, so the whole provable
 *  record of finished work is five cards inside a three-day window, and the
 *  seven finished cards in the Trash carry no date at all. `earned` is a field
 *  and not a tone, so nothing downstream can read a bad week as a good one. */
function praiseOf(cards, at, habitBehind) {
  const since = at.getTime() - PRAISE_DAYS * DAY_MS;
  const finished = cards.filter((c) => c.columnId === DONE && c.doneAt && c.doneAt >= since);
  const habits = cards.filter((c) => isHabit(c) && c.columnId !== DONE)
    .map((card) => ({ card, ...(habitBehind ? habitBehind(card, at) : {}) }));
  const kept = habits.filter((h) => h.done >= h.card.habitCount).map((h) => h.card);
  const labels = [...finished, ...kept].map(labelOf);

  if (!labels.length) {
    const behind = habits.filter((h) => h.done < h.expected).length;
    const nothing = `Nothing has been finished in the last ${PRAISE_DAYS} days`;
    return {
      earned: false,
      labels: [],
      text: behind
        ? `${nothing}, and ${plural(behind, 'habit')} ${behind === 1 ? 'is' : 'are'} behind.`
        : `${nothing}.`,
    };
  }
  const parts = [];
  if (finished.length) parts.push(`${plural(finished.length, 'card')} finished`);
  if (kept.length) parts.push(`${plural(kept.length, 'habit')} kept`);
  return {
    earned: true,
    labels,
    text: `${parts.join(' and ')} in the last ${PRAISE_DAYS} days: ${labels.join(', ')}.`,
  };
}

/**
 * Read a direction off a board.
 *
 * @param {Array} cards   the open board's cards, as `state.cards` holds them
 * @param {object} [opts]
 * @param {Date}   opts.at            the moment to judge against
 * @param {Function} opts.habitBehind `(card, at) => { done, expected }` — the
 *   cadence arithmetic, passed in rather than imported so this module stays
 *   importless and its test stays loadable under plain node. Omitted, the habit
 *   rule contributes nothing and no habit is praised; it never guesses.
 * @returns {{items: Array, note: string, praise: {earned: boolean, text: string, labels: string[]}}}
 *   `items` is at most `DIRECTION_MAX`, each naming its card's ledger number
 *   and its one-phrase reason; `note` is '' unless there is nothing to act on,
 *   and then says so; `praise` is what the record proves, never more.
 */
export function direction(cards, { at = new Date(), habitBehind = null } = {}) {
  // A proposal and a card in the Trash are never named. The browser's board
  // holds neither — `readBoard` excludes both — so this is a floor rather than
  // a filter, and tests/direction.test.js is what keeps it a floor.
  const live = (Array.isArray(cards) ? cards : [])
    .filter((c) => c && typeof c === 'object' && !c.pending && !c.deletedAt);

  const rules = [
    overdueItems(live, at),
    habitItems(live, at, habitBehind),
    dueSoonItems(live, at),
    plannedItems(live, at),
    areaItems(live, at),
  ];

  // First rule to claim a card names it: one card, one reason. A card that is
  // both overdue and planned for this month is one thing to do, not two.
  const items = [];
  const named = new Set();
  for (const found of rules) {
    let taken = 0;
    for (const it of found) {
      if (items.length >= DIRECTION_MAX || taken >= PER_REASON_MAX) break;
      if (named.has(it.cardId)) continue;
      named.add(it.cardId);
      items.push(it);
      taken += 1;
    }
  }

  const note = items.length ? ''
    : live.length ? 'Nothing is overdue, behind or planned for now.'
      : 'This board holds no cards to act on.';

  return { items, note, praise: praiseOf(live, at, habitBehind) };
}
