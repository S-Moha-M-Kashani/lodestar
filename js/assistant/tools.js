import { renderChatSettings } from './models.js';
import { renderRecallPanel } from './recall.js';
import { assistantState, chatCacheKey, openChatSession, purgeChatMessage, refreshChatSessions, restoreChatMessage, startNewChat } from './session.js';
import { sendChat } from './streaming.js';
import { boardUrl } from '../core/boards.js';
import { view } from '../core/state.js';
import { ask, prompt } from '../ui/dialogs.js';
import { $, announce } from '../ui/dom.js';
import { render } from '../ui/render.js';
import { importChatFile, openExportDialog } from '../ui/transfer.js';

// The Assistant's header tools: search, the chats panel, New chat, and the
// settings gear. Static markup in the app header, wired once at boot — so a
// repaint of the transcript underneath cannot tear an open panel down. The
// corner widget deliberately carries none of them.
// Whether the extras drawer is unfolded. It lives with setExtrasOpen below
// rather than with the Models panel it contains: the button that opens it is
// header furniture wired once at boot, and the panel and the button are applied
// independently, so one guard over both is what left the button claiming
// "expanded" over a shut drawer.

export let extrasOpen = false;

const CHAT_HISTORY_IDLE_MS = 4000;
let historyIdleTimer = null;

export function cancelHistoryIdle() {
  if (historyIdleTimer) clearTimeout(historyIdleTimer);
  historyIdleTimer = null;
}

/** Close the panel once it has been left alone — but ask again rather than act
 *  if it is still in use, and read the DOM afresh each time instead of holding
 *  the dock element: every render replaces it, and a timer closing over the
 *  previous one would decide on a node no longer in the page.
 *
 *  "In use" includes a dialog the panel itself opened. Rename and Delete both
 *  open one in the middle of the screen, which necessarily takes the pointer
 *  out of the tools — the panel closing under an open dialog loses the place
 *  the user was working in, which is the bug this check exists for.
 *
 *  Focus counts only inside the PANEL, not anywhere in the tools. The button
 *  is static markup and keeps focus after the click that opened the list, so
 *  counting it would mean a panel opened with the mouse never idle-closes at
 *  all — it survived only because the old dock was rebuilt by every render,
 *  which dropped focus to the body by destroying the button holding it.
 *  Someone reading the list with a keyboard has tabbed into it, and is still
 *  never rushed. */
export function armHistoryIdle() {
  cancelHistoryIdle();
  historyIdleTimer = setTimeout(function settle() {
    const tools = document.querySelector('.assistant-tools');
    const panel = document.getElementById('chat-history');
    const busy = document.querySelector('dialog[open]')
      || (tools && tools.matches(':hover'))
      || (panel && panel.contains(document.activeElement));
    if (busy) {
      historyIdleTimer = setTimeout(settle, CHAT_HISTORY_IDLE_MS);
      return;
    }
    closeChatHistory();
  }, CHAT_HISTORY_IDLE_MS);
}

export function closeChatHistory({ focusBack = false } = {}) {
  cancelHistoryIdle();
  if (!assistantState.historyOpen) return;
  assistantState.historyOpen = false;
  // The panel alone, not the whole view: closing a list must not repaint the
  // transcript under it and lose the reader's place in the conversation.
  renderAssistantTools();
  // Escape and a chosen chat return the caret to the button that opened the
  // panel; a click elsewhere is the user already looking somewhere else.
  if (focusBack) document.getElementById('chat-history-btn')?.focus();
}

/** The dock: which chat you are in, and New chat under it. In the page margin
 *  off the sheet's corner, because in the toolbar row they were two more
 *  buttons among five and went unnoticed.
 *
 *  It used to open the chats panel too, from a ▾ on the title. That panel is
 *  in the header now — hanging it off a button in the margin is what made the
 *  history, and the deleted messages at the foot of it, unfindable. So the
 *  title is a label again: it says where you are, and no longer pretends to
 *  be a control. */
export function renderChatDock() {
  const dock = document.createElement('div');
  dock.className = 'chat-dock';
  dock.appendChild(renderChatSwitcher());
  return dock;
}

