import { cardLabel, filtersActive, matchesFilters } from '../core/cards.js';
import { catById, catColor, catLabel, categories } from '../core/categories.js';
import { PRIO_TITLE, TYPE_META, priorityOf } from '../core/constants.js';
import { dueWords } from '../core/direction.js';
import { isHabit } from '../core/habits.js';
import { planConflict } from '../core/plan.js';
import { filters, setDraggedId, state } from '../core/state.js';
import { sortMenu } from './card-actions.js';
import { cardMenu, fromCardMenu, openCardMenu } from './card-menu.js';
import { childrenFold } from './children.js';
import { openCatsDialog } from './cats-dialog.js';
import { clearDropIndicator, wireDropZone } from './dnd.js';
import { $, columnCards, columnTitle } from './dom.js';
import { openDialog, openNewCard } from './edit-dialog.js';
import { onCardKeydown } from './keyboard.js';
import { render } from './render.js';

// The Board view: a column and its cards, the create control, the card itself,
// and the two rails — categories and tags — that filter what it shows.

export function renderColumn(col) {
  const section = document.createElement('section');
  section.className = 'column';
  section.dataset.col = col.id;
  section.setAttribute('aria-label', col.title);

  // A habit is not painted here. It lives in the rail beside the board, which
  // is where it is read, punched, retired and brought back — being listed in a
  // column as well was one fact told twice, in two shapes. It is still *filed*
  // in a column, and that is what says whether it is running or retired; since
  // 2026-09-06 the card dialog can change where a card is filed, which is what
  // makes hiding it here safe. Before that the column was the only way out and
  // hiding a habit in it would have stranded the card.
  //
  // The header count below is `visible.length`, so it counts what is drawn.
  const visible = columnCards(col.id).filter((c) => !isHabit(c)).filter(matchesFilters);

  const header = document.createElement('div');
  header.className = 'column-header';

  const title = document.createElement('h2');
  title.className = 'column-title';
  title.textContent = col.title;

  const count = document.createElement('span');
  count.className = 'column-count';
  count.textContent = visible.length;

  header.append(title, count);

  if (visible.length > 1) header.append(sortMenu(col.id));

  section.append(header);

  if (col.id === 'inbox') section.append(renderCaptureRow());

  const cardsEl = document.createElement('div');
  cardsEl.className = 'cards';
  cardsEl.dataset.col = col.id;

  if (visible.length === 0) {
    const emptyCopy = {
      'inbox': 'Start a card above — a question, a task, an idea',
      'in-progress': 'Drag a card here when you start on it',
      'answered': 'Finished and answered cards land here',
    };
    const hint = document.createElement('div');
    hint.className = 'empty-hint';
    hint.textContent = filtersActive() ? 'No cards match' : emptyCopy[col.id];
    cardsEl.append(hint);
  } else {
    for (const card of visible) cardsEl.append(renderCard(card));
  }

  wireDropZone(cardsEl);
  section.append(cardsEl);
  return section;
}

// The capture row, at the head of the Inbox and of the Backlog: the control
// that makes a card on the left, the box that finds one on the right. They are
// one reach — you come to the top of the Inbox either to write something down
// or to look something up — and search left the header for this row on
// 2026-09-04. Shared, so there is one of each: #new-card-btn and #search are
// ids, and only one view renders into #board at a time.
export function renderCaptureRow() {
  const row = document.createElement('div');
  row.className = 'capture-row';
  row.append(newCardButton(), searchBox());
  return row;
}

// The create control. It builds nothing: the dialog holds the one
// card-construction path, so a capture that is cancelled halfway burns no
// ledger number, writes no undo entry and leaves no Trash row behind. No
// argument — a draft with no source starts empty and inherits the drawer it
// was opened in, which is the dialog's decision to make, not this button's.
function newCardButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'new-card-btn';
  btn.className = 'new-card-btn';
  btn.textContent = '+ New card';
  btn.setAttribute('aria-label', 'Add a card to the Inbox');
  btn.addEventListener('click', () => openNewCard());
  return btn;
}

