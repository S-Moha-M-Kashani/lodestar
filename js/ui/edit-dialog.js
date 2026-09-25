import { cardLabel, catVal, columnAccepts, controlVal, deadlineVal, effortVal, iuVal, matchesFilters, placeCard, typeVal, uid } from '../core/cards.js';
import { catColor, categories } from '../core/categories.js';
import { COLUMNS, TYPES, TYPE_META } from '../core/constants.js';
import { dueWords } from '../core/direction.js';
import { blankDraft, draftFrom } from '../core/draft.js';
import { HABIT_EVERY, habitCountVal, habitFreqVal, habitTimesVal, scheduleSentence } from '../core/habits.js';
import { canHaveChildren, childrenOf } from '../core/nesting.js';
import { MONTHS, SLOT_READS, WEEKDAYS, slotsVal } from '../core/schedule.js';
import { planConflict, planSrcVal, planVal, planWords } from '../core/plan.js';
import { commit, short } from '../core/history.js';
import { filters, nextNum, state } from '../core/state.js';
import { deleteCard } from './card-actions.js';
import { childRow, childrenSummary } from './children.js';
import { $, announce, columnTitle, getCard } from './dom.js';
import { habitTape } from './habits.js';
import { renderPlanCalendar, resetPlanCalendar } from './plan-calendar.js';
import { discardEdit, reviewingEditId, setReviewingEditId } from './proposals.js';

// The card dialog — the one place a card is edited by hand, and the same path
// a suggested edit from the Assistant takes before it is saved.

const dialog = $('#card-dialog');
const form = $('#card-form');
let editingId = null;
// Create mode: the card-shaped object being composed, and — for a Duplicate —
// the id of the card it was copied from. The source's id is held here and not
// as a field on the draft, because the draft object itself is what gets spliced
// into state.cards on save and sent to the server: a `fromId` riding along
// would be a field nothing reads and `cleanCard` would have to learn to drop.
let draft = null;
let sourceId = null;
// Import-review mode: the step being weighed up, and the driver to answer with.
// The step is held here rather than passed to the buttons because the three of
// them are wired once, like every other control in this file.
let reviewStep = null;
let reviewDecide = null;

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
  // In Progress takes no habit (columnAccepts). Disabled rather than hidden,
  // so the rule is visible where it applies; and a card sitting in that column
  // when it is stamped moves its selection to the Inbox, which is exactly what
  // sanitizeCard does to such a card on the way in — a disabled radio left
  // checked would be a form saying something the save cannot honour.
  const inProgress = $('#card-column input[value="in-progress"]');
  inProgress.disabled = habit;
  if (habit && inProgress.checked) $('#card-column input[value="inbox"]').checked = true;
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
    [['', '—'], ...MONTHS.map((name, i) => [pad2(i + 1), name])], m);
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
  // Marked across the whole When block, not just the plan: the refusal is a
  // statement about the *pair*, and either field can resolve it. It sat under
  // the plan alone until 2026-09-07, which read as the plan being at fault.
  $('#card-when').classList.toggle('has-error', bad);

  // The plan read back at the precision it was given. Three dropdowns showing
  // a year, a month and a day never look wrong even when they say something
  // else, so the words are the honest check.
  const words = planWords(plan);
  $('#card-plan-words').textContent = words ? `→ ${words}` : '';

  $('#card-plan-follow').hidden = planSrcNow === 'auto' || !deadline;
  $('#card-plan-hint').textContent = planSrcNow === 'auto'
    ? (deadline ? 'Following the deadline. Pick a year to plan it yourself.'
                : 'Leave it empty, or pick a year — the month and day are optional.')
    : 'Your own plan. The deadline no longer changes it.';
  paintPlanCalendar(plan);
  return plan;
}

// --- The plan, on a calendar ------------------------------------------------
// A second way into the same three dropdowns, for the same reason `Between…`
// stopped being two date inputs: a reader usually wants to find a day rather
// than to know one. The grid can only ever name a whole day, so the dropdowns
// stay — and the calendar's own two controls take the day, and then the month,
// back off again. Everything lands through `paintPlan`, so there is still one
// place that writes the plan.

const planCal = $('#card-plan-calendar');
const planCalBtn = $('#card-plan-cal-btn');

function paintPlanCalendar(plan) {
  if (planCal.hidden) return;
  renderPlanCalendar(planCal, plan, (next) => {
    planSrcNow = 'user'; // a person chose: the deadline stops writing here
    paintPlan(next);
  });
}

