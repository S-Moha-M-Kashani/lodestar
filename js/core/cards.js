import { categories, sanitizeCategories } from './categories.js';
import { COLUMNS, TYPES, priorityOf } from './constants.js';
import { habitCountVal, habitFreqVal, habitHistoryVal, habitTimesVal, isHabit } from './habits.js';
import { parentIdVal, resolveLineage } from './nesting.js';
import { planSrcVal, planVal, resolvePlan } from './plan.js';
import { dayStart, spanOf } from './timespan.js';
import { commit } from './history.js';
import { filters, state, view } from './state.js';
import { columnTitle, getCard } from '../ui/dom.js';

// A card, from raw JSON to the shape the board trusts: the field validators,
// the permanent ledger number, and the two operations that belong to a card
// rather than to any one view (does it pass the filters, and moving it).

// 'plan' was the sixth type until 2026-08-28. A card that still says so — a
// stored board, an old export, an assistant working from memory — is a task
// now, and its plan is the date it always had. Coercing it to 'question' (the
// fallback for real nonsense) would have re-filed years of work as unanswered.
const LEGACY_TYPES = { plan: 'task' };
export const typeVal = (t) => (TYPES.includes(t) ? t : LEGACY_TYPES[t] || 'question');
// Validate against the live registry by default; imports pass the file's own
// registry so custom categories survive the round trip.
export const catVal = (c, reg = categories) => (reg.some((x) => x.id === c) ? c : '');

// Importance & urgency are each High, Low, or unset ('') — a card needs
// both to be placed on the Eisenhower matrix.
export const iuVal = (v) => (v === 'high' || v === 'low' ? v : '');

// A deadline is an ISO calendar date ('YYYY-MM-DD') or unset (''). The
// toISOString round-trip rejects shape-valid impossibilities (2026-13-45).
export const deadlineVal = (v) => {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return '';
  const d = new Date(v + 'T00:00:00Z');
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v ? v : '';
};

// Effort ("how much work is this?") and control ("can I even act on it?")
// always hold a value: the scale's midpoint stands in until a person — or,
// one day, the brain — judges it. The *Src fields record who set the value
// (default | user | ai) so an estimator never overwrites a human's call.
export const effortVal = (v) => (v === 'low' || v === 'high' ? v : 'medium');
export const controlVal = (v) => (v === 'act' || v === 'none' ? v : 'influence');
const srcVal = (v) => (v === 'user' || v === 'ai' ? v : 'default');

export const uid = () =>
  (crypto.randomUUID ? crypto.randomUUID() : 'id-' + Math.random().toString(36).slice(2) + Date.now());

export function seedCards() {
  const now = Date.now();
  const mk = (title, columnId, type, category, tags, importance = '', urgency = '', notes = '') =>
    ({ id: uid(), columnId, title, notes, type, category, importance, urgency,
       effort: 'medium', control: 'influence', effortSrc: 'default', controlSrc: 'default',
       deadline: '', plan: '', planSrc: 'auto',
       habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
       parentId: '',
       // A seed card in Done is finished, so it says when: without this the
       // one board that explains the app is also the one whose finished card
       // answers no question about when it was finished.
       doneAt: columnId === 'answered' ? now : 0,
       tags, createdAt: now, updatedAt: now });
  // Seeds span categories, types and all four matrix quadrants, so every view
  // has something to show on a fresh board.
  return [
    mk('What should I build next quarter?', 'inbox', 'question', 'work', ['planning'], 'high', 'low'),
    mk('How do we keep weekday evenings free together?', 'inbox', 'problem', 'love', ['us'], 'high', 'high'),
    mk('Which Stoic should I read after Meditations?', 'inbox', 'question', 'mind', ['reading'], 'low', 'low'),
    mk('Plan a long weekend in the mountains', 'in-progress', 'task', 'travel', ['autumn'], 'low', 'high'),
    mk('Learn the intro to “Blackbird”', 'in-progress', 'task', 'music', ['guitar']),
    mk('Book the dentist check-up', 'answered', 'task', 'health', [], '', '', 'Done — appointment on the 12th.'),
  ].map((c, i) => ({ ...c, num: i + 1 }));
}