// The board's text search. It lives inside #board now, which render() wipes on
// every keystroke — so this element is not the one the next letter is typed
// into, and three things have to be carried across the repaint by hand:
//
//   * the text, which is read back from `filters.search`. That field therefore
//     holds what was *typed*, raw; matchesFilters lower-cases and trims when
//     it compares, or a trailing space would be eaten mid-word and a capital
//     undone under the caret.
//   * the focus, and
//   * the caret, because focus alone puts it at the end — which is only where
//     it was if you never move it.
function searchBox() {
  const input = document.createElement('input');
  input.type = 'search';
  input.id = 'search';
  input.className = 'board-search';
  input.placeholder = 'Search the board…';
  input.setAttribute('aria-label', 'Search the board');
  input.value = filters.search;
  input.addEventListener('input', (e) => {
    filters.search = e.target.value;
    const caret = e.target.selectionStart;
    render();
    const fresh = $('#search');
    if (!fresh) return;
    fresh.focus();
    fresh.setSelectionRange(caret, caret);
  });
  return input;
}

// Rubber-stamp badge for a card's type — always neutral ink. The Backlog's
// rows, the map's tooltip and the Review's stamped cards wear this one: they
// are lines and panels with room for a word.
export function typeBadge(card) {
  const badge = document.createElement('span');
  badge.className = `badge type-${card.type}`;
  badge.textContent = `${TYPE_META[card.type].glyph} ${TYPE_META[card.type].label}`;
  return badge;
}

// The same stamp for the board card, reduced to its glyph. A card on the board
// is a glance, and `✓ TASK` in a bordered, rotated stamp was six characters of
// chrome saying what one already says — the word is still on the badge's own
// tooltip and inside the card's accessible name, which is where a reader who
// needs it was always going to find it. Same `.badge type-<type>` classes, so
// the stamp is still the same thing wherever it is drawn.
function typeGlyph(card) {
  const badge = typeBadge(card);
  badge.classList.add('glyph-only');
  badge.textContent = TYPE_META[card.type].glyph;
  badge.title = TYPE_META[card.type].label;
  return badge;
}

// Priority on the board card: `P1` alone — urgent *and* important, derived on
// the fly from the two judgements. Null for everything else, which is most of
// the board.
//
// It was a bare `!` first, which is what the design drew, and the two marks
// side by side on a real card is what corrected it: `!` is the *problem* type's
// own glyph (TYPE_META), so a problem that was also urgent and important wore
// `! !` — two identical marks meaning two different things, next to each other.
// `P1` is the name the priority filter and the Matrix already give it.
//
// It used to paint P1–P4 on every judged card, which meant the commonest thing
// printed at the top of a card was a coloured pill saying the card could wait.
// P2–P4 are what the Matrix view is for: it reads the same two fields and is
// *about* the comparison. The stamp keeps its class and its `data-prio` so
// nothing downstream has to learn a second name for it, and an unjudged card
// still wears no label at all.
function prioBadge(card) {
  const p = priorityOf(card);
  if (p !== 1) return null;
  const badge = document.createElement('span');
  badge.className = 'prio-badge';
  badge.dataset.prio = String(p);
  badge.textContent = `P${p}`;
  badge.title = PRIO_TITLE[p];
  return badge;
}

// Plan chip — when the card is meant to happen, at the precision it was
// planned at ('2028', '2028-03', '2028-03-04'). Marked when the plan starts
// after the deadline, which is the one pair the dialog refuses to save: a card
// can still be holding it (an import, a server board, an older browser), and
// it has to be visible to be fixable.
function planChip(card) {
  const chip = document.createElement('span');
  chip.className = 'card-plan';
  chip.textContent = `→ ${card.plan}`;
  if (planConflict(card.plan, card.deadline)) {
    chip.dataset.conflict = 'true';
    chip.title = `Planned after the deadline (${card.deadline})`;
  } else {
    chip.title = 'Planned for ' + card.plan;
  }
  return chip;
}

