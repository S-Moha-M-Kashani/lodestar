import { closeRecall } from '../assistant/recall.js';
import { assistantState, refreshChatSessions, refreshChatTrash, startNewChat } from '../assistant/session.js';
import { armHistoryIdle, cancelHistoryIdle, closeChatHistory, closeChatRowMenus, extrasOpen, fromChatRowMenu, renderAssistantTools, setChatMenuOpen, setExtrasOpen } from '../assistant/tools.js';
import { KEY_PREFIX } from '../core/keys.js';
import { filters } from '../core/state.js';
import { $, announce } from './dom.js';
import { render } from './render.js';

// Toolbar: search, the type and priority filters, the actions menu, the
// Assistant's header controls, and the keyboard shortcuts that reach them.

$('#search').addEventListener('input', (e) => {
  filters.search = e.target.value.trim().toLowerCase();
  render();
});

$('#type-filter').addEventListener('change', (e) => {
  filters.type = e.target.value;
  render();
});

$('#prio-filter').addEventListener('change', (e) => {
  filters.prio = e.target.value;
  render();
});

// The tags dropdown: one tag or all of them. It writes the same Set the #
// bar toggles, so the two stay one filter with two handles — picking here
// replaces whatever combination the bar had built, and the empty option
// clears it.
$('#tag-filter').addEventListener('change', (e) => {
  filters.tags.clear();
  if (e.target.value) filters.tags.add(e.target.value);
  render();
});

// One Menu button holds the board actions, History / Export / Import, the
// Show and Sound hover submenus, the habit sound and the theme. The panel
// closes on outside click, Escape, or after any one-shot action inside it is
// chosen; the submenus and the toggles they hold keep it open, because
// flipping three toggles should not mean opening the menu three times.
const menuBtn = $('#menu-btn');
const menuPanel = $('#menu-panel');

// A hover submenu: unfolds while the pointer is over its wrapper, and the
// button itself toggles it for keyboard and touch, where there is no hover.
function wireFlyout(btnSel, panelSel) {
  const btn = $(btnSel);
  const panel = $(panelSel);
  const wrap = btn.parentElement;
  const setOpen = (open) => {
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
  };
  wrap.addEventListener('mouseenter', () => setOpen(true));
  wrap.addEventListener('mouseleave', () => setOpen(false));
  btn.addEventListener('click', () => setOpen(panel.hidden));
  return setOpen;
}
const setShowOpen = wireFlyout('#menu-show', '#show-panel');
const setSoundOpen = wireFlyout('#menu-sound', '#sound-panel');
const setPlanOpen = wireFlyout('#menu-plan', '#plan-panel');

function setMenuOpen(open) {
  menuPanel.hidden = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
  if (!open) { setShowOpen(false); setSoundOpen(false); setPlanOpen(false); }
}

menuBtn.addEventListener('click', () => setMenuOpen(menuPanel.hidden));
menuPanel.addEventListener('click', (e) => {
  // Submenus, their toggles, the sound on/off and the theme row are "stay
  // open" surfaces — flipping a switch and watching it flip is the point.
  // Only a one-shot action (history, export, a board action…) dismisses.
  if (e.target.closest('.menu-flyout') || e.target.closest('.menu-theme-row')
      || e.target.closest('#habit-mute')) return;
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

// The Show submenu's four paint toggles: tag chips (and the tag filter bar),
// priority stamps, type stamps, the Done column. Purely visual — a body class
// the CSS reads — so the
// stored flag and the class are the whole state; no card data is touched.
// Same toggle idiom as #habit-mute: aria-pressed announces the state ("true"
// = hidden), and persisting is done here where the click is. Hidden stores
// '1', shown removes the key outright, and boot applies whatever is stored so
// a reload keeps the choice. (The ledger numbers had a toggle here once;
// review retired it — they are simply never shown now, styles.css.)
for (const [btnId, cls, key] of [
  ['#toggle-tags', 'hide-tags', KEY_PREFIX + 'hideTags'],
  ['#toggle-prios', 'hide-prios', KEY_PREFIX + 'hidePrios'],
  ['#toggle-types', 'hide-types', KEY_PREFIX + 'hideTypes'],
  ['#toggle-done-col', 'hide-done-col', KEY_PREFIX + 'hideDoneCol'],
]) {
  const btn = $(btnId);
  const apply = (hidden) => {
    document.body.classList.toggle(cls, hidden);
    btn.setAttribute('aria-pressed', String(hidden));
  };
  apply(localStorage.getItem(key) === '1');
  btn.addEventListener('click', () => {
    const hidden = !document.body.classList.contains(cls);
    if (hidden) localStorage.setItem(key, '1');
    else localStorage.removeItem(key);
    apply(hidden);
  });
}

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
  if (assistantState.historyOpen) closeChatHistory({ focusBack: true });
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
