// mcp/filter.mjs — the board, asked one question.
//
// One predicate over one card, and the whole filter surface is either a rule
// the board's own UI already applies (`matchesFilters`, js/core/cards.js) or a
// field the board stores and can therefore be asked about. Nothing here is a
// new idea about what a card is.
//
// **It imports only import-free leaves** — js/core/constants.js,
// js/core/timespan.js and js/core/plan.js — which is the js/core/plan.js,
// nesting.js, schedule.js, timespan.js, direction.js, duplicates.js and
// lenses.js rule. `js/core/cards.js` is where `matchesFilters` actually lives
// and it cannot be imported here: it reaches `ui/dom.js` through six hops, so
// one import of it would take this module, `tests/mcp.test.js`'s unit half and
// the MCP server itself down under plain node. What that costs is a second
// writing of the predicate, and what it buys is a filter table pinned by a
// test that needs no browser — the same trade `brain/…/widget2/tools/read.py`
// made from the other side of the language boundary.
//
// **The date rules are borrowed rather than re-derived.** `spanOf` is the
// board's own, so "this week" means the local Monday here exactly as it does
// in the filter panel, and `dayStart` is what turns a deadline's ISO *day*
// into a moment — without it a due-date span compares a string against
// milliseconds and answers that nothing is ever due.
//
// Alternatives considered
// -----------------------
//
// **Ask the server to filter.** There is no such route, and adding one would
// put a second definition of "matches" in `server.js` — the file whose whole
// discipline is that the board travels whole. The board is one person's life:
// a few hundred cards, read in one request the client already makes before
// every write anyway.
//
// **Reuse `matchesFilters` by extracting it into a leaf.** The honest option,
// and the one to take the day a third caller wants it. It is not free: that
// function reads module-level `filters`, `state` and `view`, so extracting it
// means changing the signature at every call site in the frontend for a
// surface none of them asked for. Deferred deliberately, and written down here
// so the next reader knows it was weighed rather than missed.
import { priorityOf } from '../js/core/constants.js';
import { planEnd, planStart } from '../js/core/plan.js';
import { dayStart, spanOf } from '../js/core/timespan.js';

/** How many cards an answer lists when nobody says, and the most it will list
 *  at once. A line runs to roughly 120 characters, so the cap is ~12 000 — a
 *  week's work read off in one go, and small enough that asking for everything
 *  cannot spend a whole context on one tool result. The stated total is what
 *  keeps the cap honest. */
export const DEFAULT_LIMIT = 25;
export const MAX_LIMIT = 100;

/** The filters that are a plain equality against one card field, and the field
 *  each one reads. One table rather than eight `if`s: an eighth branch
 *  spelling `habitFreq` by hand is how a filter comes to name a field the
 *  board has not got. */
const EQUAL_FIELDS = {
  columnId: 'columnId', type: 'type', category: 'category',
  importance: 'importance', urgency: 'urgency', effort: 'effort',
  control: 'control', habitFreq: 'habitFreq',
};

/** The five date filters, and the card field each one is about. `deadline` and
 *  `plan` are days the owner typed; the three timestamps are moments nobody
 *  writes by hand. */
const SPAN_FIELDS = {
  captured: 'createdAt', finished: 'doneAt', updated: 'updatedAt',
  deadline: 'deadline', plan: 'plan',
};

export const SORT_KEYS = [...Object.keys(SPAN_FIELDS), 'priority'];

/** The text the board's search box searches, lower-cased.
 *
 *  `title + ' ' + notes + ' ' + tags.join(' ')`, exactly — `matchesFilters`.
 *  One joined string and not three fields, so a query running from the last
 *  word of a title into the first word of the notes matches here as it matches
 *  there. A search that is *nearly* the board's is worse than one that is
 *  obviously different, because the near-miss is the one nobody can explain. */
const haystack = (card) =>
  `${card.title || ''} ${card.notes || ''} ${(card.tags || []).join(' ')}`.toLowerCase();

/** The board's search rule. A blank query matches everything, which is the
 *  whole `if` being skipped rather than a special case. */
const matchesKeyword = (card, keyword) => {
  const needle = String(keyword ?? '').trim().toLowerCase();
  return !needle || haystack(card).includes(needle);
};

/** A span asked for in the MCP's own words, in the board's. `spanOf` reads the
 *  two dates only under the `custom` preset, so naming either one IS asking
 *  for a custom span — which is what lets a caller say "since March" without
 *  also having to say the word. */
function askedSpan(given, now) {
  if (!given || typeof given !== 'object') return null;
  const { preset = '', start = '', end = '' } = given;
  const custom = Boolean(start || end);
  return spanOf({ preset: custom ? 'custom' : preset, from: start, to: end }, now);
}

/** Does a moment fall inside the span? An absent one — `0` for a timestamp,
 *  `null` from a malformed day — is outside every span rather than at the
 *  epoch, so an undated card never sweeps into "due this week". */