// Deadline chip — the *reading* rather than the stored value: `overdue by 2
// days`, `due today`, `due in 4 days`, and the date itself once it is further
// off than a week. `2026-09-09` is a fact you have to do arithmetic on before
// it means anything, and the arithmetic was already written: `dueWords` is
// core/direction.js's own sentence, so the card and the line at the head of
// the board can never say two different things about one date. Still flagged
// in red pencil once the day has passed.
function deadlineChip(card) {
  const chip = document.createElement('span');
  chip.className = 'card-deadline';
  chip.textContent = dueWords(card.deadline);
  chip.title = `Deadline ${card.deadline}`;
  if (card.deadline < new Date().toISOString().slice(0, 10)) {
    chip.dataset.overdue = 'true';
    chip.title = `Deadline passed — ${card.deadline}`;
  }
  return chip;
}

export function cardAria(card) {
  const cat = card.category ? `, ${catLabel(card.category)}` : '';
  return `${cardLabel(card)}: ${card.title} — ${TYPE_META[card.type].label}${cat}, in ${columnTitle(card.columnId)}`;
}

function renderCard(card) {
  const el = document.createElement('article');
  el.className = 'card';
  el.dataset.id = card.id;
  el.draggable = true;
  el.tabIndex = 0;
  el.style.setProperty('--cat', catColor(card.category));
  if (card.category) el.classList.add('categorized');
  el.setAttribute('aria-label', cardAria(card));

  // The card says four things: whose life area it belongs to (the coloured
  // spine, which has said it since the first commit), what it is, when it is
  // due, and which card it is. The words come first and everything else is one
  // quiet line under them — a card on the board is a glance, and the record is
  // the dialog, which is already a good one.
  //
  // What left this face, and where it went:
  //
  //   * the type's word, to the glyph's tooltip and the accessible name;
  //   * P2–P4, to the Matrix, which is the view *about* that comparison;
  //   * the category's name, to the spine that was already drawing it;
  //   * every tag, to the dialog and to the tag filters — a tag is how you
  //     find a card, not what you need to know once you have found it;
  //   * the raw deadline, to the reading of it (`deadlineChip`).
  //
  // Nothing moved to a hover: the actions `+` still occupies layout and still
  // answers Tab, for the reason written over `.card-menu-btn` in styles.css.
  const title = document.createElement('p');
  title.className = 'card-title';
  title.textContent = card.title;
  el.append(title);

  const line = document.createElement('div');
  line.className = 'card-line';
  line.append(typeGlyph(card));
  const prio = prioBadge(card);
  if (prio) line.append(prio);
  if (card.deadline) line.append(deadlineChip(card));
  // Only when it says something the deadline chip does not: while the plan is
  // simply following the deadline, one chip is the honest count.
  const plan = card.type === 'habit' ? '' : card.plan;
  if (plan && plan !== card.deadline) line.append(planChip(card));
  if (card.notes.trim()) {
    const dot = document.createElement('span');
    dot.className = 'notes-dot';
    dot.title = 'Has notes';
    dot.textContent = '¶';
    line.append(dot);
  }
  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  line.append(num);
  // Last on the line: the card's actions belong beside its other marks rather
  // than in a corner of their own.
  line.append(cardMenu(card));
  el.append(line);

  // The smaller cards this one is made of, at the foot and closed by default —
  // null for a childless card, which is therefore no taller than it was.
  const kids = childrenFold(card);
  if (kids) el.append(kids);

  // The whole card is one click target, and the actions menu now lives inside
  // it: a click, a key or a right-click the menu owns must not also do the
  // card's thing. Without these four guards the + opens the edit dialog behind
  // its own panel, Enter on the + does the same, a right-click on an open panel
  // repaints it back to its root and throws away the submenu the user had just
  // stepped into, and a press on a menu row drags the card out of its column.
  //
  // The children fold needs the same four, and it is not part of the menu: its
  // summary is a click and a keypress of its own, and each row is a card with
  // its own dialog and its own right-click. `fromChildren` is that test, and
  // the rows stop their own click besides — a row must open the child, not the
  // parent behind it.
  const fromChildren = (e) => Boolean(e.target.closest?.('.card-children-inline'));
  el.addEventListener('click', (e) => {
    if (fromCardMenu(e) || fromChildren(e)) return;
    openDialog(card.id);
  });
  el.addEventListener('keydown', (e) => {
    if (fromCardMenu(e) || fromChildren(e)) return;
    onCardKeydown(e, card.id);
  });

  // Right-click is an accelerator onto the menu the + opens — the same panel,
  // in the same place beside the +, since settle() measures the column scroller
  // to decide which way to flip and following the pointer would be a second
  // positioning system for one panel. Repeated right-clicks leave it open: the
  // toggle belongs to the button, which has an aria-expanded to keep honest.
  el.addEventListener('contextmenu', (e) => {
    if (fromCardMenu(e) || fromChildren(e)) return;
    e.preventDefault();
    openCardMenu(card.id, el.querySelector('.card-menu'));
  });

  el.addEventListener('dragstart', (e) => {
    if (fromCardMenu(e) || fromChildren(e)) { e.preventDefault(); return; }
    setDraggedId(card.id);
    e.dataTransfer.setData('text/plain', card.id);
    e.dataTransfer.effectAllowed = 'move';
    requestAnimationFrame(() => el.classList.add('dragging'));
  });
  el.addEventListener('dragend', () => {
    setDraggedId(null);
    el.classList.remove('dragging');
    clearDropIndicator();
    document.querySelectorAll('.cards.drop-target').forEach((z) => z.classList.remove('drop-target'));
  });

  return el;
}

