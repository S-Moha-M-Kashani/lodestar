import { COMPASS_KEYS, askingAboutFinished, matchesFilters } from '../core/cards.js';
import { catColor, catById, categories } from '../core/categories.js';
import { COLUMNS, CONTROL_LABEL, TYPES, TYPE_META } from '../core/constants.js';
import { isHabit } from '../core/habits.js';
import { LENSES, lensFilters, lensOf, MULTI_KEYS } from '../core/lenses.js';
import { filters, state, view } from '../core/state.js';
import { RANGE_FIELDS, RANGE_PRESETS, rangeText, rangeVal, spanHint, spanOf } from '../core/timespan.js';
import { $ } from './dom.js';
import { render } from './render.js';

// The filter window behind `+ Filter`, and one chip for each filter in force.
//
// The window is the card dialog's property ledger turned into a question: a
// row per field, and every answer a stamp pressed on or off, so a row holds
// all, some or one of its answers. An empty Set on `filters` is "any". The
// window changes the board as you press (there is no Apply), and its rows are
// repainted in place on every render, so a stamp keeps its focus.
//
// The tag, time and scope rows keep their own controls and ids, wired once at
// boot by js/ui/toolbar.js and js/ui/board.js. This module must not import
// toolbar.js (js/core/history.js's graph reaches this file, and toolbar.js
// builds `<option>`s at evaluation): it writes those controls by setting a
// value and dispatching `change`.

const btn = $('#filter-btn');
const panel = $('#filter-panel');

// --------------------------------------------------------------------------
// What each row offers, in the words the card dialog already uses.

const COMPASS_OPTIONS = {
  importance: [['high', 'important'], ['low', 'not important'], ['', 'not set']],
  urgency: [['high', 'urgent'], ['low', 'not urgent'], ['', 'not set']],
  effort: [['low', 'light'], ['medium', 'medium'], ['high', 'heavy']],
  control: Object.entries(CONTROL_LABEL),
};
const COMPASS_NAMES = { importance: 'Importance', urgency: 'Urgency', effort: 'Effort', control: 'Control' };

const options = {
  column: () => COLUMNS.map((c) => [c.id, c.title]),
  type: () => TYPES.map((t) => [t, `${TYPE_META[t].glyph} ${TYPE_META[t].label}`]),
  category: () => categories.map((c) => [c.id, c.label]),
  // Keyed off the table above, not COMPASS_KEYS: cards.js may not have
  // finished evaluating when this module does (the graph has cycles).
  ...Object.fromEntries(Object.keys(COMPASS_OPTIONS).map((k) => [k, () => COMPASS_OPTIONS[k]])),
};

const optionLabel = (facet, value) => (options[facet]().find(([v]) => v === value) || [value, value])[1];

// The body class each Settings switch sets to take a column off the Board.
const HIDDEN_BY = { 'inbox': 'hide-inbox-col', 'on-hold': 'hide-hold-col', 'answered': 'hide-done-col' };

/** The columns an empty Where row stands for, on this view: every column the
 *  Board paints, or the open ones (and Done while a finished span is asked)
 *  everywhere else. What the row shows pressed before anything is chosen. */
function defaultColumns() {
  if (view === 'board') {
    const body = document.body.classList;
    return COLUMNS.map((c) => c.id).filter((id) => !body.contains(HIDDEN_BY[id]));
  }
  const open = ['inbox', 'on-hold', 'in-progress'];
  return askingAboutFinished() ? [...open, 'answered'] : open;
}

const sameSet = (a, b) => a.size === b.size && [...a].every((v) => b.has(v));

/** Press one Where stamp. The row starts from what it shows, so the first
 *  press un-ticks one column rather than narrowing to it; ticking back to the
 *  default empties the Set again, and the last column cannot be un-ticked. */
function toggleColumn(id) {
  const next = new Set(filters.column.size ? filters.column : defaultColumns());
  if (next.has(id)) next.delete(id); else next.add(id);
  if (!next.size) return;
  filters.column.clear();
  if (!sameSet(next, new Set(defaultColumns()))) for (const v of next) filters.column.add(v);
  render();
}

function toggle(facet, value) {
  const set = filters[facet];
  if (set.has(value)) set.delete(value); else set.add(value);
  render();
}

// --------------------------------------------------------------------------
// Painting the rows

