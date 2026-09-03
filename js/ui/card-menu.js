import { cardLabel, catVal, deadlineVal, moveCard, typeVal } from '../core/cards.js';
import { catColor, catLabel, categories } from '../core/categories.js';
import { COLUMNS, TYPES, TYPE_META } from '../core/constants.js';
import { planConflict, planDay } from '../core/plan.js';
import { pad2 } from '../core/habits.js';
import { commit, short } from '../core/history.js';
import { setFocusCard } from '../core/state.js';
import { deleteCard } from './card-actions.js';
import { announce, columnTitle, getCard } from './dom.js';
import { openDialog, openNewCard } from './edit-dialog.js';

// The card's own actions menu — the + that sits in .card-top beside the ledger
// number and the stamps, and the panel it drops.
//
// Eight entries, seven on a habit — which repeats on its own calendar and so
// has no plan to set. Edit opens the card dialog, exactly as clicking the card
// does; Duplicate opens that same dialog on an unsaved copy, so the copy costs
// no ledger number until it is saved; Delete is the ordinary soft delete; and
// the four in between are quick-edits that change one field in place. Those four exist because re-filing a card is
// the most common thing anyone does to it, and routing "this is Health, not
// Work" through a modal with nine other fields in it makes a one-word decision
// cost a form.
//
// One panel with two levels rather than hover-revealed submenus: a submenu you
// have to keep the pointer inside is unreachable by touch, and this control is
// on every card. Picking Category ▸ repaints the same panel with the categories
// and a ‹ Back — one tap in, one tap out, and the whole thing works from the
// keyboard with nothing but Tab and Enter.
//
// The panel is markup the toolbar already owns (.menu-panel / .menu-item), so
// this is not a second dropdown idiom: it dismisses on an outside click, treats
// a click inside as use, and closes on Escape with focus back on its button.

const OPEN_PANEL = '.card-menu-panel:not([hidden])';

/** Did this event happen inside a card's actions menu?
 *
 *  composedPath(), and deliberately not `e.target.closest('.card-menu')`.
 *  Picking "Category ▸" repaints the panel from inside the clicked row's own
 *  handler, which detaches that row before the event has finished bubbling —
 *  and a detached node has no ancestors, so closest() answers null for a click
 *  that plainly came from the menu. Every listener that has to tell "the menu"
 *  from "the card underneath it" asks through here, because both of them got
 *  it wrong: the dismisser shut the submenu it had just opened, and the card
 *  opened its edit dialog behind the panel. The path is recorded when the
 *  event is dispatched, so it still names where the click came from. */
export const fromCardMenu = (e) =>
  e.composedPath().some((n) => n.nodeType === 1 && n.matches('.card-menu'));

/** Shut whichever card menu is open. At most one ever is — openMenu closes the
 *  others first — and a repaint of the board takes any open panel with it,
 *  since the panel is a child of the card element being replaced. */
function closeCardMenus() {
  for (const panel of document.querySelectorAll(OPEN_PANEL)) {
    panel.hidden = true;
    const wrap = panel.closest('.card-menu');
    wrap.querySelector('.card-menu-btn').setAttribute('aria-expanded', 'false');
    wrap.closest('.card')?.classList.remove('menu-open');
  }
}

// The two ways out every menu in this app has. Registered once here, at module
// scope, rather than per card: the board is rebuilt on every render and a
// listener added in cardMenu() would be added again for every card, on every
// repaint, forever. Registering a listener while a module evaluates is safe;
// calling into the state modules is not, and nothing below runs until a click.
document.addEventListener('click', (e) => {
  // Two things are not "elsewhere": inside this menu, which is use, and inside
  // a <dialog> — Delete opens one, centred on the screen and therefore outside
  // the card it belongs to.
  if (fromCardMenu(e)) return;
  if (e.composedPath().some((n) => n.nodeType === 1 && n.matches('dialog'))) return;
  closeCardMenus();
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const panel = document.querySelector(OPEN_PANEL);
  if (!panel) return;
  const btn = panel.closest('.card-menu').querySelector('.card-menu-btn');
  closeCardMenus();
  btn.focus();
});