const inSpan = (at, span) =>
  typeof at === 'number' && at > 0 && at >= span.from && at <= span.to;

/** A plan is a *period*, so it matches on overlap: a card planned for '2027'
 *  answers a question about any week of 2027. Every other date is a moment.
 *  This is the one difference between the five spans. */
function planInSpan(plan, span) {
  const from = dayStart(planStart(plan));
  const to = dayStart(planEnd(plan));
  if (from === null || to === null) return false;
  // The plan's last day counts whole, the way a range's end does.
  return from <= span.to && to + 86_399_999 >= span.from;
}

/** What one span filter reads off a card. */
const spanValue = (card, name) => {
  const raw = card[SPAN_FIELDS[name]];
  return typeof raw === 'string' ? dayStart(raw) : raw;
};

function matchesTags(card, query) {
  const carried = new Set((card.tags || []).map((t) => String(t).trim().toLowerCase()));
  const wanted = (list) =>
    (list || []).map((t) => String(t).trim().toLowerCase()).filter(Boolean);
  const all = wanted(query.tagsAll);
  const any = wanted(query.tagsAny);
  if (all.length && !all.every((t) => carried.has(t))) return false;
  return !any.length || any.some((t) => carried.has(t));
}

function matchesScope(card, query) {
  const parent = String(card.parentId || '').trim();
  if (query.childOf != null && parent !== query.childOf) return false;
  if (query.scope === 'top') return !parent;
  if (query.scope === 'children') return Boolean(parent);
  return true;
}

/** Every filter that was given, combined with AND. An absent filter narrows
 *  nothing; `''` is a filter and a real question — "the cards nobody has
 *  judged" — which is why the test is against `undefined` and not falsiness. */
function matchesQuery(card, query, spans) {
  for (const [name, field] of Object.entries(EQUAL_FIELDS)) {
    const wanted = query[name];
    if (wanted !== undefined && (card[field] || '') !== wanted) return false;
  }
  if (!matchesScope(card, query) || !matchesTags(card, query)) return false;
  if (query.hasDeadline !== undefined
      && Boolean(String(card.deadline || '').trim()) !== query.hasDeadline) return false;
  for (const [name, span] of Object.entries(spans)) {
    const passed = name === 'plan'
      ? planInSpan(card.plan, span)
      : inSpan(spanValue(card, name), span);
    if (!passed) return false;
  }
  return matchesKeyword(card, query.keyword);
}

/** What the sort reads off a card, or `null` when the card has not got it.
 *  `null` is what sends a card to the tail, so the absences the board spells
 *  differently — `0` for a timestamp, `''` for a deadline or a plan, `0` for an
 *  unjudged priority — all become one answer here. */
function sortValue(card, sortBy) {
  if (sortBy === 'priority') return priorityOf(card) || null;
  const raw = card[SPAN_FIELDS[sortBy]];
  // A partial plan sorts lexicographically, which is chronological for this
  // shape: '2027' < '2027-03' < '2027-03-04' < '2027-04'.
  if (typeof raw === 'string') return raw.trim() || null;
  return typeof raw === 'number' && raw > 0 ? raw : null;
}

/** The matched cards in the order asked for, with the ones that have no value
 *  for the field at the end — in *both* directions, because a list whose head
 *  is eighty undated cards is a list nobody reads. */
function sortCards(cards, sortBy, order) {
  const sign = order === 'asc' ? 1 : -1;
  return cards
    .map((card, at) => ({ card, at, key: sortValue(card, sortBy) }))
    .sort((a, b) => {
      if (a.key === null || b.key === null) {
        if (a.key === b.key) return a.at - b.at;   // stable: the board's order
        return a.key === null ? 1 : -1;            // absent last, either way
      }
      if (a.key === b.key) return a.at - b.at;
      return a.key < b.key ? -sign : sign;
    })
    .map((row) => row.card);
}

/**
 * Every card that passes, in the order asked for, capped — and the true total.
 *
 * The total is counted before the cap and reported beside it: "all my travel
 * cards" answered with twenty-five of forty is a sentence a reader, or a model,
 * goes on to reason from. A partial board presented as the whole one is this
 * module's version of a fabricated number.
 */
export function selectCards(cards, query = {}, now = new Date()) {
  const spans = {};
  for (const name of Object.keys(SPAN_FIELDS)) {
    const span = askedSpan(query[name], now);
    if (span) spans[name] = span;
  }
  const matched = (cards || []).filter((card) => matchesQuery(card, query, spans));
  const sortBy = SORT_KEYS.includes(query.sortBy) ? query.sortBy : 'captured';
  const order = query.order === 'asc' ? 'asc' : 'desc';
  const limit = Math.min(
    Math.max(Number.isInteger(query.limit) ? query.limit : DEFAULT_LIMIT, 1), MAX_LIMIT);
  return { total: matched.length, cards: sortCards(matched, sortBy, order).slice(0, limit) };
}