/** Fill a row's stamps, rebuilding only when the answers themselves changed
 *  (a category added, say) and otherwise updating `aria-pressed` in place.
 *  `hint`, when given, writes a small second line under a stamp's name. */
function paintStamps(box, list, isOn, onPress, cls, hint = null) {
  const keys = list.map(([v]) => v).join('\u0000');
  if (box.dataset.keys !== keys) {
    box.dataset.keys = keys;
    box.replaceChildren(...list.map(([value, label]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = cls;
      b.dataset.value = value;
      b.textContent = label;
      b.addEventListener('click', () => onPress(value));
      return b;
    }));
  }
  for (const b of box.children) {
    b.setAttribute('aria-pressed', String(isOn(b.dataset.value)));
    if (hint) {
      let small = b.querySelector('.filter-hint');
      if (!small) { small = document.createElement('small'); small.className = 'filter-hint'; b.append(small); }
      small.textContent = hint(b.dataset.value);
    }
    if (cls === 'filter-pill') b.style.setProperty('--cat', catColor(b.dataset.value));
  }
}

/** The small "any" link under a row's name, shown only while the row narrows. */
function paintRowClear(rowId, text, active, onClear) {
  const label = $(`#${rowId} .filter-label`);
  if (!label) return;
  let link = label.querySelector('.filter-row-clear');
  if (!link) {
    link = document.createElement('button');
    link.type = 'button';
    link.className = 'filter-row-clear';
    link.addEventListener('click', (e) => { e.preventDefault(); onClear(); });
    label.append(link);
  }
  link.textContent = text;
  link.hidden = !active;
}

const boardCards = () => state.cards.filter((c) => !isHabit(c));

function renderPanel() {
  const shownCols = filters.column.size ? filters.column : new Set(defaultColumns());
  paintStamps($('#filter-row-column .filter-opts'), options.column(),
    (v) => shownCols.has(v), toggleColumn, 'filter-stamp');
  paintRowClear('filter-row-column', 'reset', filters.column.size > 0, () => clearFilter('column'));

  paintStamps($('#filter-row-type .filter-opts'), options.type(),
    (v) => filters.type.has(v), (v) => toggle('type', v), 'filter-stamp');
  paintRowClear('filter-row-type', 'any type', filters.type.size > 0, () => clearFilter('type'));

  paintStamps($('#filter-row-category .filter-opts'), options.category(),
    (v) => filters.category.has(v), (v) => toggle('category', v), 'filter-pill');
  paintRowClear('filter-row-category', 'any area', filters.category.size > 0, () => clearFilter('category'));

  for (const k of COMPASS_KEYS) {
    paintStamps($(`#filter-row-compass .filter-opts[data-facet="${k}"]`), options[k](),
      (v) => filters[k].has(v), (v) => toggle(k, v), 'filter-stamp filter-stamp-soft');
  }
  paintRowClear('filter-row-compass', 'any', COMPASS_KEYS.some((k) => filters[k].size), () => {
    for (const k of COMPASS_KEYS) filters[k].clear();
    render();
  });

  paintRowClear('filter-row-tags', 'no tags', filters.tags.size > 0, () => {
    filters.tags.clear();
    render();
  });
  for (const b of panel.querySelectorAll('#tag-mode [data-mode]')) {
    b.setAttribute('aria-checked', String(b.dataset.mode === filters.tagMode));
  }
  // The chosen tags read as the sentence they make: "#a and #b".
  const said = $('#tag-readback');
  said.hidden = !filters.tags.size;
  said.textContent = [...filters.tags].map((t) => `#${t}`)
    .join(filters.tagMode === 'any' ? ' or ' : ' and ');

  // When: the date, then the span, each pressed through its own select so
  // toolbar.js stays the one writer of the stored span.
  const press = (sel, v) => {
    $(sel).value = v;
    $(sel).dispatchEvent(new Event('change'));
  };
  paintStamps($('#filter-row-range .filter-opts[data-facet="range-field"]'),
    RANGE_FIELDS.map((f) => [f.id, f.label]),
    (v) => v === filters.range.field, (v) => press('#range-field', v), 'filter-stamp filter-stamp-soft');
  paintStamps($('#filter-row-range .filter-opts[data-facet="range-preset"]'),
    RANGE_PRESETS.map((p) => [p.id, p.label]),
    (v) => v === filters.range.preset, (v) => press('#range-preset', v), 'filter-stamp filter-stamp-soft',
    (v) => spanHint({ ...filters.range, preset: v }) || (v === 'custom' ? 'pick days' : 'no limit'));
  paintRowClear('filter-row-range', 'any time', spanOf(filters.range) !== null, () => clearFilter('range'));

  const nesting = $('#nesting-filter');
  paintStamps($('#filter-row-nesting .filter-opts'), [...nesting.options].map((o) => [o.value, o.textContent.trim()]),
    (v) => v === filters.nesting, (v) => {
      nesting.value = v;
      nesting.dispatchEvent(new Event('change'));
    }, 'filter-stamp filter-stamp-soft');

  const all = boardCards();
  const n = all.filter(matchesFilters).length;
  $('#filter-count').textContent = `${n} of ${all.length} cards`;
  $('#filter-done').textContent = n ? `Show ${n} card${n === 1 ? '' : 's'}` : 'No cards match';
  $('#filter-clear-all').disabled = !activeFilters().length && !filters.category.size;

}