/** One row of the panel. `current` marks the value the card already holds. */
function mkItem(label, onPick, { current = false, danger = false, cat = null } = {}) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'menu-item card-menu-item' + (danger ? ' is-danger' : '');
  if (cat !== null) {
    const dot = document.createElement('span');
    dot.className = 'card-menu-dot';
    dot.style.setProperty('--cat', catColor(cat));
    btn.append(dot);
  }
  btn.append(document.createTextNode(label));
  if (current) {
    btn.setAttribute('aria-current', 'true');
    const tick = document.createElement('span');
    tick.className = 'card-menu-tick';
    tick.textContent = '✓';
    btn.append(tick);
  }
  btn.addEventListener('click', onPick);
  return btn;
}

const GAP = 6; // the offset in .card-menu-panel's top/bottom, in px

/** Put a freshly painted panel where it fits, and focus into it.
 *
 *  A column is a scroller (`.cards { overflow-y: auto }`), so this menu has to
 *  fit inside one or be cut off by it — which is the one thing an absolutely
 *  positioned panel inside a card gets wrong, in both directions. Dropping
 *  below the + is right only while there is room below; flipping above it is
 *  right only while there is more room there, and flipping blind is how the
 *  ‹ Back row ended up sliced off the top of a short Inbox. So: measure both
 *  sides, take the better one, and cap the panel to the room it actually has —
 *  a longer list then scrolls inside the panel instead of vanishing under the
 *  edge of the column. The cap lives here rather than in styles.css because
 *  there is no fixed height that is right in a column whose height is the
 *  window's. */
function settle(panel) {
  if (panel.hidden) return;
  const scroller = panel.closest('.cards');
  const bounds = scroller
    ? scroller.getBoundingClientRect()
    : { top: 0, bottom: window.innerHeight };
  const anchor = panel.parentElement.querySelector('.card-menu-btn').getBoundingClientRect();
  const below = bounds.bottom - anchor.bottom - GAP;
  const above = anchor.top - bounds.top - GAP;

  panel.classList.remove('up');
  panel.style.maxHeight = ''; // measure the list at its natural height
  const up = panel.scrollHeight > below && above > below;
  panel.classList.toggle('up', up);
  panel.style.maxHeight = `${Math.max(0, Math.round(up ? above : below))}px`;

  // Repainting the panel destroyed the row that was clicked, and with it the
  // focus that was on it — without this a keyboard user drops out of the menu
  // the moment they step into a submenu. No-op while the panel is still hidden,
  // which is the state paintRoot is first called in.
  panel.querySelector('.menu-item')?.focus();
}

// --------------------------------------------------------------------------
// What the entries do
// --------------------------------------------------------------------------

/** Change one field and commit. Every mutation in the app goes through
 *  commit(), which saves locally and debounces the whole-board PUT; nothing
 *  here writes a card any other way. */
function quickEdit(cardId, change, what) {
  const card = getCard(cardId);
  if (!card) return;
  closeCardMenus();
  change(card);
  card.updatedAt = Date.now();
  // The commit repaints the board and destroys this card's element with it, so
  // say where focus belongs first — otherwise a keyboard user is dropped at the
  // top of the page by every quick-edit they make.
  setFocusCard(cardId);
  const message = `Set ${cardLabel(card)} “${short(card.title)}” to ${what}`;
  commit(message);
  announce(message);
}

function quickMove(cardId, columnId) {
  const card = getCard(cardId);
  if (!card) return;
  closeCardMenus();
  setFocusCard(cardId);
  moveCard(cardId, columnId); // writes its own timeline entry
  announce(`Moved “${card.title}” to ${columnTitle(columnId)}`);
}

// Relative, because that is how a deadline is decided — "by the end of the
// week", not "the 19th". The date is computed when the entry is picked, so a
// board left open overnight never sets yesterday.
const DEADLINES = [['Today', 0], ['Tomorrow', 1], ['In a week', 7], ['In a month', 30]];
const isoIn = (days) => {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
};

// --------------------------------------------------------------------------
// The two levels
// --------------------------------------------------------------------------

