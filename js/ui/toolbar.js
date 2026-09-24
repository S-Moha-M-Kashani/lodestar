import { closeRecall } from '../assistant/recall.js';
import { assistantState, refreshChatSessions, refreshChatTrash, startNewChat } from '../assistant/session.js';
import { armHistoryIdle, cancelHistoryIdle, closeChatHistory, closeChatRowMenus, extrasOpen, fromChatRowMenu, renderAssistantTools, setChatMenuOpen, setExtrasOpen } from '../assistant/tools.js';
import { widgetShowing } from '../assistant/shell.js';
import { closeWidget, toggleWidget } from '../assistant/widget.js';
import { planDedupe } from '../core/duplicates.js';
import { commit } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { filters, setCards, state } from '../core/state.js';
import { RANGE_FIELDS, RANGE_PRESETS, rangeText, rangeVal, spanOf } from '../core/timespan.js';
import { renderTagBar, setTagQuery } from './board.js';
import { renderDateRange } from './date-range.js';
import { ask } from './dialogs.js';
import { $, announce } from './dom.js';
import { render } from './render.js';

// Toolbar: the type and priority filters, the actions menu and its Settings
// dialog, the Assistant's
// header controls, and the keyboard shortcuts that reach them.
//
// The text search is not wired here. It is painted into the board beside
// `+ New card` (js/ui/board.js) and so is rebuilt on every repaint, which is
// the one control whose listener cannot be attached once at boot.

$('#type-filter').addEventListener('change', (e) => {
  filters.type = e.target.value;
  render();
});

$('#prio-filter').addEventListener('change', (e) => {
  filters.prio = e.target.value;
  render();
});

// Where the card is filed. Painted on every view but the Board, where it would
// be a control for emptying two of the three columns you are looking at.
$('#column-filter').addEventListener('change', (e) => {
  filters.column = e.target.value;
  render();
});

// How deep to read: parents only, or parents and children. Wired here beside
// the other filters and deliberately NOT through `displaySwitch`, which is for
// the Menu's switches — this is a filter with a dropdown of its own, painted on
// every view including the Board. The stored value is applied before any
// listener runs and WITHOUT calling render(): this module is still evaluating,
// and a render() from here is the "Cannot access before initialization" the
// module-graph notes are about. main.js's first render picks it up.
const NESTING_KEY = KEY_PREFIX + 'nesting';
try {
  const stored = localStorage.getItem(NESTING_KEY);
  if (stored === 'all') { filters.nesting = 'all'; $('#nesting-filter').value = 'all'; }
} catch (_) { /* private mode */ }
$('#nesting-filter').addEventListener('change', (e) => {
  filters.nesting = e.target.value === 'all' ? 'all' : '';
  try {
    if (filters.nesting) localStorage.setItem(NESTING_KEY, filters.nesting);
    else localStorage.removeItem(NESTING_KEY);
  } catch (_) { /* private mode */ }
  render();
});

// What a span of time held: which date on the card (`Captured` / `Finished`)
// and which span. Wired here beside the other filters and deliberately NOT
// through `displaySwitch`, which is for the Menu's switches — this is a filter
// with dropdowns of its own, painted on every view including the Board.
//
// Remembered between visits, the way NESTING_KEY is, and applied the same way:
// before any listener runs and WITHOUT calling render(). This module is still
// evaluating at that point and a render() from here is the "Cannot access
// before initialization" the module-graph notes are about; main.js's first
// render picks it up. What comes back out of storage goes through `rangeVal`,
// so a value written by an older build — or edited by hand — is discarded
// rather than trusted: a preset nothing can name would narrow the board by a
// rule nothing on screen explains.
const RANGE_KEY = KEY_PREFIX + 'range';
for (const { id, label } of RANGE_FIELDS) {
  $('#range-field').append(new Option(label, id));
}
for (const { id, label } of RANGE_PRESETS) {
  $('#range-preset').append(new Option(label, id));
}