// Every card keeps a permanent ledger number (C-001, C-002, …) in capture order.
export function ensureNums(cards) {
  let max = cards.reduce((m, c) => Math.max(m, c.num || 0), 0);
  [...cards]
    .filter((c) => !c.num)
    .sort((a, b) => a.createdAt - b.createdAt)
    .forEach((c) => { c.num = ++max; });
  return cards;
}

export const cardLabel = (card) => 'C-' + String(card.num).padStart(3, '0');

// Which columns a card can be in. In Progress takes no habit: a habit's
// progress IS the punch strip in the rail, so a habit filed there is one fact
// told twice — and the column that shows a card is the only place it can be
// dragged out of, which is why this is a refusal rather than a render filter.
// Consulted by the three ways into a column: loading a board (below), the
// pointer (ui/dnd.js) and the keyboard (ui/keyboard.js).
export const columnAccepts = (card, columnId) =>
  !(columnId === 'in-progress' && isHabit(card));

export function sanitizeCard(raw, reg = categories) {
  if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) return null;
  const habitCount = habitCountVal(raw.habitCount);
  const deadline = deadlineVal(raw.deadline);
  const planSrc = planSrcVal(raw.planSrc);
  const type = typeVal(raw.type);
  const habitFreq = habitFreqVal(raw.habitFreq);
  const known = COLUMNS.some((c) => c.id === raw.columnId) ? raw.columnId : 'inbox';
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : uid(),
    // A habit an older board (or an import) left in In Progress comes back to
    // the Inbox, where it can be seen and moved. Hiding it in a column it is
    // not painted in would strand it: the rail can open the card but not
    // retire it.
    columnId: columnAccepts({ type, habitFreq }, known) ? known : 'inbox',
    title: raw.title.trim(),
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    type,
    category: catVal(raw.category, reg),
    importance: iuVal(raw.importance),
    urgency: iuVal(raw.urgency),
    effort: effortVal(raw.effort),
    control: controlVal(raw.control),
    effortSrc: srcVal(raw.effortSrc),
    controlSrc: srcVal(raw.controlSrc),
    deadline,
    // While nobody has set a plan by hand it *is* the deadline, so a dated card
    // needs no second act to appear in the plan. See js/core/plan.js.
    plan: resolvePlan({ plan: planVal(raw.plan), planSrc, deadline }),
    planSrc,
    habitFreq,
    habitCount,
    habitTimes: habitTimesVal(raw.habitTimes, habitCount),
    habitHistory: habitHistoryVal(raw.habitHistory),
    // Shape only, like `category` before the registry is consulted. Whether
    // that id names a card this board holds — and whether the link is one level
    // deep — are questions about the *list*, which is resolveLineage's job and
    // runs once over the sanitized board.
    parentId: parentIdVal(raw.parentId),
    num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
    tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
    createdAt: typeof raw.createdAt === 'number' ? raw.createdAt : Date.now(),
    updatedAt: typeof raw.updatedAt === 'number' ? raw.updatedAt : Date.now(),
    // Shape only. Whether it *should* hold a date is a question about the
    // column, and the answer is the server's: cleanCard clears it on a card
    // that is not in Done and backfills one that is, so the two ends cannot
    // disagree about a card the browser has never opened. What stamps the real
    // moment is placeCard, below.
    doneAt: doneAtVal(raw.doneAt),
  };
}

// When the card was finished, or 0. Mirrors the server's own validator: a
// positive finite number and nothing else, so an absent field, a string and a
// negative all read as "never finished" — which is a different answer from
// "finished at a time nobody recorded", and has to stay one.
const doneAtVal = (v) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0);

export function parseState(json) {
  const data = JSON.parse(json);
  if (!data || data.version !== 1 || !Array.isArray(data.cards)) throw new Error('Unrecognized data format');
  // Files/saves that predate custom categories have no registry — cats stays
  // null and the caller keeps whatever registry it already has.
  const cats = sanitizeCategories(data.categories);
  return {
    version: 1,
    columns: COLUMNS,
    categories: cats,
    // resolveLineage runs once, over the whole sanitized list: an import of
    // half a board, or a file naming parents it does not carry, lands with the
    // cards and without the links it cannot honour.
    cards: resolveLineage(
      ensureNums(data.cards.map((c) => sanitizeCard(c, cats || categories)).filter(Boolean))),
  };
}

