import { cardLabel, filtersActive, matchesFilters, uid } from '../core/cards.js';
import { catById, catColor, catLabel, categories } from '../core/categories.js';
import { PRIO_TITLE, TYPE_META, priorityOf } from '../core/constants.js';
import { isHabit } from '../core/habits.js';
import { commit, short } from '../core/history.js';
import { filters, nextNum, setDraggedId, state } from '../core/state.js';
import { sortMenu } from './card-actions.js';
import { openCatsDialog } from './cats-dialog.js';
import { clearDropIndicator, wireDropZone } from './dnd.js';
import { $, announce, columnCards, columnTitle } from './dom.js';
import { openDialog } from './edit-dialog.js';
import { habitCardParts } from './habits.js';
import { onCardKeydown } from './keyboard.js';
import { render } from './render.js';

// The Board view: a column and its cards, the quick-add form, the card itself,
// and the two rails — categories and tags — that filter what it shows.

export function renderColumn(col) {
  const section = document.createElement('section');
  section.className = 'column';
  section.dataset.col = col.id;
  section.setAttribute('aria-label', col.title);

  const visible = columnCards(col.id).filter(matchesFilters);

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

  if (col.id === 'inbox') section.append(renderQuickAdd());

  const cardsEl = document.createElement('div');
  cardsEl.className = 'cards';
  cardsEl.dataset.col = col.id;

  if (visible.length === 0) {
    const emptyCopy = {
      'inbox': 'Capture anything above — a question, a task, an idea',
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

export function renderQuickAdd() {
  const form = document.createElement('form');
  form.className = 'quick-add';

  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Write down anything on your mind…';
  input.setAttribute('aria-label', 'Add a card to the Inbox');

  const btn = document.createElement('button');
  btn.type = 'submit';
  btn.textContent = '+';
  btn.setAttribute('aria-label', 'Add card');

  form.append(input, btn);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const title = input.value.trim();
    if (!title) { input.focus(); return; } // nothing to add — put the cursor back
    const now = Date.now();
    // A capture inherits the drawer it was written in: with a category tab or
    // type filter active, the new card belongs there — and stays visible.
    const card = { id: uid(), columnId: 'inbox', title, notes: '',
      type: filters.type || 'question', category: filters.category,
      importance: '', urgency: '', deadline: '',
      effort: 'medium', control: 'influence', effortSrc: 'default', controlSrc: 'default',
      // Captured while filtered to habits, it is a habit: once a day until
      // the user says otherwise, rather than a habit with no cadence at all.
      habitFreq: filters.type === 'habit' ? 'daily' : '',
      habitCount: 1, habitTimes: [], habitHistory: {},
      num: nextNum(), tags: [], createdAt: now, updatedAt: now };
    // New captures go to the top of the Inbox
    const firstInbox = state.cards.findIndex((c) => c.columnId === 'inbox');
    state.cards.splice(firstInbox === -1 ? state.cards.length : firstInbox, 0, card);
    // A search, tag or priority filter could still hide the fresh card —
    // clear those so the capture never vanishes silently.
    if (!matchesFilters(card)) {
      filters.search = '';
      filters.tags.clear();
      filters.prio = '';
      $('#search').value = '';
      $('#prio-filter').value = '';
    }
    commit(`Added ${cardLabel(card)} “${short(title)}”`);
    announce(`Added “${title}” to Inbox`);
    const fresh = $('#board .quick-add input');
    if (fresh) fresh.focus();
  });
  return form;
}

// Rubber-stamp badge for a card's type — always neutral ink.
export function typeBadge(card) {
  const badge = document.createElement('span');
  badge.className = `badge type-${card.type}`;
  badge.textContent = `${TYPE_META[card.type].glyph} ${TYPE_META[card.type].label}`;
  return badge;
}

// Priority stamp (P1–P4), derived on the fly from importance × urgency.
// Null when either judgement is unset — an unjudged card wears no label.
function prioBadge(card) {
  const p = priorityOf(card);
  if (!p) return null;
  const badge = document.createElement('span');
  badge.className = 'prio-badge';
  badge.dataset.prio = String(p);
  badge.textContent = `P${p}`;
  badge.title = PRIO_TITLE[p];
  return badge;
}

// Deadline chip — flagged overdue once the date is behind today.
function deadlineChip(card) {
  const chip = document.createElement('span');
  chip.className = 'card-deadline';
  chip.textContent = card.deadline;
  if (card.deadline < new Date().toISOString().slice(0, 10)) {
    chip.dataset.overdue = 'true';
    chip.title = 'Deadline passed';
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

  const top = document.createElement('div');
  top.className = 'card-top';

  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  top.append(num);

  if (card.notes.trim()) {
    const dot = document.createElement('span');
    dot.className = 'notes-dot';
    dot.title = 'Has notes';
    dot.textContent = '¶';
    top.append(dot);
  }

  top.append(typeBadge(card));
  const prio = prioBadge(card);
  if (prio) top.append(prio);

  const title = document.createElement('p');
  title.className = 'card-title';
  title.textContent = card.title;

  el.append(top, title);

  if (card.category || card.tags.length || card.deadline) {
    const tags = document.createElement('div');
    tags.className = 'card-tags';
    if (card.deadline) tags.append(deadlineChip(card));
    if (card.category) {
      const cat = document.createElement('span');
      cat.className = 'card-cat';
      cat.textContent = catLabel(card.category);
      tags.append(cat);
    }
    for (const t of card.tags) {
      const chip = document.createElement('span');
      chip.className = 'card-tag';
      chip.textContent = t;
      tags.append(chip);
    }
    el.append(tags);
  }

  if (isHabit(card)) habitCardParts(card, el);

  el.addEventListener('click', () => openDialog(card.id));
  el.addEventListener('keydown', (e) => onCardKeydown(e, card.id));

  el.addEventListener('dragstart', (e) => {
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