/** The calendar is shown only for `Between…`, and the read-back only while the
 *  filter is actually narrowing something — "Any time" is already what the
 *  preset select says, so printing it twice would be the one thing a read-back
 *  must not do.
 *
 *  `#range-from` and `#range-to` are hidden inputs now: the calendar
 *  (js/ui/date-range.js) is what a reader presses, and those two are still the
 *  values it writes and everything else reads. */
function syncRangeControls() {
  const custom = filters.range.preset === 'custom';
  const calendar = $('#range-calendar');
  calendar.hidden = !custom;
  renderDateRange();
  const active = spanOf(filters.range) !== null;
  const back = $('#range-readback');
  back.hidden = !active;
  back.textContent = active ? rangeText(filters.range) : '';
}

/** One writer for all four controls, so the stored value, the read-back and the
 *  repaint cannot be updated by one handler and forgotten by another. The lens
 *  row reaches it the way every other filter does, by dispatching `change` on
 *  the control — see the note over `applyLens` for why it may not import this
 *  module. */
function setRange(next) {
  filters.range = rangeVal(next);
  try {
    if (spanOf(filters.range) !== null) localStorage.setItem(RANGE_KEY, JSON.stringify(filters.range));
    else localStorage.removeItem(RANGE_KEY);
  } catch (_) { /* private mode */ }
  syncRangeControls();
  render();
}

try {
  const stored = localStorage.getItem(RANGE_KEY);
  if (stored) {
    filters.range = rangeVal(JSON.parse(stored));
    $('#range-field').value = filters.range.field;
    $('#range-preset').value = filters.range.preset;
    $('#range-from').value = filters.range.from;
    $('#range-to').value = filters.range.to;
  }
} catch (_) { /* private mode, or a stored value that is not JSON */ }
syncRangeControls();

for (const [sel, key] of [['#range-field', 'field'], ['#range-preset', 'preset'],
  ['#range-from', 'from'], ['#range-to', 'to']]) {
  $(sel).addEventListener('change', (e) => setRange({ ...filters.range, [key]: e.target.value }));
}

// The tags dropdown: one tag or all of them. It writes the same Set the #
// bar toggles, so the two stay one filter with two handles — picking here
// replaces whatever combination the bar had built, and the empty option
// clears it.
$('#tag-filter').addEventListener('change', (e) => {
  filters.tags.clear();
  if (e.target.value) filters.tags.add(e.target.value);
  render();
});

// The tag search narrows which tags are LISTED here, never which cards the
// board shows — so this repaints only the bar and the dropdown (renderTagBar,
// which already owns both), never the whole board via render(). A `search`
// input's native × fires `input` too, so one listener covers typing and
// clearing alike.
$('#tag-search').addEventListener('input', (e) => {
  setTagQuery(e.target.value);
  renderTagBar();
});

// One Menu button holds the board actions plus History / Export / Import —
// eight rows now that every standing preference (theme, Plan layout, habit
// chime, which filter controls show) moved into its own Settings dialog. That
// leaves every row a one-shot action, so the panel dismisses after any of
// them rather than staying open for a toggle to be watched flipping.
const menuBtn = $('#menu-btn');
const menuPanel = $('#menu-panel');

function setMenuOpen(open) {
  menuPanel.hidden = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
}

menuBtn.addEventListener('click', () => setMenuOpen(menuPanel.hidden));
menuPanel.addEventListener('click', (e) => {
  if (e.target.closest('button')) setMenuOpen(false);
});
document.addEventListener('click', (e) => {
  if (!menuPanel.hidden && !e.target.closest('.toolbar-menu')) setMenuOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !menuPanel.hidden) {
    setMenuOpen(false);
    menuBtn.focus();
  }
});

// The view row's own dropdown, beside the ⚙ menu and on the same one rule:
// a click outside dismisses, a click inside is use, Escape closes and hands the
// focus back, one `aria-expanded` on the button. Three dropdowns, one rule —
// the 2026-09-03 lesson, applied a fourth time rather than reimplemented.
//
// The three views inside it are static markup with listeners of their own
// (js/ui/render.js), so nothing here switches a view: this owns whether the
// panel is open and where it hangs, and nothing else.
const lensesBtn = $('#lenses-btn');
const lensesPanel = $('#lenses-panel');