/** Which column a card is in, as a filter.
 *
 *  Two rules in one, and both of them are about where the reader is. On the
 *  Board this is inert: the Board *is* the three columns, and a filter that
 *  emptied two of them would read as cards having gone missing. Everywhere
 *  else a finished card is a record rather than work — it belongs in the Done
 *  column and in nothing that is asking what to do next — so Done is out
 *  unless the reader asks for it by name, or for 'all'.
 *
 *  `columnsOff` is the Menu switching the whole idea off: the default goes
 *  with the control, because a rule still narrowing the board from behind a
 *  hidden dropdown is a board nobody can explain. */
// Is the rule in force at all? Off on the Board, and off when the Menu has
// switched the filter away. Shared with filtersActive below, which would
// otherwise claim a filter is narrowing the Board — and answer "No cards match"
// over a column the reader emptied themselves.
export const columnFilterOn = () => view !== 'board' && !filters.columnsOff;

function matchesColumn(card) {
  if (!columnFilterOn() || filters.column === 'all') return true;
  if (filters.column) return card.columnId === filters.column;
  // The hide-Done *default* stands down while the range is asking about
  // finished cards: a reader who asks for what they finished last month and is
  // answered with an empty view has been shown a control hiding the very thing
  // they named, which is the rule the scope filter already bends for a running
  // search. A column the reader picked by name is left alone above — they chose
  // that, and "Inbox, finished last month" is a question with no answer rather
  // than a filter getting in the way.
  if (askingAboutFinished()) return true;
  return card.columnId !== 'answered';
}

/** Is the reader asking about *finished* cards, right now?
 *
 *  Read by `matchesRange` and by `matchesColumn` above, so the suspension and
 *  the filter cannot disagree about whether a finished span is in force — and
 *  **exported**, because the Backlog needs it too and re-deriving it there is
 *  the mistake `columnFilterOn` was extracted to stop. That view picks its own
 *  default column rather than leaning on `matchesColumn`, so the suspension has
 *  to be applied a second time; what must not happen twice is the *rule*. */
export const askingAboutFinished = () =>
  filters.range.field === 'done' && spanOf(filters.range) !== null;

/** Cards captured — or finished — inside the span the reader asked for.
 *
 *  Consulted first inside `matchesFilters`, so the Board, the Backlog, the map
 *  and the Matrix needed no change; **exported** because areas.js and review.js
 *  call it alone through `scopedCards()`, exactly as they call `matchesNesting`.
 *  Switching those two to `matchesFilters` to pick up one new filter would hand
 *  them four others nobody asked for.
 *
 *  A card with no finished date is out of every finished span. That is not the
 *  same as being out of a captured one: "what did I finish in March" is a
 *  question about a moment the card either has or has not got, and an
 *  unfinished card has not got it. `spanOf` answering `null` is the filter
 *  being off, which is its default state on every view. */
export function matchesRange(card) {
  const span = spanOf(filters.range);
  if (!span) return true;
  // Three dates a card can be measured against, and the deadline is the one
  // that is a *day* rather than a moment — `dayStart` is where that difference
  // is answered, once, so nothing downstream has to know about it.
  const { field } = filters.range;
  const when = field === 'done' ? card.doneAt
    : field === 'due' ? dayStart(card.deadline)
      : card.createdAt;
  if (!when) return false;
  return when >= span.from && when <= span.to;
}

/** Parents only, or parents and children together?
 *
 *  `filters.nesting` is '' for *parents only* — the default, because a child is
 *  already shown inside its parent, so listing it again counts one piece of
 *  work twice on the map, the Matrix and the register. 'all' shows everything.
 *
 *  Unlike the column filter this is **live on the Board too**, and it does not
 *  have that filter's problem: hiding a child on the Board hides it from one
 *  place while still showing it in another — its parent's fold — where the
 *  column filter would have emptied two of the three columns in front of the
 *  reader with nowhere else to look.
 *
 *  **An active text search outranks it.** A search whose only match is a child
 *  would otherwise answer "no cards match" over a card the board is holding,
 *  and a control that narrows the board must never be able to hide the thing
 *  you just asked for by name. Exported because areas.js and review.js consult
 *  this predicate alone: they have never applied the category, type, priority
 *  or tag filters, and making them do so is a different change nobody asked
 *  for. */
