import { assistantState, ensureChatSession, refreshChatSessions } from '../assistant/session.js';
import { renderAssistant } from '../assistant/sheet.js';
import { renderAssistantTools, rescueAssistantTools } from '../assistant/tools.js';
import { COLUMNS, VIEWS, VIEW_LABELS } from '../core/constants.js';
import { timeline } from '../core/history.js';
import { VIEW_KEY } from '../core/keys.js';
import { dealCards, focusCardId, setCurrentView, setDealCards, setFocusCard, view } from '../core/state.js';
import { renderCatRail, renderColumn, renderTagBar } from './board.js';
import { $, announce } from './dom.js';
import { renderHabitBanner, renderHabitRail } from './habits.js';
import { renderPlanRail } from './plan.js';
import { refreshEdits, refreshProposals } from './proposals.js';
import { renderAreas } from '../views/areas.js';
import { renderBacklog } from '../views/backlog.js';
import { renderMatrix } from '../views/matrix.js';
import { renderOverview } from '../views/overview.js';
import { hidePlotTip } from '../views/plot.js';
import { renderReview } from '../views/review.js';

// Rendering — the single entry point that paints a view, and the view switch.
// Every mutation in the app ends in render(); no other module paints #board.

/** The rail beside the board: the habits due, then the plan. Two sections in
 *  one column, so the day's repetitions and the day's shortlist are read in
 *  the order they are done in. */
function boardRail() {
  const rail = document.createElement('aside');
  rail.className = 'board-rail';
  const habits = renderHabitRail();
  if (habits) rail.appendChild(habits);
  rail.appendChild(renderPlanRail());
  return rail;
}

export function render() {
  const board = $('#board');
  board.className = view;
  // The header's controls are the board's — search, the filters, the category
  // tabs, the tag bar, the ⚙ Menu (theme included). Which view is showing
  // decides whether they are furniture that does nothing.
  document.body.dataset.view = view;
  // The assistant's tools live in the sheet's head while the Assistant is
  // open, and the sheet is about to be torn down — rescue the wired-once node
  // back to its header parking spot BEFORE the wipe destroys it.
  rescueAssistantTools();
  board.innerHTML = '';
  hidePlotTip();
  if (view === 'backlog') {
    board.appendChild(renderBacklog());
  } else if (view === 'overview') {
    board.appendChild(renderOverview());
  } else if (view === 'matrix') {
    board.appendChild(renderMatrix());
  } else if (view === 'areas') {
    board.appendChild(renderAreas());
  } else if (view === 'review') {
    board.appendChild(renderReview());
  } else if (view === 'assistant') {
    board.appendChild(renderAssistant());
  } else {
    for (const col of COLUMNS) board.appendChild(renderColumn(col));
    board.appendChild(boardRail());
    board.classList.add('has-rail');
  }
  renderCatRail();
  renderTagBar();
  renderHabitBanner();
  renderAssistantTools();

  if (dealCards) {
    board.querySelectorAll('.card, .backlog-row').forEach((el, i) => {
      el.classList.add('deal');
      el.style.animationDelay = `${i * 45}ms`;
    });
    setDealCards(false);
  }

  if (focusCardId) {
    const el = board.querySelector(`[data-id="${focusCardId}"]`);
    if (el) el.focus();
    setFocusCard(null);
  }
}

const viewButtons = [...document.querySelectorAll('.view-switch button')];

export function syncViewButtons() {
  for (const btn of viewButtons) btn.setAttribute('aria-pressed', String(btn.dataset.view === view));
  syncProposalBadge();
}

// A count on the Assistant tab, so something left waiting while the user is on
// the Board is still noticed. Absent entirely when nothing is pending.
// Proposals and suggested edits share the count: from the Board they are the
// same fact — the Assistant is waiting on you.
export function syncProposalBadge() {
  const btn = viewButtons.find((b) => b.dataset.view === 'assistant');
  if (!btn) return;
  const n = assistantState.proposals.length + assistantState.edits.length;
  let badge = btn.querySelector('.view-badge');
  if (!n) {
    if (badge) badge.remove();
    btn.removeAttribute('aria-description');
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'view-badge';
    btn.appendChild(badge);
  }
  badge.textContent = String(n);
  btn.setAttribute('aria-description', `${n} item${n === 1 ? '' : 's'} awaiting your approval`);
}

function setView(next) {
  if (next === view || !VIEWS.includes(next)) return;
  setCurrentView(next);
  try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* private mode */ }
  syncViewButtons();
  // Entering the Assistant: make sure the lists are current, not stale, and
  // that a chat is open at all — the first visit of a session is where the
  // resume-or-start-fresh decision gets made.
  if (view === 'assistant') {
    refreshProposals(); refreshEdits();
    ensureChatSession().then(refreshChatSessions);
  }
  setDealCards(true); // re-deal for a gentle transition between views
  render();
  announce(`${VIEW_LABELS[view]} view`);
}

// Registering a listener is safe at evaluation time; *calling* into the state
// modules is not. syncViewButtons() reads `view`, and this module sits on a
// cycle back to core/state.js — so at the moment render.js evaluates, `view` can
// still be in its temporal dead zone and the whole graph dies with
// "Cannot access 'view' before initialization". It is called from main.js
// instead, which by definition runs after every module it imports.
for (const btn of viewButtons) btn.addEventListener('click', () => setView(btn.dataset.view));