/** Open or shut, and — when opening — place the panel under the button.
 *
 *  The panel is `position: fixed`, so its coordinates have to be given to it:
 *  .view-switch clips its own overflow in order to scroll sideways on a phone,
 *  and an absolutely positioned child of a clipping box is a menu nobody can
 *  see. Measured on open rather than tracked, because the header does not move
 *  while a dropdown is down. */
function setLensesOpen(open) {
  lensesPanel.hidden = !open;
  lensesBtn.setAttribute('aria-expanded', String(open));
  if (!open) return;
  const at = lensesBtn.getBoundingClientRect();
  lensesPanel.style.top = `${Math.round(at.bottom + 6)}px`;
  lensesPanel.style.left = `${Math.round(at.left)}px`;
  lensesPanel.style.minWidth = `${Math.round(at.width)}px`;
}

lensesBtn.addEventListener('click', () => setLensesOpen(lensesPanel.hidden));
// Choosing a view is a move to it, so the panel goes with the press — the ⚙
// menu's rule, for the same reason: every row in here is a one-shot action.
lensesPanel.addEventListener('click', (e) => {
  if (e.target.closest('button')) setLensesOpen(false);
});
document.addEventListener('click', (e) => {
  if (!lensesPanel.hidden && !e.target.closest('.view-lenses')) setLensesOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !lensesPanel.hidden) {
    setLensesOpen(false);
    lensesBtn.focus();
  }
});

// Settings…, opened from the Menu: the theme, the rail's Plan layout, the
// habit chime and which filter controls show, in their own dialog now that
// none of them is a one-shot action. A <dialog> handles Escape itself, so
// there is nothing else to wire beyond opening it and the Done button.
$('#settings-btn').addEventListener('click', () => $('#settings-dialog').showModal());
$('#close-settings').addEventListener('click', () => $('#settings-dialog').close());

// Remove duplicates — the Menu's one card action.
//
// A duplicate is a card whose words another card already holds (js/core/
// duplicates.js decides all of it); the copies that lose go to the Trash
// through the ordinary whole-board save, whose existing sweep is what
// soft-deletes the cards a save omits. No new route, no second deletion path,
// and one press of undo puts the whole removal back.
//
// Nothing else on the board is allowed to change with them: a card hanging off
// a retired copy is re-pointed at the survivor, which is the "no cascade
// delete" promise reaching this door — applyStep already keeps it at the
// import's. What a retired habit recorded rides across too, and that half is
// the leaf's, so the union is decided where it can be tested without a DOM.
$('#dedupe-btn').addEventListener('click', async () => {
  const groups = planDedupe(state.cards);
  const gone = groups.reduce((n, g) => n + g.drop.length, 0);
  if (!gone) { announce('No duplicate cards on this board'); return; }
  const label = `${gone} duplicate card${gone === 1 ? '' : 's'}`;

  const ok = await ask({
    title: `Remove ${label}?`,
    message: 'These cards hold words another card on this board already holds. The copy that is furthest along '
      + '— Done, then In Progress, then the Inbox — is the one kept; the rest go to the Trash, where they stay '
      + 'restorable, and anything hanging off a retired copy moves to the copy that is kept.',
    okLabel: 'Remove duplicates',
    danger: true,
  });
  if (!ok) return;

  // Where each retired card's children have to end up, read before the cards
  // are dropped: the survivor may itself be a card whose habit history was
  // just merged, so the id is what travels, never the object.
  const survivorOf = new Map();
  for (const g of groups) for (const c of g.drop) survivorOf.set(c.id, g.keep.id);
  const kept = new Map(groups.map((g) => [g.keep.id, g.keep]));

  setCards(state.cards
    .filter((c) => !survivorOf.has(c.id))
    .map((c) => {
      const card = kept.get(c.id) || c;
      const parentId = survivorOf.get(card.parentId);
      return parentId ? { ...card, parentId } : card;
    }));
  commit(`Removed ${label}`);
  announce(`Removed ${label} — in the Trash, and one undo brings them back`);
});