function renderChatSwitcher() {
  const wrap = document.createElement('div');
  wrap.className = 'chat-switcher';

  const current = assistantState.sessions.find((s) => s.id === assistantState.sessionId);
  const here = document.createElement('span');
  here.className = 'chat-current';
  // An unsaved new chat has no row in the list yet, and saying so is more
  // honest than borrowing the title of the chat you just left.
  const label = document.createElement('span');
  label.className = 'chat-history-label';
  label.textContent = current ? current.title : 'New chat';
  here.appendChild(label);
  here.title = current ? current.title : 'New chat';

  wrap.append(here);
  return wrap;
}

// The tools row is one wired-once node that MOVES: it sits in the sheet's own
// header while the Assistant view is open, and is parked back in the app
// header, hidden, when it is not. Moving the same element keeps its listeners;
// what would destroy them is being inside the sheet when render() wipes #board
// — hence the rescue, called before every wipe.
const toolsEl = document.querySelector('.assistant-tools');
const toolsHome = toolsEl ? toolsEl.parentElement : null;

export function rescueAssistantTools() {
  if (toolsEl && toolsHome && toolsEl.parentElement !== toolsHome) {
    toolsHome.appendChild(toolsEl);
  }
}

/** Seat the tools row in the sheet's head, its one host. Called once per render
 *  of the Assistant view; the rescue above is what guarantees the node is alive
 *  to be moved. */
export function mountAssistantTools(parent) {
  if (toolsEl) parent.appendChild(toolsEl);
}

/** Whether the tools have a shell to belong to. One host: the Assistant sheet's
 *  head. The widget hosted them too until the four controls were measured
 *  taking a whole line of a 380px card — off that view the row is furniture
 *  that configures a screen nobody is on. */
const toolsLive = () => view === 'assistant';

/** The Assistant's tools: search the record, History, New chat, the settings
 *  gear. Static markup driven from here, so the panels are not rebuilt by a
 *  render of the transcript underneath them.
 *
 *  Shown only on the Assistant view, seated in the sheet's own header: a gear
 *  that configures a screen you are not on is furniture that does nothing. */
export function renderAssistantTools() {
  const tools = $('.assistant-tools');
  if (!tools) return;
  tools.hidden = !toolsLive();
  const btn = $('#chat-history-btn');
  if (btn) btn.setAttribute('aria-expanded', String(assistantState.historyOpen));
  const gear = $('#assistant-extras-btn');
  if (gear) gear.setAttribute('aria-expanded', String(extrasOpen));
  // Searching past conversations is the leftmost of the group: it acts on the
  // record the two beside it list and start, and it is the one you reach for
  // while reading rather than while administering.
  const recall = $('#chat-recall-slot');
  if (recall) {
    recall.innerHTML = '';
    if (toolsLive()) recall.appendChild(renderRecallPanel());
  }
  const slot = $('#chat-history-slot');
  if (slot) {
    slot.innerHTML = '';
    if (toolsLive() && assistantState.historyOpen) slot.appendChild(renderChatHistory());
  }
  // The settings drop from the gear the way the chats drop from History —
  // both are things you open, look at, and shut again. Built even while shut,
  // because setExtrasOpen only flips `hidden`: it must not rebuild the model
  // pickers, which would throw away a half-typed choice on every toggle.
  const drawer = $('#assistant-extras-slot');
  if (!drawer) return;
  drawer.innerHTML = '';
  if (!toolsLive()) return;
  const extras = document.createElement('div');
  extras.id = 'assistant-extras';
  extras.className = 'assistant-extras';
  extras.hidden = !extrasOpen;
  extras.appendChild(renderChatActions());
  extras.appendChild(renderChatSettings());
  drawer.appendChild(extras);
}

/** The chats, grouped by day, newest first. A panel across the sheet rather
 *  than a rail beside it: a rail was measured costing the transcript 300px and
 *  was removed, and this list is read occasionally, not watched. */
function renderChatHistory() {
  const panel = document.createElement('div');
  panel.id = 'chat-history';
  panel.className = 'chat-history';

  if (!assistantState.sessions.length && !assistantState.trash.length) {
    const empty = document.createElement('p');
    empty.className = 'chat-status';
    empty.textContent = 'No earlier chats yet — this is the first one.';
    panel.appendChild(empty);
    return panel;
  }

  let lastGroup = '';
  for (const session of assistantState.sessions) {
    const group = dayGroupLabel(session.updatedAt);
    if (group !== lastGroup) {
      lastGroup = group;
      const heading = document.createElement('div');
      heading.className = 'chat-history-day';
      heading.textContent = group;
      panel.appendChild(heading);
    }
    panel.appendChild(renderChatHistoryRow(session));
  }
  // The turns deleted one at a time, under the chats they came out of — the
  // board's Trash in the place the assistant's own history is read. Absent
  // entirely when empty: a permanent "nothing here" heading is furniture.
  if (assistantState.trash.length) {
    const heading = document.createElement('div');
    heading.className = 'chat-history-day chat-trash-heading';
    heading.textContent = 'Deleted messages';
    panel.appendChild(heading);
    for (const row of assistantState.trash) panel.appendChild(renderChatTrashRow(row));
  }
  return panel;
}

