import { WEEKDAYS, monthGrid, pressDay } from '../core/calendar.js';
import { $ } from './dom.js';

// The calendar behind `Between…`: one month at a time, pressed twice.
//
// It replaces two `<input type="date">`s, which asked the reader to know a date
// rather than to find a day and which could hold a contradiction — a start
// after its end — that `spanOf` could only answer as the empty span it is. The
// rule itself is js/core/calendar.js, which imports nothing and is pinned under
// plain node; this file is the DOM half and holds no date maths of its own.
//
// **It writes the board's own two values, through their own `change` events.**
// `#range-from` and `#range-to` are the same inputs they have always been —
// they carry no calendar of their own now, so they are `type="hidden"` — and
// js/ui/toolbar.js's `setRange` is still the single writer of the stored span,
// the read-back sentence and the repaint. From first, then to: toolbar's
// handler takes the changed field off the event and the rest off `filters`, so
// the order is what carries both halves of one press.
//
// **And a half-chosen span never narrows the board.** What the grid is showing
// is a draft held here; nothing reaches `#range-from` until the span is settled
// — either a second day pressed, or one end explicitly left open. A board that
// repainted between the two presses would move the thing being chosen under the
// hand choosing it.

// What the grid is showing, and which month it is showing it in. Not the
// filter: `filters.range` is written only when a span is settled.
let draft = { from: '', to: '' };
let month = '';
// The day the pointer or the caret is on, so the grid can show the span a press
// would give before it is given.
let preview = '';

/** Settle the span: the board's two values, through their own handlers.
 *
 *  Two dispatches and not one, and in this order, because toolbar.js's handler
 *  reads the changed field off the event and every other field off `filters` —
 *  so `from` has to have landed before `to` is announced. `applyLens` writes the
 *  range the same way and for the same reason. */
function settle(next) {
  const from = next.from || '';
  const to = next.to || '';
  draft = { from, to };
  // Both values written BEFORE either is announced, and both read out of
  // locals rather than off `draft`. The first dispatch repaints the whole
  // control — `setRange` calls `syncRangeControls`, which calls this module
  // back — so a `draft` read after it is a draft that render has already
  // re-synced from the fields, and the second half of the span was being
  // dropped between the two lines that wrote it.
  const start = $('#range-from');
  const end = $('#range-to');
  start.value = from;
  end.value = to;
  start.dispatchEvent(new Event('change'));
  end.dispatchEvent(new Event('change'));
}

function press(day) {
  const next = pressDay(draft, day);
  // A start with no end is a span half said. Held here until the reader
  // finishes the sentence, so the board stays still while they choose.
  if (next.from && !next.to) {
    draft = next;
    preview = '';
    renderDateRange();
    focusDay(day);
    return;
  }
  settle(next);
  focusDay(day);
}

function focusDay(day) {
  $(`#range-calendar .range-day[data-day="${day}"]`)?.focus();
}

/** Which days a press would leave inside the span, so the reader can see the
 *  answer before committing to it. While the span is half said this is the run
 *  between the start and whatever the pointer is on; once it is settled it is
 *  the span itself. */
function inSpan(day) {
  const from = draft.from;
  const to = draft.to || (from && preview && preview > from ? preview : '');
  if (!from || !to) return day && day === from;
  return day >= from && day <= to;
}

/** Repaint which days the span covers, in place. */
function paintPreview() {
  for (const el of document.querySelectorAll('#range-calendar .range-day')) {
    el.classList.toggle('within', inSpan(el.dataset.day));
  }
}

/** Paint the calendar into its slot. Called by `syncRangeControls` in
 *  js/ui/toolbar.js, which owns when the span control is on show at all. */
