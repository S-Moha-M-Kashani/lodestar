import { cardLabel } from '../core/cards.js';
import { catColor, categories } from '../core/categories.js';
import { commit } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { state } from '../core/state.js';
import { typeBadge } from '../ui/board.js';
import { deleteCard } from '../ui/card-actions.js';
import { announce, columnCards, getCard } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
import { render } from '../ui/render.js';
import { areaRow, detailPanel, humanAge, isOpen } from './areas.js';
import { DAY } from './matrix.js';
import { mulberry32 } from './overview.js';

// Review view — GTD's weekly review as a screen: the ritual that keeps every
// other view trustworthy. Stat tiles, week-over-week drift per area, the
// neglect list, and three resurfaced old thoughts (deterministic per day, so
// the same ritual shows the same cards all day).

const REVIEW_KEY = KEY_PREFIX + 'reviewed';
const RESURFACE_KEY = KEY_PREFIX + 'resurface';
let resurfacePicks = { date: '', ids: [] };
try {
  const saved = JSON.parse(localStorage.getItem(RESURFACE_KEY) || 'null');
  if (saved && typeof saved.date === 'string' && Array.isArray(saved.ids)) resurfacePicks = saved;
} catch (_) { /* private mode */ }

const startOfToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); };
const dateSeed = (key) => {
  let seed = 0;
  for (const ch of key) seed = (Math.imul(seed, 31) + ch.charCodeAt(0)) | 0;
  return seed;
};

// Weighted sample of 3 open cards, biased stale × important, seeded on the
// date — a Readwise-style daily re-encounter. Picks are pinned for the day
// so acting on one ("Still matters") doesn't reshuffle the other two.
function resurfaceToday() {
  const open = state.cards.filter(isOpen);
  const d = new Date();
  const dateKey = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  // Today's picks are pinned (and stored) — acting on one, or even trashing
  // it, never reshuffles the others until tomorrow.
  if (resurfacePicks.date === dateKey && resurfacePicks.ids.length) {
    return resurfacePicks.ids.map((id) => open.find((c) => c.id === id)).filter(Boolean);
  }
  const rand = mulberry32(dateSeed(dateKey));
  const pool = open.map((c) => ({
    c,
    w: Math.max(1, (Date.now() - c.updatedAt) / DAY) * (c.importance === 'high' ? 3 : 1),
  }));
  const picks = [];
  while (picks.length < 3 && pool.length) {
    let r = rand() * pool.reduce((s, e) => s + e.w, 0);
    let idx = 0;
    for (; idx < pool.length - 1; idx++) { r -= pool[idx].w; if (r <= 0) break; }
    picks.push(pool[idx].c);
    pool.splice(idx, 1);
  }
  resurfacePicks = { date: dateKey, ids: picks.map((c) => c.id) };
  try { localStorage.setItem(RESURFACE_KEY, JSON.stringify(resurfacePicks)); } catch (_) { /* private mode */ }
  return picks;
}

function renderResurfaceCard(card) {
  const el = document.createElement('div');
  el.className = 'resurface-card';
  el.dataset.id = card.id;
  el.style.setProperty('--cat', catColor(card.category));
  const keptToday = card.updatedAt >= startOfToday();
  if (keptToday) el.dataset.kept = 'true';

  const head = document.createElement('div');
  head.className = 'resurface-head';
  const num = document.createElement('span');
  num.className = 'card-num';
  num.textContent = cardLabel(card);
  head.append(num, typeBadge(card));
  const age = document.createElement('span');
  age.className = 'resurface-age';
  const ms = Date.now() - card.updatedAt;
  age.textContent = ms < DAY ? 'touched today' : `${humanAge(ms)} untouched`;
  head.append(age);

  const title = document.createElement('p');
  title.className = 'resurface-title';
  title.textContent = card.title;

  const actions = document.createElement('div');
  actions.className = 'resurface-actions';
  const keep = document.createElement('button');
  keep.type = 'button';
  keep.className = 'btn ghost';
  if (keptToday) { keep.textContent = '✓ kept'; keep.disabled = true; }
  else {
    keep.textContent = 'Still matters';
    keep.addEventListener('click', () => {
      const c = getCard(card.id);
      if (!c) return;
      c.updatedAt = Date.now();
      commit(`Reviewed ${cardLabel(c)} — still matters`);
      announce(`Kept “${c.title}” — freshness stamped`);
    });
  }
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'btn ghost';
  openBtn.textContent = 'Open';
  openBtn.addEventListener('click', () => openDialog(card.id));
  const trash = document.createElement('button');
  trash.type = 'button';
  trash.className = 'btn ghost';
  trash.textContent = 'To Trash';
  trash.addEventListener('click', () => deleteCard(card.id)); // existing soft-delete path — durability promise intact
  actions.append(keep, openBtn, trash);

  el.append(head, title, actions);
  return el;
}