for (const b of panel.querySelectorAll('#tag-mode [data-mode]')) {
  b.addEventListener('click', () => {
    filters.tagMode = b.dataset.mode;
    render();
  });
}

export function setFilterMenuOpen(open) {
  panel.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  // The Ask pill floats over the window's foot otherwise.
  document.body.classList.toggle('filter-open', open);
  if (!open) return;
  renderPanel();
  // Fixed, placed from the button's rectangle and kept inside the window, so
  // a button near the right edge does not push the panel off screen. The
  // phone's stylesheet overrides all three with a sheet against the bottom.
  const r = btn.getBoundingClientRect();
  const top = r.bottom + 6;
  panel.style.setProperty('--fp-top', `${top}px`);
  panel.style.setProperty('--fp-left', `${Math.max(16, Math.min(r.left, innerWidth - panel.offsetWidth - 16))}px`);
  panel.style.setProperty('--fp-max-h', `${innerHeight - top - 16}px`);
}

btn.addEventListener('click', () => setFilterMenuOpen(panel.hidden));
$('#filter-done').addEventListener('click', () => {
  setFilterMenuOpen(false);
  btn.focus();
});
$('#filter-clear-all').addEventListener('click', () => {
  filters.category.clear();
  filters.tagMode = 'all';
  applyLens('open');
});

// A click inside is use, so changing three filters does not mean opening the
// window three times. **Capture phase, and that is load-bearing**: a tag
// chip's own handler repaints the tag bar, so on the bubble phase the clicked
// element is already detached and `closest()` would call it outside.
document.addEventListener('click', (e) => {
  if (!panel.hidden && !e.target.closest('.filter-menu')) setFilterMenuOpen(false);
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || panel.hidden) return;
  setFilterMenuOpen(false);
  btn.focus();
});

// --------------------------------------------------------------------------
// The lens row
//
// A lens writes the same filters the window writes. Which one is pressed is
// read back off the filters by `lensOf` every time, never stored, so a stamp
// pressed by hand un-presses the row.

const lensRow = $('#lens-row');

/** Press a lens: every field it names, then the time and scope controls,
 *  which are written through their own handlers (both are remembered between
 *  visits, and toolbar.js owns writing them down). The range dispatch is also
 *  what repaints, so nothing here calls render() itself. */
export function applyLens(id) {
  const next = lensFilters(id);
  if (!next) return;
  for (const k of MULTI_KEYS) {
    filters[k].clear();
    for (const v of next[k]) filters[k].add(v);
  }
  const nesting = $('#nesting-filter');
  if (nesting.value !== next.nesting) {
    nesting.value = next.nesting;
    nesting.dispatchEvent(new Event('change'));
  }
  filters.range = rangeVal(next.range);
  $('#range-field').value = next.range.field;
  $('#range-from').value = next.range.from;
  $('#range-to').value = next.range.to;
  // Last, and the one that lands: toolbar.js's handler re-reads the whole
  // range off `filters`, and brings the remembered span, the read-back and the
  // repaint with it.
  $('#range-preset').value = next.range.preset;
  $('#range-preset').dispatchEvent(new Event('change'));
}

// Once, on the header row: the window does not repeat them.
for (const lens of LENSES) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'lens-btn';
  b.dataset.lens = lens.id;
  b.textContent = lens.label;
  b.title = lens.hint;
  b.setAttribute('aria-pressed', 'false');
  b.addEventListener('click', () => applyLens(lens.id));
  lensRow?.append(b);
}