/** One deleted turn: what it said, which chat it came out of, and the two
 *  ways out. Both controls stay visible rather than appearing on hover — this
 *  is the only screen where a permanent delete can be reached, and a hidden
 *  destructive control is worse than a visible one. */
function renderChatTrashRow(row) {
  const el = document.createElement('div');
  el.className = 'chat-history-item chat-trash-item';

  const text = document.createElement('div');
  text.className = 'chat-history-open chat-trash-text';
  const said = document.createElement('span');
  said.className = 'chat-history-title';
  said.textContent = row.content;
  const from = document.createElement('span');
  from.className = 'chat-history-count';
  from.textContent = `${row.role === 'user' ? 'you' : 'assistant'} · ${row.sessionTitle}`;
  text.append(said, from);

  const restore = document.createElement('button');
  restore.type = 'button';
  restore.className = 'btn ghost chat-trash-restore';
  restore.textContent = 'Restore';
  restore.addEventListener('click', () => restoreChatMessage(row));

  const purge = document.createElement('button');
  purge.type = 'button';
  purge.className = 'btn danger chat-trash-purge';
  purge.textContent = 'Delete permanently';
  purge.addEventListener('click', () => purgeChatMessage(row));

  const actions = document.createElement('div');
  actions.className = 'chat-trash-actions';
  actions.append(restore, purge);

  el.append(text, actions);
  return el;
}

/** Today / Yesterday / the date — the way a reader actually looks for a
 *  conversation they half-remember. */
function dayGroupLabel(ms) {
  const then = new Date(ms);
  const today = new Date();
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(today) - midnight(then)) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return then.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

// The id a row's actions are named by, so its + can point `aria-controls` at
// them. A counter and not the session id: the id is a client-supplied string
// (`adhoc`, `e2e-doomed`, whatever an import carried) and nothing guarantees it
// is a legal, unique DOM id.
let rowActionsSeq = 0;

/** Is this event inside a row's own control — its + or the actions it unfolds?
 *  Both count as use, so neither dismisses the thing being used. */
export const fromChatRowMenu = (e) =>
  Boolean(e.target.closest?.('.chat-row-menu-btn, .chat-row-actions'));

/** Fold away whichever row is unfolded, and say whether there was one.
 *
 *  At most one ever is: opening a row folds the others first, so a list read
 *  top to bottom does not end up with five rows of buttons in it. The answer is
 *  what lets Escape close one thing at a time — the row's actions before the
 *  list they are unfolded in. */
export function closeChatRowMenus({ focusBack = false } = {}) {
  const open = document.querySelector('.chat-row-actions:not([hidden])');
  if (!open) return false;
  open.hidden = true;
  const btn = open.closest('.chat-history-item')?.querySelector('.chat-row-menu-btn');
  if (btn) {
    btn.setAttribute('aria-expanded', 'false');
    if (focusBack) btn.focus();
  }
  return true;
}

/** One chat in the list: what it is called, how long it is, and a + holding
 *  what can be done to it.
 *
 *  Rename and Delete used to sit in the row itself, collapsed to `max-width: 0`
 *  until the row was hovered — which is why they could not be reached at all
 *  without a pointer. They are behind one always-painted control now, the same
 *  shape the board's cards use (`ui/card-menu.js`): a real button in flow, 24px
 *  on a finger, tabbable, and costing the row 24px instead of the 134px two
 *  buttons held.
 *
 *  It unfolds *inside* the row rather than dropping a panel over it, because
 *  this list is itself a 320px dropdown with `overflow-y: auto` — an absolutely
 *  positioned panel would be clipped by that scroller, and dodging the clip is
 *  the flip-and-measure code card-menu needs. Unfolded, the actions take a line
 *  of their own and the title keeps the whole width it had. */