// The Settings dialog's Show switches. Same idiom as #habit-mute: hidden stores '1',
// shown removes the key outright, and boot applies whatever is stored so a
// reload keeps the choice.
/** One switch in the Show row: a body class the CSS reads, a remembered
 *  flag, and an `aria-pressed` that announces the state ("true" = hidden).
 *
 *  `onSet` is for a switch that changes *which cards are shown* rather than how
 *  they are painted, and it is what tells the two kinds apart: the four paint
 *  toggles pass none and need no repaint, and the one that passes one gets a
 *  render on every click. Never on the boot pass, which only records the stored
 *  value — this module is still evaluating, and calling render() from here is
 *  the "Cannot access before initialization" the module-graph notes are about.
 */
function displaySwitch(btnId, cls, key, onSet = null) {
  const btn = $(btnId);
  const apply = (hidden) => {
    document.body.classList.toggle(cls, hidden);
    btn.setAttribute('aria-pressed', String(hidden));
    onSet?.(hidden);
  };
  apply(localStorage.getItem(key) === '1');
  btn.addEventListener('click', () => {
    const hidden = !document.body.classList.contains(cls);
    if (hidden) localStorage.setItem(key, '1');
    else localStorage.removeItem(key);
    apply(hidden);
    if (onSet) render();
  });
}

// The paint toggles: tag chips (and the tag filter bar), priority stamps,
// type stamps. No card data is touched. (The ledger numbers
// had a toggle here once; review retired it — they are simply never shown now,
// styles.css.)
displaySwitch('#toggle-tags', 'hide-tags', KEY_PREFIX + 'hideTags');
displaySwitch('#toggle-prios', 'hide-prios', KEY_PREFIX + 'hidePrios');
displaySwitch('#toggle-types', 'hide-types', KEY_PREFIX + 'hideTypes');
// The three column switches repaint (the empty `onSet`), because render()
// counts the card tracks the grid lays out, and a hidden Backlog or On hold is
// left out of the board so the capture row heads whichever column is leftmost.
displaySwitch('#toggle-done-col', 'hide-done-col', KEY_PREFIX + 'hideDoneCol', () => {});
displaySwitch('#toggle-inbox-col', 'hide-inbox-col', KEY_PREFIX + 'hideInboxCol', () => {});
displaySwitch('#toggle-hold-col', 'hide-hold-col', KEY_PREFIX + 'hideHoldCol', () => {});

// And the one that is not paint. Switching the column filter off has to take
// the hide-Done default with it, or cards go missing behind a dropdown that is
// no longer there to explain them — so it writes the flag matchesFilters reads.
displaySwitch('#toggle-columns', 'hide-columns', KEY_PREFIX + 'hideColumns',
  (hidden) => { filters.columnsOff = hidden; });

// The Assistant's Chat menu closes the same two ways. Registered here, once,
// rather than in renderChatActions: the rail is rebuilt on every render, and
// a listener added there would be added again on each one.
document.addEventListener('click', (e) => {
  const panel = $('#chat-menu-panel');
  if (panel && !panel.hidden && !e.target.closest('.chat-menu')) setChatMenuOpen(false);
});
// The Assistant's two header tools. Static markup, so unlike everything else
// about the Assistant they are wired once here rather than on every render —
// and the History panel therefore survives a repaint of the transcript
// underneath it.
$('#chat-history-btn').addEventListener('click', () => {
  assistantState.historyOpen = !assistantState.historyOpen;
  // Asked for on opening, so a chat added in another tab is there, and so the
  // deleted messages are the ones deleted since the panel was last read.
  if (assistantState.historyOpen) { refreshChatSessions(); refreshChatTrash(); }
  renderAssistantTools();
});
$('#chat-new').addEventListener('click', () => {
  // No confirmation: nothing is destroyed. The chat you were in is in the
  // record and one click away under History beside it.
  startNewChat();
  announce('New chat');
});
$('#assistant-extras-btn').addEventListener('click', () => setExtrasOpen(!extrasOpen));