export function renderReview() {
  const sheet = document.createElement('div');
  sheet.className = 'plot-sheet review-sheet';
  const now = Date.now();
  const open = state.cards.filter(isOpen);
  const wk1 = now - 7 * DAY, wk2 = now - 14 * DAY;
  const answeredIn = (from, to) => state.cards.filter((c) => !isOpen(c) && c.updatedAt >= from && c.updatedAt < to);
  const createdIn = (from, to) => state.cards.filter((c) => c.createdAt >= from && c.createdAt < to);

  let lastReviewedAt = 0;
  try { lastReviewedAt = Number(localStorage.getItem(REVIEW_KEY)) || 0; } catch (_) { /* private mode */ }

  const head = document.createElement('div');
  head.className = 'plot-head';
  const title = document.createElement('h2');
  title.className = 'plot-title';
  title.textContent = 'Review';
  const caption = document.createElement('p');
  caption.className = 'plot-caption';
  caption.textContent = 'The weekly sweep that keeps every other view honest — clear the inbox, notice the drift, re-meet three old thoughts, stamp it done.';
  const status = document.createElement('p');
  status.className = 'plot-status';
  status.textContent = lastReviewedAt
    ? (now - lastReviewedAt < DAY ? 'last reviewed today' : `last reviewed ${humanAge(now - lastReviewedAt)} ago`)
    : 'never stamped — make this the first review';
  head.append(title, caption, status);
  sheet.append(head);

  const tiles = document.createElement('div');
  tiles.className = 'review-tiles';
  const stat = (key, value, label) => {
    const t = document.createElement('div');
    t.className = 'review-tile';
    t.dataset.stat = key;
    const n = document.createElement('span');
    n.className = 'review-num';
    n.textContent = String(value);
    const l = document.createElement('span');
    l.className = 'review-label';
    l.textContent = label;
    t.append(n, l);
    return t;
  };
  tiles.append(
    stat('inbox', columnCards('inbox').length, 'in the inbox'),
    stat('answered-week', answeredIn(wk1, Infinity).length, 'answered this week'),
    stat('new-week', createdIn(wk1, Infinity).length, 'new this week'),
    stat('open', open.length, 'open in total'),
  );
  sheet.append(tiles);

  // Week-over-week drift per life area.
  const inUse = categories.filter((c) => state.cards.some((k) => k.category === c.id));
  if (inUse.length) {
    const deltas = detailPanel('Week over week', 'New and answered per area — this week against last.');
    deltas.classList.add('review-deltas');
    const arrow = (a, b) => (a > b ? '▲' : a < b ? '▼' : '·');
    for (const cat of inUse) {
      const of = (list) => list.filter((c) => c.category === cat.id).length;
      const cThis = of(createdIn(wk1, Infinity)), cLast = of(createdIn(wk2, wk1));
      const aThis = of(answeredIn(wk1, Infinity)), aLast = of(answeredIn(wk2, wk1));
      const row = document.createElement('div');
      row.className = 'review-delta-row';
      row.dataset.cat = cat.id;
      row.style.setProperty('--cat', catColor(cat.id));
      const name = document.createElement('span');
      name.className = 'review-delta-cat';
      name.textContent = cat.label;
      const newer = document.createElement('span');
      newer.className = 'review-delta-stat';
      newer.innerHTML = `new <b>${cThis}</b> ${arrow(cThis, cLast)}`;
      const answered = document.createElement('span');
      answered.className = 'review-delta-stat';
      answered.innerHTML = `answered <b>${aThis}</b> ${arrow(aThis, aLast)}`;
      row.append(name, newer, answered);
      deltas.append(row);
    }
    sheet.append(deltas);
  }

  // Neglect list — important and untouched for a month.
  const neglect = detailPanel('Neglected', 'High-importance cards untouched for more than 30 days.');
  neglect.classList.add('review-neglect');
  const neglected = open
    .filter((c) => c.importance === 'high' && now - c.updatedAt > 30 * DAY)
    .sort((a, b) => a.updatedAt - b.updatedAt);
  if (neglected.length) {
    const list = document.createElement('div');
    list.className = 'stale-list';
    for (const c of neglected) {
      const row = areaRow(c);
      row.classList.add('stale-row');
      const age = document.createElement('span');
      age.className = 'stale-age';
      age.textContent = `${humanAge(now - c.updatedAt)} ago`;
      row.append(age);
      list.append(row);
    }
    neglect.append(list);
  } else {
    const p = document.createElement('p');
    p.className = 'panel-hint';
    p.textContent = 'Nothing important is gathering dust.';
    neglect.append(p);
  }
  sheet.append(neglect);

  // Resurfacing — three old thoughts, same three all day.
  const resurface = detailPanel("Today's resurfacing", 'Three old thoughts, re-met on purpose. Keep, open, or let go.');
  resurface.classList.add('review-resurface');
  const picks = resurfaceToday();
  if (picks.length) for (const c of picks) resurface.append(renderResurfaceCard(c));
  else {
    const p = document.createElement('p');
    p.className = 'panel-hint';
    p.textContent = 'Nothing open to resurface — the board is at rest.';
    resurface.append(p);
  }
  sheet.append(resurface);

  // The stamp — done is recorded on this device only, like a desk habit.
  const stampRow = document.createElement('div');
  stampRow.className = 'review-stamp-row';
  const stampBtn = document.createElement('button');
  stampBtn.type = 'button';
  stampBtn.id = 'review-stamp';
  stampBtn.className = 'btn primary';
  stampBtn.textContent = 'Stamp the review done';
  stampBtn.addEventListener('click', () => {
    try { localStorage.setItem(REVIEW_KEY, String(Date.now())); } catch (_) { /* private mode */ }
    render();
    announce('Review stamped');
  });
  stampRow.append(stampBtn);
  if (lastReviewedAt >= startOfToday()) {
    const stamped = document.createElement('span');
    stamped.className = 'review-stamped';
    stamped.textContent = 'Reviewed';
    stampRow.append(stamped);
  }
  sheet.append(stampRow);
  return sheet;
}