export const matchesNesting = (card) =>
  Boolean(filters.nesting === 'all' || searchNeedle() || !card.parentId);

export function matchesFilters(card) {
  if (!matchesNesting(card)) return false;
  if (!matchesRange(card)) return false;
  if (!matchesColumn(card)) return false;
  if (filters.type && card.type !== filters.type) return false;
  if (filters.category && card.category !== filters.category) return false;
  if (filters.prio) {
    const p = priorityOf(card); // 0 = unlabelled (either judgement missing)
    if (filters.prio === 'none' ? p !== 0 : String(p) !== filters.prio) return false;
  }
  if (filters.tags.size && ![...filters.tags].every((t) => card.tags.includes(t))) return false;
  // `filters.search` is the text as typed — the search box is rebuilt from it
  // on every repaint, so trimming or lower-casing it there would eat a space
  // mid-word and undo a capital letter under the caret. Normalising belongs
  // here, at the one place that compares it to anything.
  const needle = searchNeedle();
  if (needle) {
    const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

const searchNeedle = () => filters.search.trim().toLowerCase();

// The column filter counts only where it is narrowing to one named column: the
// default hides Done on its own, and "No cards match" over an empty Backlog
// would blame a filter the reader never set.
export const filtersActive = () => Boolean(searchNeedle() || filters.type || filters.category || filters.prio || filters.tags.size
  // The range counts, and it counts everywhere — it is never a default, so an
  // empty column, an empty list and an empty map all have to be able to say a
  // span is responsible. It is remembered between visits, which is exactly why:
  // a reader opening the app on Monday to three cards is told why.
  || spanOf(filters.range) !== null
  || (columnFilterOn() && filters.column && filters.column !== 'all')
  // The scope filter counts, and its *default* is the narrowing one — so the
  // empty-column hint must not say "No cards match" over children the reader
  // cannot see and did not switch off. It is inert while a search is running,
  // where the search itself is already in this list.
  || (filters.nesting !== 'all' && !searchNeedle() && state.cards.some((c) => c.parentId)));

/** File a card in a column, and record nothing.
 *
 *  A card's place in `state.cards` is what decides where it appears in its
 *  column, so setting `columnId` alone leaves it at whatever index it held in
 *  the column it left. Split out of moveCard for the card dialog, which files a
 *  card as part of a save that is already one act: calling moveCard there would
 *  write a second undo entry and repaint mid-save, and copying these eleven
 *  lines would be two answers to where a card goes. Returns whether it moved. */
export function placeCard(cardId, columnId, beforeId = null) {
  const card = getCard(cardId);
  if (!card || cardId === beforeId) return false;
  if (!columnAccepts(card, columnId)) return false;
  state.cards = state.cards.filter((c) => c.id !== cardId);
  const now = Date.now();
  // The one site that assigns a column is the one site that decides whether the
  // card is finished, so `doneAt` is observed here and guessed nowhere. Entering
  // Done stamps this moment; leaving it clears the field, because a reopened
  // card that kept its date would go on answering "what did I finish last
  // month" for ever. The dialog's save already calls this, so it needs no rule
  // of its own — and neither does a drag or the keyboard.
  card.doneAt = columnId === 'answered' ? now : 0;
  card.columnId = columnId;
  card.updatedAt = now;

  let index = -1;
  if (beforeId) index = state.cards.findIndex((c) => c.id === beforeId);
  if (index === -1) {
    let lastInColumn = -1;
    state.cards.forEach((c, i) => { if (c.columnId === columnId) lastInColumn = i; });
    index = lastInColumn + 1 || state.cards.length;
    if (lastInColumn === -1) index = state.cards.length;
  }
  state.cards.splice(index, 0, card);
  return true;
}

export function moveCard(cardId, columnId, beforeId = null) {
  const card = getCard(cardId);
  if (!placeCard(cardId, columnId, beforeId)) return;
  commit(`Moved ${cardLabel(card)} to ${columnTitle(columnId)}`);
}
