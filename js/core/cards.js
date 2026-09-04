import { categories, sanitizeCategories } from './categories.js';
import { COLUMNS, TYPES, priorityOf } from './constants.js';
import { habitCountVal, habitFreqVal, habitHistoryVal, habitTimesVal, isHabit } from './habits.js';
import { planSrcVal, planVal, resolvePlan } from './plan.js';
import { commit } from './history.js';
import { filters, state } from './state.js';
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
    num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
    tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
    createdAt: typeof raw.createdAt === 'number' ? raw.createdAt : Date.now(),
    updatedAt: typeof raw.updatedAt === 'number' ? raw.updatedAt : Date.now(),
  };
}

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
    cards: ensureNums(data.cards.map((c) => sanitizeCard(c, cats || categories)).filter(Boolean)),
  };
}

export function matchesFilters(card) {
  if (filters.type && card.type !== filters.type) return false;
  if (filters.category && card.category !== filters.category) return false;
  if (filters.prio) {
    const p = priorityOf(card); // 0 = unlabelled (either judgement missing)
    if (filters.prio === 'none' ? p !== 0 : String(p) !== filters.prio) return false;
  }
  if (filters.tags.size && ![...filters.tags].every((t) => card.tags.includes(t))) return false;
  if (filters.search) {
    const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
    if (!haystack.includes(filters.search)) return false;
  }
  return true;
}

export const filtersActive = () => Boolean(filters.search || filters.type || filters.category || filters.prio || filters.tags.size);

export function moveCard(cardId, columnId, beforeId = null) {
  const card = getCard(cardId);
  if (!card || cardId === beforeId) return;
  if (!columnAccepts(card, columnId)) return;
  state.cards = state.cards.filter((c) => c.id !== cardId);
  card.columnId = columnId;
  card.updatedAt = Date.now();

  let index = -1;
  if (beforeId) index = state.cards.findIndex((c) => c.id === beforeId);
  if (index === -1) {
    let lastInColumn = -1;
    state.cards.forEach((c, i) => { if (c.columnId === columnId) lastInColumn = i; });
    index = lastInColumn + 1 || state.cards.length;
    if (lastInColumn === -1) index = state.cards.length;
  }
  state.cards.splice(index, 0, card);
  commit(`Moved ${cardLabel(card)} to ${columnTitle(columnId)}`);
}