function paintRoot(cardId, panel) {
  const card = getCard(cardId);
  panel.replaceChildren(
    mkItem('Edit…', () => { closeCardMenus(); openDialog(cardId); }),
    // Nothing is created here: openNewCard opens the dialog on a draft copy,
    // and the second card exists only once the user saves it — so a cancelled
    // duplicate burns no ledger number and leaves no Trash entry to clean up.
    // The card is re-read when the entry is picked rather than taken from the
    // paint above, the way every sub-level below does it: a quick-edit repaints
    // this panel, and copying the card as it was drawn would copy a stale field.
    mkItem('Duplicate', () => {
      const fresh = getCard(cardId);
      if (!fresh) return;
      closeCardMenus();
      openNewCard(fresh);
    }),
    mkItem('Category ▸', () => paintCategories(cardId, panel)),
    mkItem('Type ▸', () => paintTypes(cardId, panel)),
    mkItem('Deadline ▸', () => paintDeadlines(cardId, panel)),
    // A habit repeats on its own calendar and has no plan to set.
    ...(card && card.type === 'habit' ? [] : [mkItem('Plan ▸', () => paintPlans(cardId, panel))]),
    mkItem('Move to ▸', () => paintColumns(cardId, panel)),
    mkItem('Delete', () => { closeCardMenus(); deleteCard(cardId); }, { danger: true }),
  );
  settle(panel);
}

function paintSub(cardId, panel, rows) {
  panel.replaceChildren(mkItem('‹ Back', () => paintRoot(cardId, panel)), ...rows);
  settle(panel);
}

function paintCategories(cardId, panel) {
  const card = getCard(cardId);
  if (!card) return;
  // Read off the live registry, not a copy taken at boot: categories are the
  // user's to add and remove while the board is open.
  const rows = [
    mkItem('No category', () => quickEdit(cardId, (c) => { c.category = ''; }, 'no category'),
      { current: !card.category, cat: '' }),
    ...categories.map((cat) => mkItem(
      cat.label,
      () => quickEdit(cardId, (c) => { c.category = catVal(cat.id); }, catLabel(cat.id)),
      { current: card.category === cat.id, cat: cat.id },
    )),
  ];
  paintSub(cardId, panel, rows);
}

function paintTypes(cardId, panel) {
  const card = getCard(cardId);
  if (!card) return;
  const rows = TYPES.map((t) => mkItem(
    `${TYPE_META[t].glyph} ${TYPE_META[t].label}`,
    () => quickEdit(cardId, (c) => {
      c.type = typeVal(t);
      // Stamping Habit for the first time gives the card a cadence, the same
      // default the dialog uses. Nothing else about the habit is touched:
      // habitCount, habitTimes and habitHistory are validated unconditionally
      // and are never coupled to the card's type, so a card mis-stamped a task
      // and stamped back is the same habit with the same record. This is the
      // fastest way in the app to mis-stamp one, which is exactly why it must
      // not be the way a year of completions is lost.
      if (c.type === 'habit' && !c.habitFreq) c.habitFreq = 'daily';
    }, TYPE_META[t].label),
    { current: card.type === t },
  ));
  paintSub(cardId, panel, rows);
}

function paintDeadlines(cardId, panel) {
  const card = getCard(cardId);
  if (!card) return;
  const rows = [
    ...DEADLINES.map(([label, days]) => {
      const date = isoIn(days);
      return mkItem(label,
        () => quickEdit(cardId, (c) => {
          c.deadline = deadlineVal(date);
          // While the plan is following the deadline it moves with it.
          if (c.planSrc !== 'user' && c.planSrc !== 'ai') c.plan = c.deadline;
        }, date),
        { current: card.deadline === date });
    }),
    mkItem('No deadline', () => quickEdit(cardId, (c) => {
      c.deadline = '';
      if (c.planSrc !== 'user' && c.planSrc !== 'ai') c.plan = '';
    }, 'no deadline'),
      { current: !card.deadline }),
  ];
  paintSub(cardId, panel, rows);
}