planCalBtn.addEventListener('click', () => {
  planCal.hidden = !planCal.hidden;
  planCalBtn.setAttribute('aria-expanded', String(!planCal.hidden));
  paintPlanCalendar(readPlan());
});

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

// --- The schedule: one row per repetition -----------------------------------
// A habit says how often *and* when. `slotsNow` is the working schedule while
// the dialog is open — painted from the card, written by the controls, read at
// the save — the same shape `planSrcNow` above already has. It holds all four
// slot fields whatever the cadence reads, which is what lets a weekly habit
// moved to monthly and back have its weekday again: the row simply stops
// *showing* a field, it never clears it.

let slotsNow = [];

// Which control the middle column is. A weekday for the two cadences whose
// period is measured in days, a day of the month for the two that are not —
// so a daily habit is shown that a weekday exists and does not apply, rather
// than being shown a 1–31 list that would never apply to it either.
const middleIsWeekday = (freq) => freq === 'daily' || freq === 'weekly';

/** A slot select: the options, the value, and — when the cadence does not read
 *  this field — disabled with an accessible reason. Disabled and *visible*
 *  rather than removed, so the rule is shown where it applies; a control that
 *  greys without saying why is a rule the reader has to guess. */
function slotSelect(cls, label, options, value, reads, why) {
  const wrap = document.createElement('label');
  wrap.className = 'field slot-field';
  wrap.append(document.createTextNode(label));
  const sel = document.createElement('select');
  sel.className = cls;
  fillOptions(sel, options, value);
  sel.disabled = !reads;
  if (!reads) {
    sel.title = why;
    sel.setAttribute('aria-label', `${label} — ${why}`);
  } else {
    sel.setAttribute('aria-label', label);
  }
  wrap.append(sel);
  return wrap;
}

const dayOptions = () =>
  [['', '—'], ...Array.from({ length: 31 }, (_, i) => [pad2(i + 1), String(i + 1)])];
const monthOptions = () => [['', '—'], ...MONTHS.map((n, i) => [pad2(i + 1), n])];
const weekdayOptions = () => [['', '—'], ...WEEKDAYS.map((n, i) => [String(i + 1), n])];

/** Paint one row per repetition, from `slotsNow`.
 *
 *  An <ol>, because these genuinely are a sequence — the repetitions of one
 *  period, in order — and the marker is that repetition's own ordinal. This is
 *  the one place a numbered list is the honest shape rather than decoration. */
function paintSchedule() {
  const freq = habitFreqVal($('#card-habit-freq').value) || 'daily';
  const count = habitCountVal($('#card-habit-count').value);
  slotsNow = slotsVal(slotsNow, count);
  const reads = SLOT_READS[freq];
  const noun = HABIT_EVERY[freq];

  const list = $('#card-schedule');
  list.replaceChildren();
  slotsNow.forEach((slot, i) => {
    const li = document.createElement('li');
    li.className = 'schedule-row';

    li.append(slotSelect('slot-month', 'Month', monthOptions(), slot.month, reads.month,
      `only a yearly habit falls in a particular month; this one repeats every ${noun}`));

    if (middleIsWeekday(freq)) {
      li.append(slotSelect('slot-when', 'Day', weekdayOptions(), slot.weekday, reads.weekday,
        `a daily habit is expected every day, so it names no weekday`));
    } else {
      li.append(slotSelect('slot-when', 'Date', dayOptions(), slot.day, reads.day,
        `this habit repeats every ${noun}, which has no day of the month to name`));
    }

    const timeWrap = document.createElement('label');
    timeWrap.className = 'field slot-field';
    timeWrap.append(document.createTextNode('At'));
    const time = document.createElement('input');
    time.type = 'time';
    time.className = 'slot-time';
    time.value = slot.time;
    time.setAttribute('aria-label', `Time of repetition ${i + 1}`);
    timeWrap.append(time);
    li.append(timeWrap);

    // Every control writes only the field its cadence reads, straight into the
    // working slot — so the fields it does not show keep whatever they held.
    li.addEventListener('change', () => {
      const s = slotsNow[i];
      if (reads.month) s.month = li.querySelector('.slot-month').value;
      if (reads.weekday) s.weekday = li.querySelector('.slot-when').value;
      if (reads.day) s.day = li.querySelector('.slot-when').value;
      s.time = li.querySelector('.slot-time').value;
      slotsNow = slotsVal(slotsNow, count); // re-validate what was just typed
      syncScheduleWords();
    });

    list.append(li);
  });
  syncScheduleWords();
}

