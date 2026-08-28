import { assistantState } from '../assistant/session.js';
import { cardLabel, catVal, controlVal, deadlineVal, effortVal, iuVal, typeVal } from '../core/cards.js';
import { catColor, categories } from '../core/categories.js';
import { COLUMNS, TYPES, TYPE_META } from '../core/constants.js';
import { habitCountVal, habitFreqVal, habitTimesVal } from '../core/habits.js';
import { planConflict, planSrcVal, planVal } from '../core/plan.js';
import { commit, short } from '../core/history.js';
import { deleteCard } from './card-actions.js';
import { $, announce, columnTitle, getCard } from './dom.js';
import { discardEdit, reviewingEditId, setReviewingEditId } from './proposals.js';

// The card dialog — the one place a card is edited by hand, and the same path
// a suggested edit from the Assistant takes before it is saved.

const dialog = $('#card-dialog');
const form = $('#card-form');
let editingId = null;

// The type picker is built once (types are fixed); the category picker is
// rebuilt on every open, because the registry is the user's to change.
(() => {
  const typeWrap = $('#type-picker-options');
  for (const t of TYPES) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'type';
    input.value = t;
    const span = document.createElement('span');
    span.className = `badge type-${t}`;
    span.textContent = `${TYPE_META[t].glyph} ${TYPE_META[t].label}`;
    label.append(input, span);
    typeWrap.append(label);
  }
  // The cadence fields only make sense for a habit, so they appear with the
  // stamp rather than sitting empty on every other card.
  typeWrap.addEventListener('change', syncHabitFields);
})();

function syncHabitFields() {
  const habit = form.elements.type.value === 'habit';
  $('#card-habit').hidden = !habit;
  // A habit repeats on a calendar of its own — planning it for a Tuesday would
  // say nothing — so the plan is hidden rather than ignored.
  $('#card-plan').hidden = habit;
}

// --- The plan: three dropdowns that assemble one partial date ---------------
// Year alone is a plan ("some time in 2028"); Month needs the Year and Day
// needs the Month, which is why each list is dead until the one to its left
// has an answer. `planSrc` starts as whatever the card carried and becomes
// 'user' the moment one of these is touched, because from then on the deadline
// must stop overwriting what a person chose.

const planYear = $('#card-plan-year');
const planMonth = $('#card-plan-month');
const planDay = $('#card-plan-day');
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];
let planSrcNow = 'auto';

const pad2 = (n) => String(n).padStart(2, '0');
const fillOptions = (select, options, value) => {
  select.replaceChildren();
  for (const [v, text] of options) {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = text;
    select.append(opt);
  }
  select.value = options.some(([v]) => v === value) ? value : '';
};

/** Years offered: a few back for a plan that has slipped, twenty on for a
 *  dream, and always the card's own year even when it is outside that. */
function yearOptions(own) {
  const now = new Date().getFullYear();
  const years = new Set();
  for (let y = now - 3; y <= now + 20; y++) years.add(y);
  if (own) years.add(Number(own));
  return [['', '—'], ...[...years].sort().map((y) => [String(y), String(y)])];
}

/** Read the three fields as one plan string. */
const readPlan = () => planVal([planYear.value, planMonth.value, planDay.value]
  .filter(Boolean).join('-'));

function paintPlan(plan) {
  const [y = '', m = '', d = ''] = planVal(plan).split('-');
  fillOptions(planYear, yearOptions(y), y);
  fillOptions(planMonth,
    [['', '—'], ...MONTH_NAMES.map((name, i) => [pad2(i + 1), name])], m);
  const days = y && m ? new Date(Number(y), Number(m), 0).getDate() : 31;
  fillOptions(planDay,
    [['', '—'], ...Array.from({ length: days }, (_, i) => [pad2(i + 1), String(i + 1)])], d);
  syncPlanFields();
}

