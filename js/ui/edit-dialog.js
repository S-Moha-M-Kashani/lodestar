import { cardLabel, catVal, columnAccepts, controlVal, deadlineVal, effortVal, iuVal, matchesFilters, placeCard, typeVal, uid } from '../core/cards.js';
import { catColor, categories } from '../core/categories.js';
import { COLUMNS, TYPES, TYPE_META } from '../core/constants.js';
import { blankDraft, cardHasDetails, draftFrom } from '../core/draft.js';
import { habitCountVal, habitFreqVal, habitTimesVal } from '../core/habits.js';
import { planConflict, planSrcVal, planVal } from '../core/plan.js';
import { commit, short } from '../core/history.js';
import { filters, nextNum, state } from '../core/state.js';
import { deleteCard } from './card-actions.js';
import { $, announce, columnTitle, getCard } from './dom.js';
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
  $('#card-habit-times').value = shown.habitTimes.join(', ');
  planSrcNow = planSrcVal(shown.planSrc);
  paintPlan(shown.plan);
  syncHabitFields();
  // The details fold: derived here on every open and stored nowhere, so it
  // cannot go stale against the card in front of it. A suggested edit forces it
  // open — a change to a folded setting must never be approved unseen.
  $('#card-details').open = cardHasDetails(shown) || Boolean(reviewingEditId);
}

// `suggested` prefills the form with an Assistant suggestion instead of what
// the card currently says. It fills the *form*, deliberately, and not the card:
// the values are a draft the user can change or abandon, and until they submit
// nothing has happened to the board.
export function openDialog(cardId, suggested = null) {
  const card = getCard(cardId);
  if (!card) return;
  editingId = cardId;
  const shown = suggested ? { ...card, ...suggested } : card;
  paintMode(false);
  paintForm(shown);
  const fmt = (ts) => new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  $('#card-meta').textContent =
    `${cardLabel(card)} · in ${columnTitle(card.columnId)} · added ${fmt(card.createdAt)} · updated ${fmt(card.updatedAt)}`;
  dialog.showModal();
  $('#card-title').focus();
}

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
export function openNewCard(source = null) {
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
  paintMode(true);
  paintForm(draft);
  dialog.showModal();
  $('#card-title').focus();
}

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
    card.habitTimes = readHabitTimes(card.habitCount);
  }
  // Where it is filed. Refused rather than written when the card cannot be
  // there, so this control can never put a habit in In Progress — the type is
  // read above, so `card` already carries the stamp being saved.
  const column = form.elements.column.value;
  if (COLUMNS.some((c) => c.id === column) && columnAccepts(card, column)) card.columnId = column;
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
  // error stands under the fieldset, and the focus goes to the field that can
  // fix it — but the plan fieldset now lives inside the details fold, and
  // focus() on an element inside a closed <details> does nothing, so from a
  // collapsed dialog this refusal showed no message and moved no caret: Save
  // appeared to do nothing at all. The fold is opened first. And the type is
  // read off the draft as well as the card, because a guard written against
  // `card` alone skipped the check entirely for every new card.
  if (subject.type !== 'habit' && planConflict(readPlan(), deadlineVal($('#card-deadline').value))) {
    $('#card-details').open = true;
    syncPlanFields();
    announce($('#card-plan-error').textContent);
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
});