// The category rail — coloured index-tabs, one per life area. "All" opens
// every drawer at once (the whole-life visualization); clicking a category
// filters the board to that drawer; ✎ Edit manages the registry itself.
export function renderCatRail() {
  const rail = $('#cat-rail');
  if (filters.category && !catById(filters.category)) filters.category = '';
  rail.hidden = false;
  rail.innerHTML = '';

  const mkTab = (id, label, color, extraClass = '') => {
    const tab = document.createElement('button');
    tab.className = ('cat-tab ' + extraClass).trim();
    tab.dataset.cat = id;
    tab.style.setProperty('--cat', color);
    tab.setAttribute('aria-pressed', String(filters.category === id));
    tab.textContent = label;
    tab.addEventListener('click', () => {
      filters.category = filters.category === id ? '' : id;
      render();
    });
    return tab;
  };

  rail.append(mkTab('', 'All', 'var(--ink)', 'cat-tab-all'));
  for (const cat of categories) rail.append(mkTab(cat.id, cat.label, catColor(cat.id)));

  const edit = document.createElement('button');
  edit.id = 'edit-cats-btn';
  edit.className = 'cat-tab cat-tab-edit';
  edit.title = 'Add or remove categories';
  edit.textContent = '✎ Edit';
  edit.addEventListener('click', openCatsDialog);
  rail.append(edit);
}

export function renderTagBar() {
  const bar = $('#tag-bar');
  const allTags = [...new Set(state.cards.flatMap((c) => c.tags))].sort();
  // Drop filters for tags that no longer exist
  for (const t of [...filters.tags]) if (!allTags.includes(t)) filters.tags.delete(t);

  // The dropdown twin: same tags as the bar, one pick at a time. Repainted
  // here because this is the one place that already knows every tag; its
  // value mirrors the Set only when the Set is a single tag — the bar can
  // build combinations the dropdown has no word for.
  const dropdown = $('#tag-filter');
  if (dropdown) {
    dropdown.replaceChildren(
      new Option('All tags', ''),
      ...allTags.map((t) => new Option('#' + t, t)),
    );
    dropdown.value = filters.tags.size === 1 ? [...filters.tags][0] : '';
  }

  bar.hidden = allTags.length === 0;
  bar.innerHTML = '';
  if (!allTags.length) return;

  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = 'Tags:';
  bar.append(label);

  for (const tag of allTags) {
    const chip = document.createElement('button');
    chip.className = 'tag-chip';
    chip.textContent = tag;
    chip.setAttribute('aria-pressed', String(filters.tags.has(tag)));
    chip.addEventListener('click', () => {
      filters.tags.has(tag) ? filters.tags.delete(tag) : filters.tags.add(tag);
      render();
    });
    bar.append(chip);
  }
}