/** The cascade, and the two messages under it. Returns the plan as read. */
function syncPlanFields() {
  planMonth.disabled = !planYear.value;
  if (planMonth.disabled) planMonth.value = '';
  planDay.disabled = !planMonth.value;
  if (planDay.disabled) planDay.value = '';

  const plan = readPlan();
  const deadline = deadlineVal($('#card-deadline').value);
  const bad = planConflict(plan, deadline);
  const error = $('#card-plan-error');
  error.hidden = !bad;
  error.textContent = bad
    ? `This plan starts after the deadline (${deadline}). Move the plan earlier or the deadline later — the card cannot be saved like this.`
    : '';
  $('#card-plan').classList.toggle('has-error', bad);

  $('#card-plan-follow').hidden = planSrcNow === 'auto' || !deadline;
  $('#card-plan-hint').textContent = planSrcNow === 'auto'
    ? (deadline ? 'Following the deadline. Pick a year to plan it yourself.'
                : 'Leave it empty, or pick a year — the month and day are optional.')
    : 'Your own plan. The deadline no longer changes it.';
  return plan;
}

for (const select of [planYear, planMonth, planDay]) {
  select.addEventListener('change', () => {
    planSrcNow = 'user'; // a person chose: the deadline stops writing here
    const y = planYear.value, m = planMonth.value;
    if (y && m) {
      // A shorter month can leave the 31st selected — repaint the day list.
      const days = new Date(Number(y), Number(m), 0).getDate();
      const keep = planDay.value;
      fillOptions(planDay,
        [['', '—'], ...Array.from({ length: days }, (_, i) => [pad2(i + 1), String(i + 1)])],
        Number(keep) <= days ? keep : '');
    }
    syncPlanFields();
  });
}
$('#card-plan-follow').addEventListener('click', () => {
  planSrcNow = 'auto';
  paintPlan(deadlineVal($('#card-deadline').value));
});
$('#card-deadline').addEventListener('change', () => {
  if (planSrcNow === 'auto') paintPlan(deadlineVal($('#card-deadline').value));
  else syncPlanFields(); // the pair may have just become impossible
});

// Forgiving on the way in, strict on the way out: "7:30" is what people type.
const padTime = (t) => (/^\d:\d\d$/.test(t) ? '0' + t : t);
const readHabitTimes = (count) => habitTimesVal(
  $('#card-habit-times').value.split(',').map((t) => padTime(t.trim())).filter(Boolean), count);

function rebuildCategoryPicker() {
  const catWrap = $('#category-picker-options');
  catWrap.innerHTML = '';
  const mkCat = (value, text, color) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'category';
    input.value = value;
    const span = document.createElement('span');
    span.className = 'cat-swatch';
    span.style.setProperty('--cat', color);
    span.textContent = text;
    label.append(input, span);
    return label;
  };
  catWrap.append(mkCat('', 'None', 'var(--ink-soft)'));
  for (const c of categories) catWrap.append(mkCat(c.id, c.label, catColor(c.id)));
}

// Decisional balance — on a card tagged "decision", notes lines beginning
// with + / − (or -) read back as a live two-column pro/con sheet under the
// notes box. Pure rendering: the notes text stays the single source.
function updateBalancePreview() {
  const preview = $('#balance-preview');
  const tags = $('#card-tags').value.split(',').map((t) => t.trim().toLowerCase());
  const pros = [], cons = [];
  if (tags.includes('decision')) {
    for (const line of $('#card-notes').value.split('\n')) {
      const t = line.trim();
      if (t.startsWith('+')) pros.push(t.slice(1).trim());
      else if (t.startsWith('-') || t.startsWith('−')) cons.push(t.slice(1).trim());
    }
  }
  const show = pros.length > 0 || cons.length > 0;
  preview.hidden = !show;
  if (!show) return;
  const fill = (colSel, items) => {
    const ul = $(`${colSel} ul`, preview);
    ul.innerHTML = '';
    for (const text of items) {
      const li = document.createElement('li');
      li.textContent = text;
      ul.append(li);
    }
  };
  fill('.balance-pro', pros);
  fill('.balance-con', cons);
}
$('#card-notes').addEventListener('input', updateBalancePreview);
$('#card-tags').addEventListener('input', updateBalancePreview);