// The launcher: one control over two states. On any other view it opens or
// closes the corner widget; on the Assistant view, where the conversation
// already has the whole screen, it folds back down instead of offering a
// second copy of what is in front of you.
$('#assistant-launcher').addEventListener('click', toggleWidget);

// One row, one thing dropped from it at a time: pressing History, New chat or
// the gear is a move to THAT tool, so the search fold shuts on the way. A
// click inside the fold is use of it, and a click outside the row entirely —
// the transcript, the composer — is none of the row's business and leaves it
// open, because a search you have to run again because you read your own
// conversation is not a search. Registered on the row itself, which moves
// between the app header and the sheet's head but is never rebuilt, so this
// listener travels with it; the tools' own handlers run first (they are the
// target, this is the ancestor), and the fold is shut on the live element
// afterwards rather than by asking for a repaint.
$('.assistant-tools')?.addEventListener('click', (e) => {
  if (e.target.closest('.chat-recall')) return;
  closeRecall();
});
// The panel's third way out. On the tools element, which owns both the button
// and the panel, so crossing from one to the other is never "left".
$('.assistant-tools').addEventListener('mouseleave', () => {
  if (assistantState.historyOpen) armHistoryIdle();
});
$('.assistant-tools').addEventListener('mouseenter', cancelHistoryIdle);
$('.assistant-tools').addEventListener('focusin', cancelHistoryIdle);

// And the same two ways out the board's menus have.
document.addEventListener('click', (e) => {
  // A click inside a dialog is not a click elsewhere: Rename and Delete both
  // open one, and it is centred on the screen rather than inside the panel, so
  // without this the OK that applies a rename is also what closes the list it
  // was applied in — the panel vanished the instant the work was confirmed.
  if (e.target.closest('dialog')) return;
  // A row's actions dismiss on the same rule, one level in: a click on the +
  // or inside the actions it unfolded is use, and anything else folds them —
  // including a click on another row, which would otherwise leave the list with
  // two rows of buttons in it.
  if (!fromChatRowMenu(e)) closeChatRowMenus();
  const outside = !e.target.closest('.assistant-tools');
  if (assistantState.historyOpen && outside) closeChatHistory();
  // The settings drawer is a dropdown now and has to shut like one. A click
  // INSIDE it is use, not dismissal — it holds the model pickers, the chat
  // menu and the export controls, and a panel that closed under the hand
  // reaching into it would be unusable.
  if (extrasOpen && outside) setExtrasOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  // One Escape closes one thing, innermost first — the same rule the chat menu
  // inside the extras drawer follows. Decided here rather than in a second
  // listener beside it: two handlers would both fire on the one keypress, and
  // whichever ran first would close its own layer and then hand the other a
  // page where nothing was in the way.
  if (closeChatRowMenus({ focusBack: true })) return;
  if (assistantState.historyOpen) { closeChatHistory({ focusBack: true }); return; }
  // The widget is the outermost layer of the three, so it goes last — and only
  // when the keypress happened inside it. Escape pressed on the board behind an
  // open widget is not a request to close the widget, and the two dropdowns
  // this handler has already dealt with above have to keep their first claim on
  // the key. The extras drawer has its own listener, so it is asked here rather
  // than assumed shut — the search fold was asked beside it until 2026-09-10,
  // by a selector that has matched nothing since the four tools became the
  // Assistant page's alone.
  const inWidget = document.activeElement?.closest?.('#assistant-widget');
  if (widgetShowing() && inWidget && !extrasOpen) closeWidget();
});
document.addEventListener('keydown', (e) => {
  // Only when the menu inside it is already shut, so one Escape closes one
  // thing: the chat menu's own handler below has the inner layer.
  const inner = $('#chat-menu-panel');
  if (e.key === 'Escape' && extrasOpen && (!inner || inner.hidden)) {
    setExtrasOpen(false);
    $('#assistant-extras-btn')?.focus();
  }
});
document.addEventListener('keydown', (e) => {
  const panel = $('#chat-menu-panel');
  if (e.key === 'Escape' && panel && !panel.hidden) {
    setChatMenuOpen(false);
    $('#chat-menu-btn').focus();
  }
});
