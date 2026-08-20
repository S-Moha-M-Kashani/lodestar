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

// One Menu button holds Undo / History / Export / Import. The panel closes on
// outside click, Escape, or after any action inside it is chosen.
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

// Two view toggles in the Menu: hide the tag chips (and the tag filter bar),
// hide the Done column. Purely visual — a body class the CSS reads — so the
// stored flag and the class are the whole state; no card data is touched.
// Same toggle idiom as #habit-mute: aria-pressed announces the state ("true"
// = hidden), and persisting is done here where the click is. Hidden stores
// '1', shown removes the key outright, and boot applies whatever is stored so
// a reload keeps the choice. (The ledger numbers had a toggle here once;
// review retired it — they are simply never shown now, styles.css.)
for (const [btnId, cls, key] of [
  ['#toggle-tags', 'hide-tags', KEY_PREFIX + 'hideTags'],
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
