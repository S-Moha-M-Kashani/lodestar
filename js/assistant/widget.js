import { renderChatChrome, restoreChatScroll } from './chrome.js';
import { brainModels, probeBrainModels } from './models.js';
import { assistantState, ensureChatSession, refreshChatSessions } from './session.js';
import { persistWidget, widgetShowing, widgetState } from './shell.js';
import { mountAssistantTools } from './tools.js';
import { view } from '../core/state.js';
import { $ } from '../ui/dom.js';
import { refreshEdits, refreshProposals } from '../ui/proposals.js';
import { lastBoardView, render, setView } from '../ui/render.js';

// The corner widget — the Assistant's second shell.
//
// The conversation is the same object, the same session and the same draft as
// the full view's; what differs is how much of the screen it is given. The two
// are never in the document at once: `#chat-input` is an id, and every e2e
// selector this project owns assumes there is one of it. render() decides which
// shell paints, and this module refuses to paint on the Assistant view.
//
// Three properties are worth knowing before changing anything here:
//
//  1. The HOST (`#assistant-widget`) is static markup and render() never
//     destroys it. That is where the size lives, as two custom properties, so
//     a resize survives every repaint with no restore step. Only the card
//     inside it is rebuilt.
//  2. The host is the last child of <body>, not a child of the header or of
//     #board. #board is wiped on every render, and the header is a stacking
//     context with overflow rules of its own — a fixed-position child of one
//     is trapped by it.
//  3. The widget is a fixed-position box, so it is a ceiling for everything
//     inside it: its panels open INSIDE it, and nothing here may put
//     `overflow: hidden` on the box that holds them.

// Where the clamps sit. Measured in the browser against a real transcript
// (2026-09-02, 1440×900): below ~300px a reply's card rows wrap mid-id and the
// composer footer drops its model name; above ~720px the card stops being a
// margin on the board and becomes a second column, at which point the full view
// is the better answer. The height bounds are the same argument vertically —
// under 320px the transcript shows one turn, and 900px is taller than the
// window this was measured in. The default is the size a three-turn exchange
// reads comfortably at without covering the board's last column.
const MIN_W = 300;
const MAX_W = 720;
const MIN_H = 320;
const MAX_H = 900;
const DEFAULT_W = 380;
const DEFAULT_H = 520;
// Below this the card becomes a sheet and the handle goes away — see the media
// query in styles.css, which reads the same number. A 340px floating card with
// a composer in it is a worse phone UI than a sheet, and a handle you cannot
// usefully drag is a control that lies.
const NARROW_PX = 640;
// The margin the widget keeps from the edges of the window, both here and in
// the CSS. Named once so the clamp and the layout cannot disagree.
const EDGE_PX = 16;

const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), hi);

/** The size the window can actually hold, which is the real upper bound: a
 *  remembered 900×800 opened in a 600×500 window has to come back clamped, not
 *  cropped and half off-screen. */
const roomW = () => Math.max(MIN_W, Math.min(MAX_W, window.innerWidth - EDGE_PX * 2));
const roomH = () => Math.max(MIN_H, Math.min(MAX_H, window.innerHeight - EDGE_PX * 2));

/** Write the size onto the host — the one node render() never replaces — and
 *  clamp it to what the window can hold on the way. */
function applySize() {
  const host = $('#assistant-widget');
  if (!host) return;
  // A size of 0 is "never resized" — shell.js reads the stored value without
  // knowing the bounds, so the default is applied here, where they live.
  if (!widgetState.w) widgetState.w = DEFAULT_W;
  if (!widgetState.h) widgetState.h = DEFAULT_H;
  widgetState.w = clamp(widgetState.w, MIN_W, roomW());
  widgetState.h = clamp(widgetState.h, MIN_H, roomH());
  host.style.setProperty('--assistant-widget-w', `${widgetState.w}px`);
  host.style.setProperty('--assistant-widget-h', `${widgetState.h}px`);
}

/** Put the caret in the composer — but only if nothing else has claimed it.
 *
 *  Opening the widget kicks off three requests, and each one repaints when it
 *  answers, which destroys the textarea the caret was in. So the focus is taken
 *  again once they have all settled. The guard is what keeps that from being
 *  rude: if the user has moved on to the board's search box in the meantime,
 *  focus is theirs and a late-arriving list must not drag them into the chat. */
function focusComposer() {
  const active = document.activeElement;
  const free = !active || active === document.body
    || active.id === 'assistant-launcher';
  if (free) document.getElementById('chat-input')?.focus();
}

export function openWidget() {
  if (widgetState.open) return;
  widgetState.open = true;
  persistWidget();
  render();
  focusComposer();
  // The lists behind the tools are the Assistant view's boot work, and the
  // widget is now a way into the conversation that never passes through it.
  Promise.all([refreshProposals(), refreshEdits(),
               ensureChatSession().then(refreshChatSessions)])
    .finally(focusComposer);
}

export function closeWidget({ focusBack = true } = {}) {
  if (!widgetState.open) return;
  widgetState.open = false;
  persistWidget();
  render();
  if (focusBack) $('#assistant-launcher')?.focus();
}

export function toggleWidget() {
  // On the Assistant view the launcher is the way back down rather than a
  // second copy of what is already on screen.
  if (view === 'assistant') { collapseToWidget(); return; }
  if (widgetState.open) closeWidget();
  else openWidget();
}

/** Up to the full view: the same chat, the same draft, more room. */
export function expandToView() {
  widgetState.open = false;
  persistWidget();
  setView('assistant');
}

/** And back down, to the view the Assistant was reached from — the Board when
 *  there is no earlier one. */