// The plan, at the three precisions the dialog offers, computed when the entry
// is picked so a board left open overnight never plans for yesterday. Each
// entry refuses rather than committing when it would land after the deadline —
// the same rule the dialog enforces, in the faster way in.
function paintPlans(cardId, panel) {
  const card = getCard(cardId);
  if (!card) return;
  const today = planDay();
  const choices = [
    ['Today', today],
    ['This month', today.slice(0, 7)],
    ['This year', today.slice(0, 4)],
    ['Next year', String(Number(today.slice(0, 4)) + 1)],
  ];
  const rows = [
    ...choices.map(([label, plan]) => mkItem(
      label,
      () => {
        if (planConflict(plan, card.deadline)) {
          closeCardMenus();
          announce(`“${short(card.title)}” is due ${card.deadline} — a plan for ${plan} would start after that.`);
          return;
        }
        quickEdit(cardId, (c) => { c.plan = plan; c.planSrc = 'user'; }, `plan ${plan}`);
      },
      { current: card.plan === plan },
    )),
    mkItem('No plan', () => quickEdit(cardId, (c) => { c.plan = ''; c.planSrc = 'user'; }, 'no plan'),
      { current: !card.plan && card.planSrc === 'user' }),
    mkItem('Follow the deadline',
      () => quickEdit(cardId, (c) => { c.planSrc = 'auto'; c.plan = c.deadline; }, 'the deadline'),
      { current: card.planSrc === 'auto' }),
  ];
  paintSub(cardId, panel, rows);
}

function paintColumns(cardId, panel) {
  const card = getCard(cardId);
  if (!card) return;
  const rows = COLUMNS.map((col) => mkItem(
    col.title,
    () => quickMove(cardId, col.id),
    { current: card.columnId === col.id },
  ));
  paintSub(cardId, panel, rows);
}

// --------------------------------------------------------------------------
// The control itself
// --------------------------------------------------------------------------

/** The + and its panel, for one card. Built fresh on every render, like the
 *  card around it; the panel's contents are painted when it opens, so they are
 *  read off the card as it stands rather than as it stood when it was drawn. */
export function cardMenu(card) {
  const wrap = document.createElement('span');
  wrap.className = 'card-menu';
  // The card is draggable; a press on its menu must not start dragging it.
  wrap.draggable = false;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'card-menu-btn';
  btn.textContent = '+';
  btn.title = 'Card actions';
  btn.setAttribute('aria-haspopup', 'true');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', `Actions for ${cardLabel(card)}`);

  const panel = document.createElement('div');
  panel.className = 'menu-panel card-menu-panel';
  panel.hidden = true;

  // The toggle stays here, on the button, and deliberately did not move into
  // openCardMenu. A button that says "expanded" has to be able to say the
  // opposite when it is pressed again; a right-click has no such promise to
  // keep, and a second right-click on the same card that closed the menu would
  // read as the card flickering. So: pressing the + twice shuts it, and
  // right-clicking twice leaves it open.
  btn.addEventListener('click', () => {
    if (!panel.hidden) { closeCardMenus(); return; }
    openCardMenu(card.id, wrap);
  });

  wrap.append(btn, panel);
  return wrap;
}

/** Open one card's menu at its root level. `wrap` is the card's `.card-menu`.
 *
 *  Exported for the Board's right-click, which finds the wrapper inside the
 *  card element rather than holding the closure cardMenu() built — so the
 *  button and the panel are derived from the wrapper here instead of being
 *  passed in, and there is one open path however the menu was asked for. The
 *  panel keeps its place beside the + rather than following the pointer:
 *  settle() measures the enclosing column scroller to decide which way to
 *  flip, and a second positioning system for one panel is not worth a menu
 *  that appears somewhere new each time. */
export function openCardMenu(cardId, wrap) {
  const btn = wrap.querySelector('.card-menu-btn');
  const panel = wrap.querySelector('.menu-panel');
  closeCardMenus(); // one menu at a time, whichever card it belonged to
  paintRoot(cardId, panel);
  panel.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  wrap.closest('.card')?.classList.add('menu-open');
  settle(panel); // positions it, and puts focus on the first entry
}