/** The schedule in plain words, under the rows. */
function syncScheduleWords() {
  $('#card-schedule-words').textContent = scheduleSentence({
    habitFreq: habitFreqVal($('#card-habit-freq').value) || 'daily',
    habitCount: habitCountVal($('#card-habit-count').value),
    habitTimes: slotsNow,
  });
}

// The rows follow the count as it is typed, and the cadence decides which of
// their fields are live — so both controls repaint them. `input` on the count,
// not `change`: typing 5 should show five rows before the field is left.
$('#card-habit-count').addEventListener('input', paintSchedule);
$('#card-habit-freq').addEventListener('change', paintSchedule);

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

/** The children block: the count in the label, the rows, and the way to add
 *  one — or, where a card can hold none, the reason said out loud.
 *
 *  A control that is silently absent is a rule nobody can learn. A child cannot
 *  itself be broken up (the lineage is one level deep) and a habit is at neither
 *  end of one, and both of those are worth a sentence. */
function paintChildren(card) {
  const kids = childrenOf(state.cards, card.id);
  $('#card-children-count').textContent = childrenSummary(kids);
  const body = $('#card-children-body');
  body.replaceChildren();

  for (const kid of kids) body.append(childRow(kid));

  if (canHaveChildren(card)) {
    const add = document.createElement('button');
    add.type = 'button';
    add.id = 'card-add-child';
    add.className = 'btn ghost add-child';
    add.textContent = '+ Add a child card';
    add.addEventListener('click', () => addChildOf(card.id));
    body.append(add);
    return;
  }
  const why = document.createElement('p');
  why.className = 'field-hint';
  why.textContent = card.parentId
    ? 'This card is part of another one, and the parts of a card are not themselves broken up.'
    : 'A habit is neither a parent nor a child — its repetitions are the punch strip in the rail.';
  body.append(why);
}

// The head's dates, all three of them, in one place: the meta line's added and
// updated, and the finished line below it. A second formatter would be a second
// answer to how this dialog writes a day.
const fmtDay = (ts) =>
  new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

/** The parent line, above the card's own title and outside every fold.
 *
 *  Painted from the hidden input rather than from the card, because that field
 *  is what the save reads: detaching has to be visible before it is saved, and
 *  a line painted from the record would go on naming a parent the form has
 *  already let go of. */
function syncParentLine() {
  const id = $('#card-parent-id').value;
  const parent = id ? getCard(id) : null;
  const line = $('#card-parent-line');
  line.hidden = !parent;
  if (!parent) return;
  const open = $('#card-parent-open');
  open.textContent = `${cardLabel(parent)} ${parent.title}`;
  open.title = 'Open the card this one is part of';
}

/** The dialog's chrome, set from the mode on *every* open rather than only in
 *  create mode. A draft that hid Delete and the meta line and left them hidden
 *  would leave the next card it opened with no way to delete it and no history
 *  to read; a heading is only ever right for the mode that last wrote it. */
function paintMode(creating) {
  $('#dialog-title').textContent = creating ? 'New card' : 'Edit card';
  $('#save-card').textContent = creating ? 'Add card' : 'Save changes';
  // A draft has no number, no column it has ever been in and no dates.
  $('#card-meta').hidden = creating;
  $('#delete-card').hidden = creating;
  // And no children, nor anywhere to put one: a child names a parent by id,
  // and a draft has none until it is saved. Hidden rather than shown empty,
  // which would be a fold over a list that cannot exist yet.
  $('#card-children').hidden = creating;
}

/** Paint every field of the form from the card being shown.
 *
 *  One argument: `shown` is a card, an unsaved draft, or a card with an
 *  Assistant suggestion laid over it (`{ ...card, ...suggested }`), which
 *  already falls back to the stored value for every field the suggestion does
 *  not name. It took two until 2026-09-02, with the deadline painted from the
 *  stored record — so a suggested due date was shown as the card's old one and
 *  the save wrote the old one back, dropping the suggestion in silence. A
 *  reviewer approves what the form says, so the form has to say what is being
 *  suggested; no field here may come from anywhere else. */