export function renderDateRange() {
  const host = $('#range-calendar');
  if (!host || host.hidden) return;

  // The draft follows the filter whenever the filter was set from elsewhere —
  // a lens, a chip's ×, a value that came back out of storage — so the grid
  // never shows a span the board is not applying. A half-said span is the one
  // state the filter cannot hold, and it is exactly the state held here.
  const fieldFrom = $('#range-from').value;
  const fieldTo = $('#range-to').value;
  if (!(draft.from && !draft.to) && (fieldFrom !== draft.from || fieldTo !== draft.to)) {
    draft = { from: fieldFrom, to: fieldTo };
  }

  const grid = monthGrid(month || draft.from || draft.to || '');
  month = `${grid.year}-${String(grid.month).padStart(2, '0')}-01`;
  // Which month is on show, on the element itself. The grid's first cell is
  // usually a day of the month before, so nothing outside can read the month
  // off the days — and a check that tried would step back and forth between
  // two months for ever without reaching the one it wanted.
  host.dataset.month = month.slice(0, 7);
  host.replaceChildren();

  const head = document.createElement('div');
  head.className = 'range-cal-head';
  for (const [dir, label, to] of [['prev', '‹', grid.prev], ['next', '›', grid.next]]) {
    const step = document.createElement('button');
    step.type = 'button';
    step.className = `range-cal-step range-cal-${dir}`;
    step.textContent = label;
    step.setAttribute('aria-label', dir === 'prev' ? 'The month before' : 'The month after');
    step.addEventListener('click', () => { month = to; renderDateRange(); });
    head.append(step);
  }
  const name = document.createElement('span');
  name.className = 'range-cal-month';
  name.textContent = grid.label;
  head.insertBefore(name, head.lastChild);
  host.append(head);

  const names = document.createElement('div');
  names.className = 'range-cal-week';
  // aria-hidden: the day buttons each carry their own full date, so reading
  // seven initials before them is a header said twice.
  names.setAttribute('aria-hidden', 'true');
  for (const day of WEEKDAYS) {
    const cell = document.createElement('span');
    cell.textContent = day.slice(0, 1);
    names.append(cell);
  }
  host.append(names);

  const days = document.createElement('div');
  days.className = 'range-cal-grid';
  days.setAttribute('role', 'group');
  days.setAttribute('aria-label', 'Choose a span: the first day pressed starts it, the second ends it');
  for (const cell of grid.days) {
    const day = document.createElement('button');
    day.type = 'button';
    day.className = 'range-day';
    day.dataset.day = cell.iso;
    day.textContent = String(cell.day);
    if (!cell.inMonth) day.classList.add('outside');
    if (cell.iso === draft.from) day.classList.add('start');
    if (cell.iso === draft.to) day.classList.add('end');
    if (inSpan(cell.iso)) day.classList.add('within');
    day.setAttribute('aria-pressed', String(cell.iso === draft.from || cell.iso === draft.to));
    day.title = cell.iso;
    day.setAttribute('aria-label', cell.iso);
    day.addEventListener('click', () => press(cell.iso));
    // The span a press would give, shown before it is given — on hover and on
    // keyboard focus alike, because a preview only a pointer can see is a
    // control that answers one of its two users.
    //
    // It repaints the classes and never rebuilds the grid, which is not an
    // optimisation: rebuilding replaced the very button the pointer was over
    // and the caret was in, so the focus this hands back fired `focus` on a new
    // element, which previewed again — "Maximum call stack size exceeded",
    // measured, with every press after the first one lost inside it.
    const show = () => {
      if (!(draft.from && !draft.to) || preview === cell.iso) return;
      preview = cell.iso;
      paintPreview();
    };
    day.addEventListener('mouseenter', show);
    day.addEventListener('focus', show);
    days.append(day);
  }
  host.append(days);

  // The two ways to leave an end open, so "since the first of March" and "up to
  // the first of March" stay sayable on a control that otherwise wants two
  // days. Each settles the span, which is what tells them apart from the half
  // said state above: an end nobody has chosen yet is not an open end.
  const open = document.createElement('div');
  open.className = 'range-cal-open';
  const chosen = draft.from || draft.to;
  for (const [label, hint, make] of [
    ['Open start', 'Every card up to the day chosen', () => ({ from: '', to: draft.to || draft.from })],
    ['Open end', 'Every card from the day chosen onwards', () => ({ from: draft.from || draft.to, to: '' })],
  ]) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'range-cal-openend';
    btn.textContent = label;
    btn.title = hint;
    btn.disabled = !chosen;
    btn.addEventListener('click', () => { settle(make()); renderDateRange(); });
    open.append(btn);
  }
  host.append(open);
}
