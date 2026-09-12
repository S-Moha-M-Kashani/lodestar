import { catById } from '../core/categories.js';
import { PRIO_TITLE } from '../core/constants.js';
import { filters } from '../core/state.js';
import { rangeText, rangeVal, spanOf } from '../core/timespan.js';
import { $ } from './dom.js';
import { render } from './render.js';

// One button for every way of narrowing the board that is not a life area, and
// one chip for each way that is in force.
//
// The header used to stand seven dropdowns, two date boxes, a read-back and a
// bar of every tag on the board — before a single card was drawn. Five of the
// dropdowns were switched off, and a control at rest that says "All types" is a
// control whose whole visible state is a synonym for its own absence. The row
// says nothing now until something is narrowing the board, and then it says
// exactly that, in words, with an × on each.
//
// **Nothing moved and nothing was renamed.** `#type-filter`, `#prio-filter`,
// `#tag-filter`, `#column-filter`, `#nesting-filter` and the four range
// controls are the same elements with the same ids, wired once at boot by
// js/ui/toolbar.js; the panel is *where they are*, not *what they are*. So this
// module owns two things only: whether the panel is open, and what the chips
// say. Nothing here writes a filter except a chip's own ×, which is the one
// thing the controls themselves cannot offer — a control put back to its
// default is a different gesture from a filter being lifted.

const btn = $('#filter-btn');
const panel = $('#filter-panel');

export function setFilterMenuOpen(open) {
  panel.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
}

btn.addEventListener('click', () => setFilterMenuOpen(panel.hidden));

// A click inside is use — changing three filters must not mean opening the
// panel three times, which is the rule the Menu's own switches already follow.
//
// **Capture phase, and that is load-bearing.** On the bubble phase this handler
// runs *after* the clicked element's own, and a tag chip's own handler calls
// render() — which repaints `#tag-bar` wholesale, so by the time the question
// "did this click come from inside the panel?" was asked, the element it came
// from was no longer in the document and `closest()` answered null. The panel
// then dismissed itself on a press of its own contents, and the second tag was
// unreachable. Capturing asks the question while the target is still attached.
document.addEventListener('click', (e) => {
  if (!panel.hidden && !e.target.closest('.filter-menu')) setFilterMenuOpen(false);
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !panel.hidden) {
    setFilterMenuOpen(false);
    btn.focus();
  }
});

/** What is in force, in words, and how to lift each one.
 *
 *  The wording is read off the control that set it wherever there is one — the
 *  selected `<option>`'s own text — so a chip cannot drift from the menu row it
 *  came from, and an option renamed in index.html renames its chip with it.
 *  The two that have no option to read are the tags (a Set, which no single
 *  option can name) and the time span (four controls saying one thing, which is
 *  what `rangeText` exists for). */
function activeFilters() {
  const out = [];
  const optionText = (sel) => {
    const el = $(sel);
    return el?.selectedOptions?.[0]?.textContent?.trim() || '';
  };

  if (filters.type) out.push({ key: 'type', text: optionText('#type-filter') });
  if (filters.prio) {
    out.push({
      key: 'prio',
      text: optionText('#prio-filter'),
      title: PRIO_TITLE[Number(filters.prio)] || '',
    });
  }
  // One chip per tag, because each is separately liftable: the board asks for
  // cards carrying *all* of them, so dropping one is a real and different
  // question rather than a step back towards no filter at all.
  for (const tag of filters.tags) out.push({ key: `tag:${tag}`, text: `#${tag}` });
  if (spanOf(filters.range) !== null) out.push({ key: 'range', text: rangeText(filters.range) });
  if (filters.column) out.push({ key: 'column', text: optionText('#column-filter') });
  if (filters.nesting) out.push({ key: 'nesting', text: optionText('#nesting-filter') });
  return out;
}

/** Lift one filter, and put the control that set it back to its own default —
 *  or the chip would come back on the next repaint, since the chips are read
 *  off `filters` and the controls are read off nothing at all. */
function clearFilter(key) {
  if (key.startsWith('tag:')) {
    filters.tags.delete(key.slice(4));
    const dropdown = $('#tag-filter');
    if (dropdown) dropdown.value = filters.tags.size === 1 ? [...filters.tags][0] : '';
  } else if (key === 'type') {
    filters.type = '';
    $('#type-filter').value = '';
  } else if (key === 'prio') {
    filters.prio = '';
    $('#prio-filter').value = '';
  } else if (key === 'column') {
    filters.column = '';
    $('#column-filter').value = '';
  } else if (key === 'nesting') {
    // The one filter with a memory: it is remembered between visits, so lifting
    // it has to forget it too, exactly as the dropdown's own handler does.
    filters.nesting = '';
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

/** The chips, repainted with the board. A `Clear` only from two up: with one
 *  chip on the row it is a second control for what the × beside it already
 *  does, in a row whose whole point is that it is empty until it is not. */
export function renderFilterChips() {
  const slot = $('#filter-chips');
  if (!slot) return;
  // A life area that has been deleted out from under the filter, the same
  // guard renderCatRail() applies — the tabs paint the category filter and
  // this row must not be the one place a dead id survives.
  if (filters.category && !catById(filters.category)) filters.category = '';
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
}