function paintForm(shown) {
  rebuildCategoryPicker();
  $('#card-title').value = shown.title;
  $('#card-notes').value = shown.notes;
  $('#card-tags').value = (shown.tags || []).join(', ');
  updateBalancePreview();
  $('#card-importance').value = iuVal(shown.importance);
  $('#card-urgency').value = iuVal(shown.urgency);
  $('#card-deadline').value = deadlineVal(shown.deadline);
  $('#card-effort').value = effortVal(shown.effort);
  $('#card-control').value = controlVal(shown.control);
  for (const radio of form.elements.type) radio.checked = radio.value === shown.type;
  for (const radio of form.elements.column) radio.checked = radio.value === shown.columnId;
  for (const radio of form.elements.category) radio.checked = radio.value === (shown.category || '');
  // A card being stamped Habit for the first time starts at once a day.
  $('#card-habit-freq').value = shown.habitFreq || 'daily';
  $('#card-habit-count').value = String(shown.habitCount || 1);
  // Copied row by row: the rows are what the controls write into, and writing
  // through to the card's own slots would edit it before the save.
  slotsNow = slotsVal((shown.habitTimes || []).map((x) => ({ ...x })),
    habitCountVal(shown.habitCount));
  paintSchedule();
  // The parent, as a form field like every other one.
  $('#card-parent-id').value = shown.parentId || '';
  syncParentLine();
  planSrcNow = planSrcVal(shown.planSrc);
  // Shut on every open, and with no memory of the month it was left on: a grid
  // still showing the last card's June is a calendar answering a question
  // nobody asked.
  planCal.hidden = true;
  planCalBtn.setAttribute('aria-expanded', 'false');
  resetPlanCalendar();
  paintPlan(shown.plan);
  syncHabitFields();
  // Closed on every open, whether or not there are any: it is a list of other
  // cards rather than a setting of this one, so the rule above does not apply.
  $('#card-children').open = false;
  // The completion history, in the block where the cadence that produced it is
  // set. It was drawn on the board card until 2026-09-07, and a habit is not
  // drawn as a board card. Folded, and rebuilt here rather than in openDialog
  // because the dialog is repainted in place when it walks from a card to its
  // parent — a tape left behind would be the previous card's record.
  $('#card-habit-history').open = false;
  $('#card-habit-tape').replaceChildren(
    ...(shown.type === 'habit' && shown.habitFreq ? [habitTape(shown)] : []));
  // When it was finished, if it was. Derived from the card like the parent line
  // and stored nowhere, so it cannot go stale against the card in front of it —
  // and it is `doneAt` alone, never `updatedAt`, because a date that is really
  // "when this was last touched" is the untruth the field was added to end.
  const done = $('#card-done-line');
  done.hidden = !shown.doneAt;
  done.textContent = shown.doneAt ? `✓ finished ${fmtDay(shown.doneAt)}` : '';
  syncFolds(true);
}

// On a phone the dialog fits one screen: four rows show their value as one
// line (`.prop-fold`) and open on a tap, and styles.css hides the controls of
// a shut row. The line is read off the controls themselves, so it says what a
// Save would write. A wide window never shows the line or folds a row.
const optionText = (sel) => sel.selectedOptions[0]?.text || '';
const FOLDS = [
  ['#card-habit', () =>
    `${$('#card-habit-count').value} × a ${optionText($('#card-habit-freq'))}`],
  ['.compass-row', () =>
    ['#card-importance', '#card-urgency', '#card-effort', '#card-control']
      .map((id) => $(id)).filter((sel) => sel.value).map(optionText).join(' · ')],
  ['#card-when', () =>
    [$('#card-deadline').value && dueWords($('#card-deadline').value),
      planWords(readPlan()) && `plan ${planWords(readPlan())}`]
      .filter(Boolean).join(' · ') || 'No date'],
  ['#card-tags-row', () => $('#card-tags').value.trim() || 'No tags'],
].map(([sel, words]) => [$(sel), words]);

// The width the phone rules in styles.css apply below.
const narrow = () => matchMedia('(max-width: 640px)').matches;

/** On a phone the title and the notes grow to fit what they hold, up to eight
 *  lines of notes, so a short card leaves no blank lines and a long one hides
 *  nothing. A wide window keeps the rows the markup gives them. */
function fitWords() {
  for (const el of [$('#card-title'), $('#card-notes')]) {
    el.style.height = '';
    if (!narrow()) continue;
    el.style.height = '0';
    const max = el.id === 'card-notes' ? 8 * 1.6 * 16 + 14 : Infinity;
    el.style.height = `${Math.min(el.scrollHeight + 2, max)}px`;
  }
}
for (const id of ['#card-title', '#card-notes']) $(id).addEventListener('input', fitWords);

function setFold(row, open) {
  row.classList.toggle('open', open);
  row.querySelector('.prop-fold').setAttribute('aria-expanded', String(open));
}

/** Rewrite every fold's line; `shut` also closes them, which every open does.
 *  The chosen chip of each picker is scrolled into view on the next frame,
 *  after the dialog has a layout: a phone swipes those rows sideways. */