// `suggested` prefills the form with an Assistant suggestion instead of what
// the card currently says. It fills the *form*, deliberately, and not the card:
// the values are a draft the user can change or abandon, and until they submit
// nothing has happened to the board.
export function openDialog(cardId, suggested = null) {
  const card = getCard(cardId);
  if (!card) return;
  editingId = cardId;
  const shown = suggested ? { ...card, ...suggested } : card;
  rebuildCategoryPicker();
  $('#card-title').value = shown.title;
  $('#card-notes').value = shown.notes;
  $('#card-tags').value = (shown.tags || []).join(', ');
  updateBalancePreview();
  $('#card-importance').value = iuVal(shown.importance);
  $('#card-urgency').value = iuVal(shown.urgency);
  $('#card-deadline').value = deadlineVal(card.deadline);
  $('#card-effort').value = effortVal(card.effort);
  $('#card-control').value = controlVal(card.control);
  for (const radio of form.elements.type) radio.checked = radio.value === shown.type;
  for (const radio of form.elements.category) radio.checked = radio.value === (shown.category || '');
  // A card being stamped Habit for the first time starts at once a day.
  $('#card-habit-freq').value = card.habitFreq || 'daily';
  $('#card-habit-count').value = String(card.habitCount || 1);
  $('#card-habit-times').value = card.habitTimes.join(', ');
  planSrcNow = planSrcVal(shown.planSrc ?? card.planSrc);
  paintPlan(shown.plan ?? card.plan);
  syncHabitFields();
  const fmt = (ts) => new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  $('#card-meta').textContent =
    `${cardLabel(card)} · in ${columnTitle(card.columnId)} · added ${fmt(card.createdAt)} · updated ${fmt(card.updatedAt)}`;
  dialog.showModal();
  $('#card-title').focus();
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const card = getCard(editingId);
  // The one save this app refuses: a plan that starts after the deadline is a
  // promise to begin after the thing was already due. Nothing is written, the
  // error stands under the fieldset, and the focus goes to the field that can
  // fix it.
  if (card && card.type !== 'habit' && planConflict(readPlan(), deadlineVal($('#card-deadline').value))) {
    syncPlanFields();
    announce($('#card-plan-error').textContent);
    planYear.focus();
    return;
  }
  if (card) {
    card.title = $('#card-title').value.trim() || card.title;
    card.notes = $('#card-notes').value;
    card.type = typeVal(form.elements.type.value);
    // Written only for a habit, so editing an ordinary card can never touch
    // a cadence — or a history — it was not asked about.
    if (card.type === 'habit') {
      card.habitFreq = habitFreqVal($('#card-habit-freq').value) || 'daily';
      card.habitCount = habitCountVal($('#card-habit-count').value);
      card.habitTimes = readHabitTimes(card.habitCount);
    }
    card.category = catVal(form.elements.category.value);
    card.importance = iuVal($('#card-importance').value);
    card.urgency = iuVal($('#card-urgency').value);
    card.deadline = deadlineVal($('#card-deadline').value);
    // A habit's plan is left exactly as it was: its fields are hidden, so the
    // dialog was never asked about them.
    if (card.type !== 'habit') {
      card.planSrc = planSrcVal(planSrcNow);
      card.plan = card.planSrc === 'auto' ? card.deadline : readPlan();
    }
    // A changed effort/control is a human judgment — record the provenance so
    // the brain's future estimator knows never to overwrite it.
    const effort = effortVal($('#card-effort').value);
    if (effort !== effortVal(card.effort)) { card.effort = effort; card.effortSrc = 'user'; }
    const control = controlVal($('#card-control').value);
    if (control !== controlVal(card.control)) { card.control = control; card.controlSrc = 'user'; }
    card.tags = $('#card-tags').value
      .split(',')
      .map((t) => t.trim().toLowerCase())
      .filter(Boolean);
    // The dialog has no column control, so a suggested move is carried here.
    // Read from the suggestion rather than the form for that one field.
    const reviewed = assistantState.edits.find((e) => e.id === reviewingEditId);
    if (reviewed && COLUMNS.some((c) => c.id === reviewed.fields.columnId)) {
      card.columnId = reviewed.fields.columnId;
    }
    card.updatedAt = Date.now();
    commit(`Edited ${cardLabel(card)} “${short(card.title)}”`);
    // Answered, so it leaves the list. After the commit: the suggestion is the
    // only record of what was asked, and losing it before the save landed
    // would leave the user with neither.
    if (reviewingEditId) discardEdit(reviewingEditId, 'Suggestion applied and saved');
  }
  dialog.close();
});

$('#cancel-dialog').addEventListener('click', () => dialog.close());

$('#delete-card').addEventListener('click', () => {
  const id = editingId;
  dialog.close();
  deleteCard(id);
});

dialog.addEventListener('close', () => { editingId = null; setReviewingEditId(null); });