function renderChatHistoryRow(session) {
  const row = document.createElement('div');
  row.className = 'chat-history-item';
  if (session.id === assistantState.sessionId) row.classList.add('current');

  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'chat-history-open';
  const title = document.createElement('span');
  title.className = 'chat-history-title';
  title.textContent = session.title;
  const count = document.createElement('span');
  count.className = 'chat-history-count';
  count.textContent = session.messageCount === 1 ? '1 message' : `${session.messageCount} messages`;
  open.append(title, count);
  open.addEventListener('click', () => {
    if (session.id === assistantState.sessionId) {
      assistantState.historyOpen = false;
      render();
      return;
    }
    openChatSession(session.id);
    announce(`Opened ${session.title}`);
  });

  const rename = document.createElement('button');
  rename.type = 'button';
  rename.className = 'btn ghost chat-history-rename';
  rename.textContent = 'Rename';
  rename.addEventListener('click', () => { closeChatRowMenus(); renameChat(session); });

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn ghost chat-history-delete';
  remove.textContent = 'Delete';
  remove.addEventListener('click', () => { closeChatRowMenus(); deleteChat(session); });

  const actions = document.createElement('div');
  actions.id = `chat-row-actions-${++rowActionsSeq}`;
  actions.className = 'chat-row-actions';
  actions.hidden = true;
  actions.append(rename, remove);

  const menu = document.createElement('button');
  menu.type = 'button';
  menu.className = 'chat-row-menu-btn';
  menu.textContent = '+';
  menu.title = 'Chat actions';
  menu.setAttribute('aria-expanded', 'false');
  menu.setAttribute('aria-controls', actions.id);
  menu.setAttribute('aria-label', `Actions for ${session.title}`);
  menu.addEventListener('click', () => {
    const opening = actions.hidden;
    closeChatRowMenus();
    if (!opening) return;
    actions.hidden = false;
    menu.setAttribute('aria-expanded', 'true');
    // Focus lands on Rename, so the keyboard path is Tab to the +, Enter, and
    // you are on the first action rather than back at the top of the list.
    rename.focus();
  });

  row.append(open, menu, actions);
  return row;
}