function syncFolds(shut = false) {
  for (const [row, words] of FOLDS) {
    if (shut) setFold(row, false);
    row.querySelector('.prop-fold').textContent = words();
  }
  if (shut) {
    requestAnimationFrame(() => {
      fitWords();
      for (const on of form.querySelectorAll('.picker-options input:checked')) {
        on.closest('label').scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
    });
  }
}

for (const [row] of FOLDS) {
  row.querySelector('.prop-fold').addEventListener('click', () => {
    setFold(row, !row.classList.contains('open'));
  });
  // `click` too: the plan calendar sets the plan by pressing, not typing.
  for (const type of ['input', 'change', 'click']) row.addEventListener(type, () => syncFolds());
}

// `suggested` prefills the form with an Assistant suggestion instead of what
// the card currently says. It fills the *form*, deliberately, and not the card:
// the values are a draft the user can change or abandon, and until they submit
// nothing has happened to the board.
export function openDialog(cardId, suggested = null) {
  const card = getCard(cardId);
  if (!card) return;
  editingId = cardId;
  // Cleared here rather than by the close handler, because the dialog may be
  // open already: a child's dialog reaches its parent's, and a parent's reaches
  // a new child's, without the dialog ever shutting. See `showModal` below.
  draft = null;
  sourceId = null;
  // A suggestion naming a plan is marked as chosen, or it cannot survive being
  // approved: `planSrc` would stay whatever the card had — 'auto' on most cards
  // — and `readForm` resolves an 'auto' plan straight back to the deadline, so
  // the plan was painted, approved and thrown away.
  //
  // `'user'` is a choice among two workable values, not the only one. `'ai'` is
  // a first-class provenance on both sides (`planSrcVal`, and `resolvePlan`
  // keeps the plan for anything that is not 'auto'), and a *proposed* card
  // really does arrive carrying it — which is right, because no person has
  // looked at that card yet.
  //
  // A suggestion is the other case, and no agent may claim provenance on one:
  // `planSrc` is not in the server's allowlist, so a suggestion naming it has
  // that field dropped (Widget 1's `update_card` sends it and always has;
  // Widget 2's deliberately does not). It is therefore written here, on the
  // reviewer's side of that gate, and 'user' is the honest value: a person who
  // read this plan and pressed Save chose it, and a card whose provenance said
  // 'ai' would be one the owner could not tell from a plan nobody ever saw.
  const shown = suggested
    ? { ...card, ...suggested, ...(Object.hasOwn(suggested, 'plan') ? { planSrc: 'user' } : {}) }
    : card;
  paintMode(false);
  paintForm(shown);
  $('#card-meta').textContent =
    `${cardLabel(card)} · in ${columnTitle(card.columnId)} · added ${fmtDay(card.createdAt)} · updated ${fmtDay(card.updatedAt)}`;
  paintChildren(shown);
  // Only when it is not already showing. Navigating from a child to its parent
  // (or from a parent to a new child) repaints this dialog in place: showModal
  // on an open dialog throws, and closing it first is worse — `close` fires as
  // a *queued task*, so it would run after the new card had been painted and
  // reset `draft`, `editingId` and `sourceId` out from under it.
  if (!dialog.open) dialog.showModal();
  // On a phone, focusing the title raises the keyboard over half the card a
  // reader opened to read. A new card or a child still takes the caret: there
  // the reason for opening is to write.
  if (narrow()) document.activeElement?.blur();
  else $('#card-title').focus();
}

/** Add a child to this card: the dialog re-opens on a draft that already names
 *  it as its parent, in the hidden input the save reads. One card and one
 *  History entry when it is saved, none at all if it is cancelled — a draft
 *  costs nothing, which is the whole reason the create path works this way. */
function addChildOf(parentId) {
  const parent = getCard(parentId);
  if (!parent) return;
  // The dialog is repainted in place, not closed and reopened — see the note on
  // showModal in openDialog. What the close handler would have cleared is
  // cleared by openNewCard itself, except the review flag, which is set by the
  // caller that opens a suggestion and so has to be dropped by hand.
  setReviewingEditId(null);
  openNewCard(null, { parentId, category: parent.category });
}

// Up to the card this one is part of. Unsaved edits are left behind exactly as
// Cancel leaves them: this is a way to the other card, not a second save.
$('#card-parent-open').addEventListener('click', () => {
  const id = $('#card-parent-id').value;
  if (!id) return;
  setReviewingEditId(null);
  openDialog(id);
});

// Detaching writes the form, not the card: it is applied by the save like every
// other change, so one Cancel undoes it and one Save records it as part of the
// same edit rather than as a second entry in History.
$('#card-parent-detach').addEventListener('click', () => {
  $('#card-parent-id').value = '';
  syncParentLine();
  const card = getCard(editingId);
  if (card) paintChildren({ ...card, parentId: '' });
  announce('Detached — save the card to keep it');
});

/** Create mode: the dialog opens on a draft the board has never seen.
 *
 *  Nothing reaches `state.cards`, no ledger number is earned and no undo entry
 *  is written until `submit` — so Cancel leaves the board untouched. Creating
 *  the card first and deleting it on cancel was the alternative, and it burns a
 *  permanent `num` on an abandoned capture, records two undo entries for one
 *  act, and turns a cancel into a Trash entry somebody has to clean up.
 *
 *  `source` is the card a Duplicate was asked for; `draftFrom` decides which of
 *  its fields travel (and that its completion history does not). */
export function openNewCard(source = null, prefill = null) {
  editingId = null;
  sourceId = source ? source.id : null;
  // A blank capture inherits the drawer it was written in: with a category tab
  // or type filter open, the card belongs there. That was the quick-add form's
  // one good idea, and here the inherited values are visible and changeable
  // before the save. An unfiltered board reads '' on both, which blankDraft
  // turns into the standing defaults.
  draft = source
    ? draftFrom(source)
    : blankDraft({ type: filters.type, category: filters.category });
  // `prefill` is the one caller that knows something the filters do not: Add a
  // child card, which hands over the parent it was pressed on (and that
  // parent's life area, since a part of a piece of work usually belongs to the
  // same one). It is merged into the draft and *painted*, so the value is one
  // somebody saw before saving rather than one written behind the form.
  if (prefill) Object.assign(draft, prefill);
  paintMode(true);
  paintForm(draft);
  if (!dialog.open) dialog.showModal();
  $('#card-title').focus();
}

/** The card an import step would retire, drawn as a reading rather than a form.
 *
 *  Ledger number, words, where it is filed and what hangs off it — enough to
 *  recognise the card being replaced without opening it, and no control at all,
 *  because the only card being edited here is the one that would land. */
function paintReplaces(old) {
  const panel = $('#card-replaces');
  panel.hidden = !old;
  if (!old) { panel.replaceChildren(); return; }
  const kids = childrenOf(state.cards, old.id);
  const line = (cls, text) => {
    const el = document.createElement('p');
    el.className = cls;
    el.textContent = text;
    return el;
  };
  panel.replaceChildren(
    line('replaces-lead', 'On the board now'),
    line('replaces-num', cardLabel(old)),
    line('replaces-title', old.title),
    ...(old.notes ? [line('replaces-notes', old.notes)] : []),
    line('replaces-where', `in ${columnTitle(old.columnId)}`
      + (kids.length ? ` · ${kids.length} child card${kids.length === 1 ? '' : 's'}` : '')),
    // Said plainly, because it is the half of the swap nobody asked for. The
    // children are named too: they are not deleted with it, and a reader who
    // assumed a cascade would refuse a swap that costs nothing.
    line('replaces-fate', kids.length
      ? 'This card will be deleted — it goes to the Trash and can be brought back. Its child cards are not deleted; they move to the card replacing it.'
      : 'This card will be deleted — it goes to the Trash and can be brought back.'),
  );
}

/** Open one step of an import review in the ordinary card dialog.
 *
 *  The incoming card is painted as a draft, so every field is editable on the
 *  way in and there is no second form to learn — and `readForm` stays the one
 *  place a card is built from form input. `decide` is called with 'accept',
 *  'all', 'skip' or 'stop', and for the two that take the card, the card the
 *  form says: a reviewer approves what the form says.
 *
 *  @param {object} step     one step from planImport
 *  @param {object} progress { index, total }, 1-based
 *  @param {Function} decide the driver in js/ui/transfer.js
 */
export function openReviewStep(step, progress, decide) {
  reviewStep = step;
  reviewDecide = decide;
  editingId = null;
  draft = null;
  sourceId = null;
  const swap = step.kind === 'substitute';
  paintMode(true);
  paintForm(step.card);
  $('#dialog-title').textContent = swap ? 'Replace this card?' : 'Import this card?';
  $('#review-progress').textContent = `${progress.index} of ${progress.total}`;
  $('#card-actions').hidden = true;
  $('#review-actions').hidden = false;
  paintReplaces(swap ? step.replaces : null);
  dialog.classList.toggle('reviewing-swap', swap);
  // Never `close()` first: the dialog stays open from one step to the next, and
  // `close` fires as a queued task that would reset this state underneath the
  // step just painted — the same trap openDialog documents for a card walking
  // to its parent.
  if (!dialog.open) dialog.showModal();
  $('#card-title').focus();
}

/** End a review from the driver's side. The state is dropped *before* the
 *  close, so the close handler does not read it as the reviewer abandoning the
 *  import and call the driver back a second time. */
export function closeReview() {
  reviewStep = null;
  reviewDecide = null;
  dialog.close();
}

/** One decision about the step on screen. */
function decideReview(kind) {
  const step = reviewStep;
  const decide = reviewDecide;
  if (!step || !decide) return;
  if (kind === 'skip') { decide('skip'); return; }
  const title = $('#card-title');
  title.value = title.value.trim();
  if (!title.value) { title.reportValidity(); return; }
  // The same refusal an ordinary save makes, for the same reason: a plan that
  // starts after the deadline is a promise to begin after the thing was due.
  // An import must not be the way one gets onto the board.
  if (form.elements.type.value !== 'habit'
      && planConflict(readPlan(), deadlineVal($('#card-deadline').value))) {
    syncPlanFields();
    announce($('#card-plan-error').textContent);
    setFold($('#card-when'), true);
    planYear.focus();
    return;
  }
  const card = { ...step.card };
  readForm(card);
  decide(kind, card);
}

$('#review-accept').addEventListener('click', () => decideReview('accept'));
$('#review-accept-all').addEventListener('click', () => decideReview('all'));
$('#review-skip').addEventListener('click', () => decideReview('skip'));

/** Write the form into a card-shaped object — the one place on this board a
 *  card is built from form input. The Inbox's quick-add form used to be a
 *  second one, and two construction sites meant two answers to the question of
 *  which fields a card even has. `card` is either the live card being edited or
 *  the dialog's unsaved draft, and nothing here needs to know which. */
function readForm(card) {
  card.title = $('#card-title').value.trim() || card.title;
  card.notes = $('#card-notes').value;
  card.type = typeVal(form.elements.type.value);
  // Written only for a habit, so editing an ordinary card can never touch
  // a cadence — or a history — it was not asked about.
  if (card.type === 'habit') {
    card.habitFreq = habitFreqVal($('#card-habit-freq').value) || 'daily';
    card.habitCount = habitCountVal($('#card-habit-count').value);
    card.habitTimes = slotsVal(slotsNow, card.habitCount);
  }
  // Where it is filed. Refused rather than written when the card cannot be
  // there, so this control can never put a habit in In Progress — the type is
  // read above, so `card` already carries the stamp being saved.
  const column = form.elements.column.value;
  if (COLUMNS.some((c) => c.id === column) && columnAccepts(card, column)) card.columnId = column;
  // The parent, read off the form like every other field. The button that
  // opened a child's draft writes it into the hidden input and nowhere else —
  // a reviewer approves what the form says, so nothing may reach the card by a
  // route the form does not show. It is *not* validated for existence here:
  // that is a question about the whole list, and resolveLineage answers it once
  // over the sanitized board.
  card.parentId = $('#card-parent-id').value;
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
  // the brain's future estimator knows never to overwrite it. A draft is
  // compared against the values it was opened with, so a duplicate keeps the
  // provenance it copied and a blank capture left alone stays at 'default'.
  const effort = effortVal($('#card-effort').value);
  if (effort !== effortVal(card.effort)) { card.effort = effort; card.effortSrc = 'user'; }
  const control = controlVal($('#card-control').value);
  if (control !== controlVal(card.control)) { card.control = control; card.controlSrc = 'user'; }
  card.tags = $('#card-tags').value
    .split(',')
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const card = getCard(editingId);
  // The card being saved, or the draft standing in for one until it is.
  const subject = card ?? draft;
  if (!subject) { dialog.close(); return; }
  // The one save this app refuses: a plan that starts after the deadline is a
  // promise to begin after the thing was already due. Nothing is written, the
  // error stands in the When row, and the focus goes to the field that can fix
  // it. Nothing in the dialog folds any more, so the refusal has nothing to
  // open first — that guard existed for the settings fold, which is gone, and
  // a message inside a closed <details> is what it was protecting against. The
  // type is read off the draft as well as the card, because a guard written
  // against `card` alone skipped the check entirely for every new card.
  if (subject.type !== 'habit' && planConflict(readPlan(), deadlineVal($('#card-deadline').value))) {
    syncPlanFields();
    announce($('#card-plan-error').textContent);
    // The plan, of the two: it is the field the message names, and the deadline
    // is a date somebody has already committed to. The message and the mark now
    // cover both, so either can be the one they reach for.
    setFold($('#card-when'), true);
    planYear.focus();
    return;
  }
  if (card) {
    // Where it was filed, read before the form overwrites the field: a card's
    // *place in the array* is what decides where it appears in a column, so a
    // column changed on the form is a real move and not just a field write.
    // A suggested move needs no special case any more — the dialog has a column
    // control now, so the suggestion is painted into it like every other field
    // and comes back through readForm with the rest.
    const wasIn = card.columnId;
    readForm(card);
    if (card.columnId !== wasIn) placeCard(card.id, card.columnId);
    card.updatedAt = Date.now();
    commit(`Edited ${cardLabel(card)} “${short(card.title)}”`);
    // Answered, so it leaves the list. After the commit: the suggestion is the
    // only record of what was asked, and losing it before the save landed
    // would leave the user with neither.
    if (reviewingEditId) discardEdit(reviewingEditId, 'Suggestion applied and saved');
  } else {
    // A capture with nothing written in it is not a card. The field is
    // `required`, so the browser refuses an empty one by itself; trimming the
    // value back into it is what makes whitespace count as empty too, and
    // reportValidity then says so in the browser's own words, leaving no
    // custom validity state behind for the next save to clear.
    const title = $('#card-title');
    title.value = title.value.trim();
    if (!title.value) { title.reportValidity(); return; }
    readForm(draft);
    // Only now does the draft become a card. The number is earned at the save,
    // which is why a cancelled capture leaves no gap in the ledger.
    const now = Date.now();
    draft.id = uid();
    draft.num = nextNum();
    draft.createdAt = now;
    draft.updatedAt = now;
    // A duplicate joins its source: the same column (copied by `draftFrom`) and
    // the place immediately after it, so the copy appears beside what it came
    // from. Every other capture goes to the top of the Inbox, where one is
    // looked for — and so does a duplicate whose source was deleted while the
    // copy was being written, rather than landing at array index 0.
    // Only when the copy is still filed where its source is: "immediately after
    // it" is a position in that column, and a duplicate the form has since sent
    // to another one would land at an arbitrary place inside it.
    const source = sourceId ? state.cards.find((c) => c.id === sourceId) : null;
    const after = source && source.columnId === draft.columnId
      ? state.cards.indexOf(source) : -1;
    if (after !== -1) {
      state.cards.splice(after + 1, 0, draft);
    } else {
      // An ordinary capture — and a Duplicate whose source vanished while the
      // copy was being written (a delete in another tab, a cross-machine
      // adopt), or was sent to another column on the form. It goes to the head
      // of whichever column the form names, which is the Inbox unless the
      // person said otherwise. The column is no longer forced back to 'inbox'
      // here: it is a field on the form now, so the value is one somebody saw
      // and chose rather than one `draftFrom` copied behind their back.
      const firstInCol = state.cards.findIndex((c) => c.columnId === draft.columnId);
      state.cards.splice(firstInCol === -1 ? state.cards.length : firstInCol, 0, draft);
    }
    // A search, tag or priority filter could still hide the fresh card —
    // clear those so the capture never vanishes silently.
    if (!matchesFilters(draft)) {
      filters.search = '';
      filters.tags.clear();
      filters.prio = '';
      // The search box is painted from `filters.search` by the render this
      // commit triggers, so clearing the field is the same act as clearing the
      // filter — and reaching for #search here would throw on the views that
      // have no capture row to hold one.
      $('#prio-filter').value = '';
    }
    commit(`Added ${cardLabel(draft)} “${short(draft.title)}”`);
    announce(`Added “${draft.title}” to ${columnTitle(draft.columnId)}`);
  }
  dialog.close();
});

$('#cancel-dialog').addEventListener('click', () => dialog.close());

$('#delete-card').addEventListener('click', () => {
  const id = editingId;
  dialog.close();
  deleteCard(id);
});

// Nothing about one opening survives into the next: a draft left behind here
// would be the `subject` of a later edit-mode save whose card had vanished.
dialog.addEventListener('close', () => {
  editingId = null;
  draft = null;
  sourceId = null;
  setReviewingEditId(null);
  // The dialog goes back to being the card dialog, whatever closed it. Left as
  // it was, the next card opened would show an import's footer over a board
  // card, with no way to save it.
  $('#card-actions').hidden = false;
  $('#review-actions').hidden = true;
  paintReplaces(null);
  dialog.classList.remove('reviewing-swap');
  // Escape, or the backdrop: the reviewer walked away, so the import stops here
  // with everything already accepted left on the board. Read and cleared before
  // the call, because the driver's own end-of-review path closes this dialog.
  const decide = reviewDecide;
  reviewStep = null;
  reviewDecide = null;
  if (decide) decide('stop');
});