export function collapseToWidget() {
  widgetState.open = true;
  persistWidget();
  setView(lastBoardView());
  focusComposer();
}

/** Paint the widget into its host. Called by render() on every paint, after
 *  the view beneath it — the widget outlives every one of them. */
export function renderAssistantWidget() {
  const host = $('#assistant-widget');
  if (!host) return;
  host.innerHTML = '';
  host.hidden = !widgetShowing();
  if (!widgetShowing()) return;
  // The same probe the full view runs on entry: a brain started after the page
  // has to be found without a reload, and the widget may be the only shell this
  // browser ever opens.
  if (!brainModels.provider) probeBrainModels();
  applySize();

  const card = document.createElement('div');
  card.className = 'widget-card';

  // No handle below the narrow threshold: there the card is a sheet filling
  // the window, and a grip that cannot usefully be dragged is a control that
  // lies. The media query hides it too, for a window resized without a repaint.
  if (window.innerWidth >= NARROW_PX) card.appendChild(renderResizeHandle());
  card.appendChild(renderWidgetHead());

  // One line naming what is waiting, and a way to the room where it can be
  // read and decided. Deliberately not the decision itself: accepting a
  // proposal opens the card dialog, which needs more space than this has.
  const waiting = assistantState.proposals.length + assistantState.edits.length;
  if (waiting) card.appendChild(renderApprovalsStrip(waiting));

  const log = renderChatChrome(card, 'widget');
  host.appendChild(card);
  restoreChatScroll(log);
}

function renderWidgetHead() {
  const head = document.createElement('div');
  head.className = 'widget-head';

  // Which chat you are in. A label, clamped to two lines — the same job the
  // dock does in the full view, in the only place this shell has for it.
  const current = assistantState.sessions.find((s) => s.id === assistantState.sessionId);
  const title = document.createElement('span');
  title.className = 'widget-title';
  title.textContent = current ? current.title : 'New chat';
  title.title = current ? current.title : 'New chat';
  head.appendChild(title);

  const expand = document.createElement('button');
  expand.type = 'button';
  expand.id = 'assistant-expand';
  expand.className = 'btn ghost widget-btn';
  expand.textContent = '⤢';
  expand.title = 'Open the full Assistant view';
  expand.setAttribute('aria-label', 'Expand to the full Assistant view');
  expand.addEventListener('click', expandToView);

  const close = document.createElement('button');
  close.type = 'button';
  close.id = 'assistant-widget-close';
  close.className = 'btn ghost widget-btn';
  close.textContent = '×';
  close.title = 'Close the assistant (Esc)';
  close.setAttribute('aria-label', 'Close the assistant');
  close.addEventListener('click', () => closeWidget());

  head.append(expand, close);

  // The tools take a line of their own, under the title and the two controls
  // that belong to the window rather than to the conversation. Measured: at
  // 380px the search fold, History, New chat and the ⚙ fill a line by
  // themselves, and squeezing all six onto one clamped the chat's name to a
  // single letter — a title that names no chat is worse than a second line.
  // The DOM order is the reading order, so Tab follows the eye.
  const slot = document.createElement('div');
  slot.className = 'widget-tools-slot';
  head.appendChild(slot);
  // The wired-once node, moved in rather than copied: search, History, New chat
  // and the ⚙ are the same four controls, with the same listeners and the same
  // open panels, whichever shell is hosting them.
  mountAssistantTools(slot);
  return head;
}

function renderApprovalsStrip(n) {
  const strip = document.createElement('div');
  strip.className = 'widget-approvals';
  const said = document.createElement('span');
  said.className = 'widget-approvals-text';
  said.textContent = `${n} item${n === 1 ? '' : 's'} awaiting your approval`;
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'btn ghost widget-approvals-open';
  open.textContent = 'Review';
  open.title = 'Open the full Assistant view, where each one can be read';
  open.addEventListener('click', expandToView);
  strip.append(said, open);
  return strip;
}

/** The top-left handle. Pointer events rather than mouse events, so one code
 *  path covers touch and pen, and `setPointerCapture` so a fast drag that
 *  leaves the handle does not drop the gesture.
 *
 *  Top-LEFT because the widget is docked bottom-right: growing from the other
 *  corner would push the card off the screen it is anchored to. Native
 *  `resize: both` does exactly that, which is why it is not used. */
function renderResizeHandle() {
  const grip = document.createElement('div');
  grip.className = 'widget-resize';
  grip.setAttribute('role', 'separator');
  grip.setAttribute('aria-label', 'Resize the assistant');
  let from = null;
  grip.addEventListener('pointerdown', (e) => {
    from = { x: e.clientX, y: e.clientY, w: widgetState.w, h: widgetState.h };
    grip.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (!from) return;
    // Dragging left and up makes it bigger: the anchored corner is the one
    // diagonally opposite the hand.
    widgetState.w = clamp(from.w + (from.x - e.clientX), MIN_W, roomW());
    widgetState.h = clamp(from.h + (from.y - e.clientY), MIN_H, roomH());
    applySize();
  });
  const settle = (e) => {
    if (!from) return;
    from = null;
    if (grip.hasPointerCapture?.(e.pointerId)) grip.releasePointerCapture(e.pointerId);
    persistWidget();
  };
  grip.addEventListener('pointerup', settle);
  grip.addEventListener('pointercancel', settle);
  return grip;
}

// A window that shrank under a remembered size has to bring it back inside,
// and the size lives on the host rather than in the layout — so nothing else
// would notice. Cheap enough to do on every resize event: two clamps and two
// custom properties, no render.
window.addEventListener('resize', () => { if (widgetShowing()) applySize(); });