async function renameChat(session) {
  const title = await prompt({
    title: 'Rename chat', label: 'Title', value: session.title, okLabel: 'Rename',
  });
  if (title === null) return;
  try {
    const res = await fetch('/api/chat/sessions/' + encodeURIComponent(session.id), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    if (!res.ok) throw new Error('the server refused that title');
    await refreshChatSessions();
    announce('Chat renamed');
  } catch (err) {
    announce(`Rename failed — ${err.message}`);
  }
}

async function deleteChat(session) {
  const go = await ask({
    title: 'Delete chat',
    message: `Delete “${session.title}”? Its messages stay in the record, but the `
      + 'chat leaves this list and the assistant stops recalling from it.',
    okLabel: 'Delete',
  });
  if (!go) return;
  try {
    const res = await fetch('/api/chat/sessions/' + encodeURIComponent(session.id),
      { method: 'DELETE' });
    if (!res.ok) throw new Error(`the server refused (${res.status})`);
  } catch (err) {
    announce(`Delete failed — ${err.message}`);
    return;
  }
  try { localStorage.removeItem(chatCacheKey(session.id)); } catch { /* private mode */ }
  // The record no longer returns those rows; this is what takes them out of
  // the recall index too, rather than leaving a deleted chat answering
  // questions until the brain next boots.
  fetch(boardUrl('/api/rag/chat/reindex'), { method: 'POST' }).catch(() => {});
  await refreshChatSessions();
  if (session.id === assistantState.sessionId) startNewChat({ focus: false });
  else render();
  announce('Chat deleted');
}

/** The nudge. Shown instead of sending, with the message still in the
 *  composer: a suggestion you cannot refuse is a decision, so both answers are
 *  one click and the turn goes either way. */
export function renderChatDrift() {
  const strip = document.createElement('div');
  strip.className = 'chat-drift';

  const said = document.createElement('p');
  said.className = 'chat-drift-text';
  said.textContent = assistantState.drift.reason === 'opener'
    ? 'That looks like the start of something new rather than part of this chat.'
    : 'That looks like a new subject rather than part of this chat.';
  strip.appendChild(said);

  const actions = document.createElement('div');
  actions.className = 'chat-drift-actions';

  const fresh = document.createElement('button');
  fresh.type = 'button';
  fresh.className = 'btn primary';
  fresh.textContent = 'Start a new chat';
  fresh.addEventListener('click', () => {
    const { text } = assistantState.drift;
    startNewChat({ focus: false });
    sendChat(text);
  });

  const stay = document.createElement('button');
  stay.type = 'button';
  stay.className = 'btn';
  stay.textContent = 'Keep this one';
  stay.addEventListener('click', () => {
    const { text } = assistantState.drift;
    // Told once that this conversation is broad, it must not ask again in it.
    assistantState.driftDismissed = true;
    sendChat(text);
  });

  actions.append(fresh, stay);
  strip.appendChild(actions);
  return strip;
}

// The rail's controls: one menu holding what you do to a transcript. Export
// and import were two buttons competing with
// the heading for the top of the sheet; they are the same shape of thing the
// board's Menu already holds, so they are built from its parts — .menu-panel
// and .menu-item — rather than as a second design for one job.
function renderChatActions() {
  const wrap = document.createElement('div');
  wrap.className = 'assistant-actions';

  const menu = document.createElement('div');
  menu.className = 'chat-menu';
  const menuBtn = document.createElement('button');
  menuBtn.type = 'button';
  menuBtn.id = 'chat-menu-btn';
  menuBtn.className = 'btn ghost';
  menuBtn.setAttribute('aria-haspopup', 'true');
  menuBtn.setAttribute('aria-expanded', 'false');
  menuBtn.textContent = 'Chat ▾';
  menuBtn.title = 'Export or import this conversation';
  const panel = document.createElement('div');
  panel.id = 'chat-menu-panel';
  panel.className = 'menu-panel';
  panel.hidden = true;
  menuBtn.addEventListener('click', () => setChatMenuOpen(panel.hidden));
  // Offered even with an empty transcript: a control that appears only
  // sometimes is harder to find than one that always sits in the same place
  // and exports nothing.
  const exportBtn = document.createElement('button');
  exportBtn.type = 'button';
  exportBtn.id = 'chat-export-btn';
  exportBtn.className = 'menu-item';
  exportBtn.textContent = 'Export chat';
  exportBtn.title = 'Save this conversation as JSON or Markdown';
  exportBtn.addEventListener('click', () => openExportDialog('chat'));
  // Import — the missing half of export: a saved JSON transcript goes into
  // the durable chat record (databases/assistant.db) through the Node API.
  const importBtn = document.createElement('button');
  importBtn.type = 'button';
  importBtn.id = 'chat-import-btn';
  importBtn.className = 'menu-item';
  importBtn.textContent = 'Import chat';
  importBtn.title = 'Read a chat JSON export into the durable chat record';
  const importFile = document.createElement('input');
  importFile.type = 'file';
  importFile.id = 'chat-import-file';
  importFile.accept = 'application/json,.json';
  importFile.hidden = true;
  importFile.addEventListener('change', () => {
    const file = importFile.files && importFile.files[0];
    if (file) importChatFile(file);
    importFile.value = '';   // same file again must re-fire the change event
  });
  importBtn.addEventListener('click', () => importFile.click());
  panel.append(exportBtn, importBtn);
  // Closes after any action inside it, exactly as the board's Menu does.
  panel.addEventListener('click', (e) => {
    if (e.target.closest('button')) setChatMenuOpen(false);
  });
  menu.append(menuBtn, panel, importFile);
  wrap.appendChild(menu);
  return wrap;
}

// Toggled in place rather than through render(): re-rendering the view would
// rebuild the transcript and lose the log's scroll position, and nothing else
// on the sheet depends on whether this drawer is open.
export function setExtrasOpen(open) {
  extrasOpen = open;
  // Each half applied on its own, never behind a guard on both. The button is
  // static markup and the panel exists only on the Assistant, so a shared
  // early return left the button claiming "expanded" about a panel that had
  // been torn down when the view changed — and the next visit found the gear
  // saying open over a drawer that was shut.
  const btn = $('#assistant-extras-btn');
  if (btn) btn.setAttribute('aria-expanded', String(open));
  const panel = $('#assistant-extras');
  if (panel) panel.hidden = !open;
}

// Looked up rather than closed over: the rail is rebuilt on every render, so
// a captured element would be the one from a render ago.
export function setChatMenuOpen(open) {
  const btn = $('#chat-menu-btn');
  const panel = $('#chat-menu-panel');
  if (!btn || !panel) return;
  panel.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
}

// Chat memory has been searchable by HTTP since the brain gained a Chroma
// store, and reachable from the UI only by asking the agent and hoping it
// chose the tool. `matches: null` is "not asked yet", which is not the same
// as "asked and found nothing".