/** Which lens the board is showing, repainted with it. */
export function renderLensRow() {
  if (!lensRow) return;
  const pressed = lensOf(filters);
  for (const b of lensRow.querySelectorAll('.lens-btn')) {
    b.setAttribute('aria-pressed', String(b.dataset.lens === pressed));
  }
}

// --------------------------------------------------------------------------
// The chips

/** What is in force, in words, and how to lift each one. A pressed lens is one
 *  chip saying the lens, not one per value it wrote. The life area has no chip:
 *  its tabs already say it. */
function activeFilters() {
  const lens = lensOf(filters);
  if (lens) {
    const found = LENSES.find((l) => l.id === lens);
    return found.id === 'open' ? [] : [{ key: 'lens', text: found.label, title: found.hint }];
  }
  const out = [];
  const list = (facet) => [...filters[facet]].map((v) => optionLabel(facet, v)).join(', ');
  if (filters.column.size) out.push({ key: 'column', text: `Where: ${list('column')}` });
  if (filters.type.size) out.push({ key: 'type', text: `Type: ${list('type')}` });
  for (const k of COMPASS_KEYS) {
    if (filters[k].size) out.push({ key: k, text: `${COMPASS_NAMES[k]}: ${list(k)}` });
  }
  // One chip per tag, because each is separately liftable.
  for (const tag of filters.tags) out.push({ key: `tag:${tag}`, text: `#${tag}` });
  if (spanOf(filters.range) !== null) out.push({ key: 'range', text: rangeText(filters.range) });
  if (filters.nesting) {
    out.push({ key: 'nesting', text: $('#nesting-filter').selectedOptions[0].textContent.trim() });
  }
  return out;
}

/** Lift one filter, and put the control that set it back to its default. */
function clearFilter(key) {
  if (key === 'lens') { applyLens('open'); return; }
  if (key.startsWith('tag:')) {
    filters.tags.delete(key.slice(4));
  } else if (filters[key] instanceof Set) {
    filters[key].clear();
  } else if (key === 'nesting') {
    // Remembered between visits, so lifted through its own handler.
    $('#nesting-filter').value = '';
    $('#nesting-filter').dispatchEvent(new Event('change'));
    return;
  } else if (key === 'range') {
    filters.range = rangeVal(null);
    $('#range-preset').value = '';
    $('#range-preset').dispatchEvent(new Event('change'));
    return;
  }
  render();
}

function chip({ key, text, title }) {
  const el = document.createElement('span');
  el.className = 'filter-chip';
  el.dataset.filter = key;
  const label = document.createElement('span');
  label.textContent = text;
  if (title) label.title = title;
  const x = document.createElement('button');
  x.type = 'button';
  x.className = 'filter-chip-x';
  x.textContent = '×';
  x.setAttribute('aria-label', `Stop filtering by ${text}`);
  x.addEventListener('click', () => clearFilter(key));
  el.append(label, x);
  return el;
}

/** The chips, repainted with the board, and the window with them.
 *  A `Clear` only from two chips up. */
export function renderFilterChips() {
  const slot = $('#filter-chips');
  if (!slot) return;
  // A life area deleted out from under the filter, the same guard
  // renderCatRail() applies.
  for (const id of [...filters.category]) if (!catById(id)) filters.category.delete(id);
  // The phone's Filters toggle names the life area, because the tabs that
  // would otherwise say so are what it hides.
  const toggleBtn = $('#board-tools-toggle');
  if (toggleBtn) {
    const areas = [...filters.category].map((id) => catById(id).label).join(', ');
    toggleBtn.textContent = areas ? `Filters · ${areas}` : 'Filters';
  }
  slot.replaceChildren();

  const active = activeFilters();
  for (const f of active) slot.append(chip(f));

  if (active.length >= 2) {
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.id = 'clear-filters';
    clear.className = 'clear-filters';
    clear.textContent = 'Clear';
    clear.addEventListener('click', () => {
      for (const f of active) clearFilter(f.key);
      render();
    });
    slot.append(clear);
  }
  btn.textContent = active.length ? `+ Filter · ${active.length}` : '+ Filter';
  // Shut or open, so the window never holds a stale press for the next look.
  renderPanel();
}
