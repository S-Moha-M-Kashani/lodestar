import { assistantState, ensureChatSession, refreshChatSessions } from '../assistant/session.js';
import { renderAssistant } from '../assistant/sheet.js';
import { renderAssistantTools, rescueAssistantTools } from '../assistant/tools.js';
import { widgetShowing } from '../assistant/shell.js';
import { renderAssistantWidget } from '../assistant/widget.js';
import { askingAboutFinished } from '../core/cards.js';
import { COLUMNS, VIEWS, VIEW_LABELS } from '../core/constants.js';
import { timeline } from '../core/history.js';
import { VIEW_KEY } from '../core/keys.js';
import { dealCards, focusCardId, setCurrentView, setDealCards, setFocusCard, view } from '../core/state.js';
import { renderCatRail, renderColumn, renderTagBar } from './board.js';
import { renderDirectionLine } from './direction.js';
import { $, announce } from './dom.js';
import { renderFilterChips, renderLensRow } from './filter-menu.js';
import { renderHabitBanner, renderHabitRail } from './habits.js';
import { renderPlanRail } from './plan.js';
import { refreshEdits, refreshProposals } from './proposals.js';
import { railGrip } from './rail-resize.js';
import { renderAreas } from '../views/areas.js';
import { renderBacklog } from '../views/backlog.js';
import { renderMatrix } from '../views/matrix.js';
import { renderOverview } from '../views/overview.js';
import { hidePlotTip } from '../views/plot.js';
import { renderReview } from '../views/review.js';

// Rendering — the single entry point that paints a view, and the view switch.
// Every mutation in the app ends in render(); no other module paints #board.

/** The rail beside the board: the divider you drag it by, then the habits due,
 *  then the plan. Two sections in one column, so the day's repetitions and the
 *  day's shortlist are read in the order they are done in. */
function boardRail() {
  const frag = document.createDocumentFragment();
  // The divider first, and both of them placed into the same grid track by
  // styles.css: the grip is the rail's left edge rather than a column of its
  // own, so it costs the cards no width to paint.
  frag.appendChild(railGrip());
  const rail = document.createElement('aside');
  rail.className = 'board-rail';
  const habits = renderHabitRail();
  if (habits) rail.appendChild(habits);
  rail.appendChild(renderPlanRail());
  frag.appendChild(rail);
  return frag;
}

export function render() {
  const board = $('#board');
  board.className = view;
  // The header's controls are the board's — the filters, the category tabs,
  // the tag bar, the ⚙ Menu (theme included). Which view is showing decides
  // whether they are furniture that does nothing. Text search is not among
  // them: it is painted into the board itself, beside `+ New card`.
  document.body.dataset.view = view;
  // The `Done column` switch stands down while the range is asking
  // about finished cards — the same rule `matchesColumn` already applies to
  // the hide-Done *default*, one layer out. Without it the two halves of one
  // decision disagree: the filter correctly keeps the finished cards and the
  // stylesheet hides the only column that paints them, so "finished this
  // month" answers with a wholly empty board and two columns reading "No cards
  // match" over work the reader asked for by name. It is an override class
  // rather than a change to the switch, so the switch keeps its own state and
  // the column hides itself again the moment the span is cleared.
  document.body.classList.toggle('show-done-col', askingAboutFinished());
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
  // Which named question the board is answering, if any. Read back off the
  // filters rather than remembered, so a control moved by hand un-presses it.
  renderLensRow();
  // What is narrowing the board right now, in words, beside the `+ Filter`
  // button the controls themselves live behind. Empty when nothing is.
  renderFilterChips();
  renderHabitBanner();
  // The board's summary line, between the header and the columns: what the
  // five rules in core/direction.js found, in the order they found it. Painted
  // after the board rather than into it — it has a static slot of its own, for
  // the reason written over renderDirectionLine().
  renderDirectionLine();
  // The widget, its pill, then the tools. The tools row is the Assistant
  // sheet's alone: the rescue above parked the wired-once node back in the
  // header, the sheet claimed it again while it built, and renderAssistantTools()
  // paints its panels wherever it ended up.
  renderAssistantWidget();
  syncAssistantPill();
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
  // On a phone the switch is one line the seven tabs scroll inside (styles.css,
  // "The phone"), so the view you are on can be off the end of it — a board
  // opened on the Assistant would show a strip beginning at Board with nothing
  // pressed in it. `nearest` scrolls only when it has to and never touches the
  // page, which has its own scroll position to keep; on a wide window every
  // tab is already in view, so this does nothing at all.
  const pressed = viewButtons.find((btn) => btn.dataset.view === view);
  pressed?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  syncProposalBadge();
}

/** Whether the Ask pill is on screen. Driven from render() rather than from
 *  syncViewButtons(), because opening and closing the widget calls the former
 *  and never the latter — and a stale value here does not merely mislabel a
 *  control, it leaves the pill sitting in the corner the card has taken.
 *
 *  The pill hides for exactly as long as the conversation is on screen: behind
 *  the card, whose corner and z-index it shares, and on the Assistant view,
 *  which IS the conversation at full size and carries its own collapse control.
 *  No `aria-pressed` on it — the pill is never visible in a pressed state, and
 *  an attribute that can only ever read "false" is a control describing itself
 *  wrongly. */
function syncAssistantPill() {
  const pill = $('#assistant-launcher');
  if (pill) pill.hidden = widgetShowing() || view === 'assistant';
}

// A count on the Assistant tab, so something left waiting while the user is on
// the Board is still noticed. Absent entirely when nothing is pending.
// Proposals and suggested edits share the count: from the Board they are the
// same fact — the Assistant is waiting on you.
export function syncProposalBadge() {
  const n = assistantState.proposals.length + assistantState.edits.length;
  // Both ways into the Assistant carry the same count. The pill is in the
  // corner of every view now, so it — not the tab — is where a proposal left
  // waiting is most likely to be noticed.
  for (const btn of [viewButtons.find((b) => b.dataset.view === 'assistant'),
                     $('#assistant-launcher')]) {
    if (!btn) continue;
    let badge = btn.querySelector('.view-badge');
    if (!n) {
      if (badge) badge.remove();
      btn.removeAttribute('aria-description');
      continue;
    }
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'view-badge';
      btn.appendChild(badge);
    }
    badge.textContent = String(n);
    btn.setAttribute('aria-description',
      `${n} item${n === 1 ? '' : 's'} awaiting your approval`);
  }
}

// Where collapsing the Assistant lands. The view the user came from, and the
// Board when there is none — the Assistant is a place you go from somewhere,
// so going back to "somewhere" is the only answer that is never surprising.
// Deliberately NOT seeded from `view` here: this module sits on a cycle back
// to core/state.js, and reading that binding at evaluation time is the exact
// "Cannot access before initialization" that killed the whole page once
// already. Empty until the first switch, and the Board answers for it.
let previousView = '';
export const lastBoardView = () => previousView || 'board';

/** Switch views. Exported because the widget's expand and the sheet's collapse
 *  are view switches made from outside the view switcher. */
export function setView(next) {
  if (next === view || !VIEWS.includes(next)) return;
  if (view !== 'assistant') previousView = view;
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
