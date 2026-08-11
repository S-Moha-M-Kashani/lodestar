import { CAT_LIMIT, HUE_CHOICES, catById, catColor, catLabel, catSlug, categories, setCategories } from '../core/categories.js';
import { commit } from '../core/history.js';
import { filters, state } from '../core/state.js';
import { ask } from './dialogs.js';
import { $, announce } from './dom.js';

// Categories editor — the ✎ tab on the rail. Add a life area (name + hue) or
// remove one; removing never touches cards, they just become uncategorized.

const catsDialog = $('#cats-dialog');

export function openCatsDialog() {
  renderCatsList();
  renderHuePicker();
  $('#cat-add-name').value = '';
  catsDialog.showModal();
  $('#cat-add-name').focus();
}

function renderCatsList() {
  const list = $('#cats-list');
  list.innerHTML = '';
  const counts = new Map();
  for (const c of state.cards) if (c.category) counts.set(c.category, (counts.get(c.category) || 0) + 1);

  for (const cat of categories) {
    const row = document.createElement('div');
    row.className = 'cats-row';

    const swatch = document.createElement('span');
    swatch.className = 'cat-swatch';
    swatch.style.setProperty('--cat', catColor(cat.id));
    swatch.textContent = cat.label;

    const n = counts.get(cat.id) || 0;
    const meta = document.createElement('span');
    meta.className = 'cats-row-count';
    meta.textContent = n ? `${n} card${n === 1 ? '' : 's'}` : 'no cards';

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn ghost cats-remove';
    remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove category ${cat.label}`);
    remove.addEventListener('click', () => removeCategory(cat.id));

    row.append(swatch, meta, remove);
    list.append(row);
  }
}

function renderHuePicker() {
  const wrap = $('#cat-hue-options');
  wrap.innerHTML = '';
  const used = new Set(categories.map((c) => c.h));
  const preferred = HUE_CHOICES.find((h) => !used.has(h)) ?? HUE_CHOICES[0];
  for (const h of HUE_CHOICES) {
    const label = document.createElement('label');
    label.className = 'cat-hue';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'cat-hue';
    input.value = String(h);
    input.checked = h === preferred;
    input.setAttribute('aria-label', `Hue ${h}`);
    const dot = document.createElement('span');
    dot.className = 'cat-hue-dot';
    dot.style.setProperty('--cat', `oklch(var(--cat-l) var(--cat-c) ${h})`);
    label.append(input, dot);
    wrap.append(label);
  }
}

$('#cat-add-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const nameInput = $('#cat-add-name');
  const label = nameInput.value.trim().slice(0, 24);
  const id = catSlug(label);
  if (!label || !id) { nameInput.focus(); return; }
  if (catById(id)) {
    ask({ title: 'Already on the rail', message: `A category called “${catLabel(id)}” already exists.`, cancelLabel: null });
    return;
  }
  if (categories.length >= CAT_LIMIT) {
    ask({ title: 'The rail is full', message: `Up to ${CAT_LIMIT} categories fit — remove one to make room.`, cancelLabel: null });
    return;
  }
  const checked = $('#cat-hue-options input:checked');
  const h = checked ? Number(checked.value) : HUE_CHOICES[0];
  categories.push({ id, label, h });
  commit(`Added category “${label}”`);
  announce(`Added category “${label}”`);
  renderCatsList();
  renderHuePicker();
  nameInput.value = '';
  nameInput.focus();
});

async function removeCategory(id) {
  const cat = catById(id);
  if (!cat) return;
  const affected = state.cards.filter((c) => c.category === id).length;
  const sure = await ask({
    title: `Remove “${cat.label}”?`,
    message: affected
      ? `${affected} card${affected === 1 ? '' : 's'} carry this label — they stay on the board and become uncategorized. You can add the category back any time.`
      : 'No cards use it. You can add it back any time.',
    okLabel: 'Remove category',
    danger: true,
  });
  if (!sure) return;
  setCategories(categories.filter((c) => c.id !== id));
  if (filters.category === id) filters.category = '';
  const now = Date.now();
  for (const c of state.cards) if (c.category === id) { c.category = ''; c.updatedAt = now; }
  commit(`Removed category “${cat.label}”`);
  announce(`Removed category “${cat.label}”`);
  renderCatsList();
  renderHuePicker();
}

$('#close-cats').addEventListener('click', () => catsDialog.close());
