(() => {
  'use strict';

  const STORAGE_KEY = 'question-board:v1';
  const THEME_KEY = 'question-board:theme';
  const VIEW_KEY = 'question-board:view';
  const HISTORY_KEY = 'question-board:history';
  const HISTORY_LIMIT = 50; // snapshots kept; oldest fall off like a rotated log

  const COLUMNS = [
    { id: 'inbox', title: 'Inbox' },
    { id: 'in-progress', title: 'In Progress' },
    { id: 'answered', title: 'Answered' },
  ];

  // What kind of thing a card is — stamped on the card like the old priority
  // stamp, but neutral ink: colour on this board always means category.
  const TYPES = ['question', 'problem', 'task', 'idea', 'plan'];
  const TYPE_META = {
    question: { glyph: '?', label: 'question' },
    problem:  { glyph: '!', label: 'problem' },
    task:     { glyph: '✓', label: 'task' },
    idea:     { glyph: '✦', label: 'idea' },
    plan:     { glyph: '→', label: 'plan' },
  };
  const TYPE_RANK = Object.fromEntries(TYPES.map((t, i) => [t, i]));

  // Life areas — the coloured celluloid tabs of the ledger. Each category is an
  // oklch hue; every theme sets --cat-l/--cat-c once, so every ink stays
  // legible on Morning/Day/Dusk/Night without per-theme colour tables.
  // The set is the user's own: categories can be added and removed (✎ on the
  // rail), and the registry is saved with the board and synced to the server.
  const DEFAULT_CATEGORIES = [
    { id: 'work',   label: 'Work',   h: 255 },
    { id: 'love',   label: 'Love',   h: 15 },
    { id: 'family', label: 'Family', h: 60 },
    { id: 'health', label: 'Health', h: 150 },
    { id: 'mind',   label: 'Mind',   h: 295 },
    { id: 'music',  label: 'Music',  h: 340 },
    { id: 'travel', label: 'Travel', h: 200 },
    { id: 'home',   label: 'Home',   h: 90 },
    { id: 'money',  label: 'Money',  h: 40 },
  ];
  const CAT_LIMIT = 24;
  // Hues a new category can be inked in, spread around the oklch wheel.
  const HUE_CHOICES = [15, 40, 60, 90, 120, 150, 180, 200, 230, 255, 285, 310, 340];

  let categories = DEFAULT_CATEGORIES.map((c) => ({ ...c }));

  const catSlug = (s) =>
    String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24);
  const catById = (id) => categories.find((c) => c.id === id);
  const catColor = (id) => {
    const c = catById(id);
    return c ? `oklch(var(--cat-l) var(--cat-c) ${c.h})` : 'var(--ink-soft)';
  };
  const catLabel = (id) => (catById(id) || { label: '' }).label;

  function sanitizeCategories(raw) {
    if (!Array.isArray(raw)) return null;
    const seen = new Set();
    const out = [];
    for (const c of raw) {
      if (!c || typeof c !== 'object') continue;
      const id = typeof c.id === 'string' ? catSlug(c.id) : '';
      const label = typeof c.label === 'string' && c.label.trim() ? c.label.trim().slice(0, 24) : '';
      const h = Number.isFinite(c.h) ? ((Math.round(c.h) % 360) + 360) % 360 : null;
      if (!id || !label || h === null || seen.has(id)) continue;
      seen.add(id);
      out.push({ id, label, h });
      if (out.length >= CAT_LIMIT) break;
    }
    return out.length ? out : null;
  }

  const typeVal = (t) => (TYPES.includes(t) ? t : 'question');
  // Validate against the live registry by default; imports pass the file's own
  // registry so custom categories survive the round trip.
  const catVal = (c, reg = categories) => (reg.some((x) => x.id === c) ? c : '');

  // Importance & urgency are each High, Low, or unset ('') — a question needs
  // both to be placed on the Eisenhower matrix.
  const iuVal = (v) => (v === 'high' || v === 'low' ? v : '');

  // Effort ("how much work is this?") and control ("can I even act on it?")
  // always hold a value: the scale's midpoint stands in until a person — or,
  // one day, the brain — judges it. The *Src fields record who set the value
  // (default | user | ai) so an estimator never overwrites a human's call.
  const effortVal = (v) => (v === 'low' || v === 'high' ? v : 'medium');
  const controlVal = (v) => (v === 'act' || v === 'none' ? v : 'influence');
  const srcVal = (v) => (v === 'user' || v === 'ai' ? v : 'default');
  const EFFORT_LABEL = { low: 'Low', medium: 'Medium', high: 'High' };
  const CONTROL_LABEL = { act: 'I can act', influence: 'I can influence', none: 'Out of my hands' };

  // --------------------------------------------------------------------------
  // State & persistence
  // --------------------------------------------------------------------------

  const uid = () =>
    (crypto.randomUUID ? crypto.randomUUID() : 'id-' + Math.random().toString(36).slice(2) + Date.now());

  function seedCards() {
    const now = Date.now();
    const mk = (title, columnId, type, category, tags, importance = '', urgency = '', notes = '') =>
      ({ id: uid(), columnId, title, notes, type, category, importance, urgency,
         effort: 'medium', control: 'influence', effortSrc: 'default', controlSrc: 'default',
         tags, createdAt: now, updatedAt: now });
    // Seeds span categories, types and all four matrix quadrants, so every view
    // has something to show on a fresh board.
    return [
      mk('What should I build next quarter?', 'inbox', 'question', 'work', ['planning'], 'high', 'low'),
      mk('How do we keep weekday evenings free together?', 'inbox', 'problem', 'love', ['us'], 'high', 'high'),
      mk('Which Stoic should I read after Meditations?', 'inbox', 'question', 'mind', ['reading'], 'low', 'low'),
      mk('Plan a long weekend in the mountains', 'in-progress', 'plan', 'travel', ['autumn'], 'low', 'high'),
      mk('Learn the intro to “Blackbird”', 'in-progress', 'plan', 'music', ['guitar']),
      mk('Book the dentist check-up', 'answered', 'task', 'health', [], '', '', 'Done — appointment on the 12th.'),
    ].map((c, i) => ({ ...c, num: i + 1 }));
  }

  // Every question keeps a permanent ledger number (Q-001, Q-002, …) in capture order.
  function ensureNums(cards) {
    let max = cards.reduce((m, c) => Math.max(m, c.num || 0), 0);
    [...cards]
      .filter((c) => !c.num)
      .sort((a, b) => a.createdAt - b.createdAt)
      .forEach((c) => { c.num = ++max; });
    return cards;
  }

  const qLabel = (card) => 'Q-' + String(card.num).padStart(3, '0');

  function sanitizeCard(raw, reg = categories) {
    if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) return null;
    return {
      id: typeof raw.id === 'string' && raw.id ? raw.id : uid(),
      columnId: COLUMNS.some((c) => c.id === raw.columnId) ? raw.columnId : 'inbox',
      title: raw.title.trim(),
      notes: typeof raw.notes === 'string' ? raw.notes : '',
      type: typeVal(raw.type),
      category: catVal(raw.category, reg),
      importance: iuVal(raw.importance),
      urgency: iuVal(raw.urgency),
      effort: effortVal(raw.effort),
      control: controlVal(raw.control),
      effortSrc: srcVal(raw.effortSrc),
      controlSrc: srcVal(raw.controlSrc),
      num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
      tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
      createdAt: typeof raw.createdAt === 'number' ? raw.createdAt : Date.now(),
      updatedAt: typeof raw.updatedAt === 'number' ? raw.updatedAt : Date.now(),
    };
  }

  function parseState(json) {
    const data = JSON.parse(json);
    if (!data || data.version !== 1 || !Array.isArray(data.cards)) throw new Error('Unrecognized data format');
    // Files/saves that predate custom categories have no registry — cats stays
    // null and the caller keeps whatever registry it already has.
    const cats = sanitizeCategories(data.categories);
    return {
      version: 1,
      columns: COLUMNS,
      categories: cats,
      cards: ensureNums(data.cards.map((c) => sanitizeCard(c, cats || categories)).filter(Boolean)),
    };
  }

  let loadedFromStorage = false; // true when this browser already had a saved board

  function loadState() {
    try {
      const json = localStorage.getItem(STORAGE_KEY);
      if (json) {
        const saved = parseState(json);
        if (saved.categories) categories = saved.categories;
        loadedFromStorage = true;
        return saved;
      }
    } catch (err) {
      console.warn('Could not load saved board, starting fresh.', err);
    }
    return { version: 1, columns: COLUMNS, cards: seedCards() };
  }

  let state = loadState();
  const filters = { search: '', type: '', category: '', tags: new Set() };
  let focusCardId = null; // restore focus after re-render (keyboard moves)
  let draggedId = null;
  let dealCards = true; // deal-in animation runs on first render only

  const VIEWS = ['board', 'backlog', 'overview', 'matrix', 'areas', 'review', 'assistant'];
  const VIEW_LABELS = { board: 'Board', backlog: 'Backlog', overview: 'Overview', matrix: 'Matrix', areas: 'Areas', review: 'Review', assistant: 'Assistant' };
  let view = 'board';
  try {
    const v = localStorage.getItem(VIEW_KEY);
    if (VIEWS.includes(v)) view = v;
  } catch (_) { /* private mode */ }

  const nextNum = () => state.cards.reduce((m, c) => Math.max(m, c.num || 0), 0) + 1;

  // --------------------------------------------------------------------------
  // History — an append-only timeline of board snapshots, like a git reflog.
  // `index` is where the board currently stands; undo/restore only move the
  // pointer, so no state is ever lost until it falls off the HISTORY_LIMIT end.
  // --------------------------------------------------------------------------

  const snapshot = (cards) => JSON.parse(JSON.stringify(cards));
  const short = (s) => (s.length > 42 ? s.slice(0, 39) + '…' : s);

  let timeline = { entries: [], index: -1 };
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.entries) && Number.isInteger(parsed.index)) timeline = parsed;
    }
  } catch (_) { /* private mode */ }
  if (!timeline.entries.length) {
    timeline.entries = [{ ts: Date.now(), action: 'Board opened', cards: snapshot(state.cards) }];
  }
  timeline.index = Math.min(Math.max(timeline.index, 0), timeline.entries.length - 1);

  function saveTimeline() {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(timeline));
    } catch (err) {
      console.warn('Could not save history.', err);
    }
  }

  function saveState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...state, categories }));
    } catch (err) {
      console.warn('Could not save board.', err);
    }
    pushToServer(); // keep the SQLite-backed server in sync when one is present
  }

  // --------------------------------------------------------------------------
  // Server sync — when a backend is reachable the board is persisted to its
  // SQLite database; otherwise the app runs entirely on localStorage. The
  // whole board is pushed on every change, so deleting a question is the only
  // thing that removes its row server-side.
  // --------------------------------------------------------------------------

  const API = '/api/state';
  let serverAvailable = false;
  let serverOffline = false; // true once a push has failed, to warn only once
  let pushTimer = null;

  // Order-sensitive fingerprint, to skip redundant work when nothing changed.
  const boardFingerprint = (cards) =>
    cards.map((c) => [c.id, c.columnId, c.title, c.notes, c.type, c.category || '', c.importance || '', c.urgency || '',
      c.effort || '', c.control || '', c.effortSrc || '', c.controlSrc || '',
      c.num, (c.tags || []).join('|')].join('␟')).join('␞');

  function pushToServer() {
    if (!serverAvailable) return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(async () => {
      try {
        const res = await fetch(API, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: 1, cards: state.cards, categories }),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        if (serverOffline) { serverOffline = false; announce('Reconnected — changes saved to the server'); }
      } catch (err) {
        if (!serverOffline) {
          serverOffline = true;
          announce('Server unreachable — changes are saved locally for now');
        }
        console.warn('Could not save to server.', err);
      }
    }, 150);
  }

  async function initServerSync() {
    let board;
    try {
      const res = await fetch(API, { headers: { Accept: 'application/json' } });
      if (!res.ok) return; // no usable backend — stay in localStorage mode
      board = await res.json();
    } catch (_) {
      return; // static / offline — localStorage mode
    }
    if (!board || !Array.isArray(board.cards)) return;
    serverAvailable = true;
    const serverCats = sanitizeCategories(board.categories);

    // A browser that already has its own board wins on load — this guarantees
    // unsynced local edits are never clobbered — and we converge the server to
    // it. A fresh browser (no local board) instead loads from the database.
    if (loadedFromStorage && state.cards.length > 0) {
      pushToServer();
      return;
    }

    if (serverCats) categories = serverCats; // fresh browser — the DB's registry wins

    if (board.cards.length === 0) {
      if (state.cards.length > 0) pushToServer(); // fresh DB — save our seed board
      return;
    }

    const incoming = ensureNums(board.cards.map((c) => sanitizeCard(c)).filter(Boolean));
    if (boardFingerprint(incoming) === boardFingerprint(state.cards)) return; // already in sync

    // Fresh browser, and the database has a board — adopt it as the source of truth.
    state = { version: 1, columns: COLUMNS, cards: incoming };
    saveState();
    timeline.entries.push({ ts: Date.now(), action: `Loaded ${incoming.length} card(s) from the server`, cards: snapshot(incoming) });
    if (timeline.entries.length > HISTORY_LIMIT) timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
    timeline.index = timeline.entries.length - 1;
    saveTimeline();
    dealCards = true;
    render();
  }

  function commit(action) {
    saveState();
    timeline.entries.push({ ts: Date.now(), action, cards: snapshot(state.cards) });
    if (timeline.entries.length > HISTORY_LIMIT) {
      timeline.entries.splice(0, timeline.entries.length - HISTORY_LIMIT);
    }
    timeline.index = timeline.entries.length - 1;
    saveTimeline();
    render();
  }

  /** Point the board at timeline entry `i` without writing a new entry. */
  function restoreEntry(i, message) {
    const entry = timeline.entries[i];
    if (!entry) return;
    state = { version: 1, columns: COLUMNS, cards: snapshot(entry.cards) };
    timeline.index = i;
    saveState();
    saveTimeline();
    dealCards = true;
    render();
    announce(message);
  }

  // --------------------------------------------------------------------------
  // Trash — deleting a question from the board only hides it; the server keeps
  // the row (soft delete) so it stays recoverable even if this browser's local
  // history is cleared. Only an explicit "Delete permanently" purges it for
  // good. The Trash is server-backed, so it only appears when a backend is
  // running (localStorage-only mode relies on the History timeline instead).
  // --------------------------------------------------------------------------

  const TRASH_API = '/api/trash';

  async function fetchTrash() {
    if (!serverAvailable) return [];
    try {
      const res = await fetch(TRASH_API, { headers: { Accept: 'application/json' } });
      if (!res.ok) return [];
      const data = await res.json();
      return Array.isArray(data.cards) ? data.cards.map((c) => sanitizeCard(c)).filter(Boolean) : [];
    } catch (_) {
      return [];
    }
  }

  function restoreFromTrash(card) {
    if (getCard(card.id)) return; // already back on the board
    const revived = { ...card, columnId: COLUMNS.some((c) => c.id === card.columnId) ? card.columnId : 'inbox' };
    state.cards = [...state.cards, revived];
    ensureNums(state.cards);
    commit(`Restored ${qLabel(revived)} “${short(revived.title)}”`); // re-adds the row server-side (clears deleted_at)
    announce(`Restored “${revived.title}”`);
  }

  async function purgeFromTrash(card) {
    const sure = await ask({
      title: 'Delete permanently?',
      message: `${qLabel(card)} “${card.title}” will be erased from the database for good. This is the only action that truly deletes it, and it cannot be undone.`,
      okLabel: 'Delete permanently',
      danger: true,
    });
    if (!sure) return false;
    try {
      const res = await fetch(`/api/cards/${encodeURIComponent(card.id)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    } catch (err) {
      announce('Could not delete permanently — the server was unreachable');
      console.warn('Purge failed.', err);
      return false;
    }
    scrubFromTimeline(card.id);
    announce(`Permanently deleted “${card.title}”`);
    return true;
  }

  // Once a question is purged, drop it from every local history snapshot too, so
  // time-travelling back through History can't resurrect what was deleted for good.
  function scrubFromTimeline(id) {
    let changed = false;
    for (const entry of timeline.entries) {
      const before = entry.cards.length;
      entry.cards = entry.cards.filter((c) => c.id !== id);
      if (entry.cards.length !== before) changed = true;
    }
    if (changed) saveTimeline();
  }

  // --------------------------------------------------------------------------
  // Helpers
  // --------------------------------------------------------------------------

  const $ = (sel, root = document) => root.querySelector(sel);

  const getCard = (id) => state.cards.find((c) => c.id === id);
  const columnCards = (columnId) => state.cards.filter((c) => c.columnId === columnId);
  const columnIndex = (columnId) => COLUMNS.findIndex((c) => c.id === columnId);
  const columnTitle = (columnId) => COLUMNS[columnIndex(columnId)].title;

  function matchesFilters(card) {
    if (filters.type && card.type !== filters.type) return false;
    if (filters.category && card.category !== filters.category) return false;
    if (filters.tags.size && ![...filters.tags].every((t) => card.tags.includes(t))) return false;
    if (filters.search) {
      const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
      if (!haystack.includes(filters.search)) return false;
    }
    return true;
  }

  const filtersActive = () => Boolean(filters.search || filters.type || filters.category || filters.tags.size);

  function announce(message) {
    $('#live-region').textContent = message;
  }

  /**
   * In-app replacement for confirm()/alert(). Native dialogs are silently
   * blocked in sandboxed embeds (e.g. artifact viewers), so all confirmations
   * go through this <dialog> instead. Pass cancelLabel: null for an alert.
   */
  function ask({ title, message, okLabel = 'OK', cancelLabel = 'Cancel', danger = false }) {
    return new Promise((resolve) => {
      const confirmDialog = $('#confirm-dialog');
      $('#confirm-title').textContent = title;
      $('#confirm-copy').textContent = message;
      const ok = $('#confirm-ok');
      ok.textContent = okLabel;
      ok.className = 'btn ' + (danger ? 'danger' : 'primary');
      const cancel = $('#confirm-cancel');
      cancel.hidden = cancelLabel === null;
      if (cancelLabel !== null) cancel.textContent = cancelLabel;
      confirmDialog.returnValue = '';
      confirmDialog.addEventListener(
        'close',
        () => resolve(confirmDialog.returnValue === 'ok'),
        { once: true }
      );
      confirmDialog.showModal();
      ok.focus();
    });
  }

  /**
   * Move a card to a column, placed before the card with id `beforeId`
   * (or at the end of the column when beforeId is null).
   */
  function moveCard(cardId, columnId, beforeId = null) {
    const card = getCard(cardId);
    if (!card || cardId === beforeId) return;
    state.cards = state.cards.filter((c) => c.id !== cardId);
    card.columnId = columnId;
    card.updatedAt = Date.now();

    let index = -1;
    if (beforeId) index = state.cards.findIndex((c) => c.id === beforeId);
    if (index === -1) {
      let lastInColumn = -1;
      state.cards.forEach((c, i) => { if (c.columnId === columnId) lastInColumn = i; });
      index = lastInColumn + 1 || state.cards.length;
      if (lastInColumn === -1) index = state.cards.length;
    }
    state.cards.splice(index, 0, card);
    commit(`Moved ${qLabel(card)} to ${columnTitle(columnId)}`);
  }

  // --------------------------------------------------------------------------
  // Rendering
  // --------------------------------------------------------------------------

  function render() {
    const board = $('#board');
    board.className = view;
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
    }
    renderCatRail();
    renderTagBar();
    $('#undo-btn').disabled = timeline.index <= 0;

    if (dealCards) {
      board.querySelectorAll('.card, .backlog-row').forEach((el, i) => {
        el.classList.add('deal');
        el.style.animationDelay = `${i * 45}ms`;
      });
      dealCards = false;
    }

    if (focusCardId) {
      const el = board.querySelector(`[data-id="${focusCardId}"]`);
      if (el) el.focus();
      focusCardId = null;
    }
  }

  function renderColumn(col) {
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

    if (visible.length > 1) {
      const sortBtn = document.createElement('button');
      sortBtn.className = 'sort-btn';
      sortBtn.textContent = 'Sort ⇅';
      sortBtn.title = 'Group this column by type';
      sortBtn.addEventListener('click', () => sortColumnByType(col.id));
      header.append(sortBtn);
    }

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

  function renderQuickAdd() {
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
        importance: '', urgency: '',
        effort: 'medium', control: 'influence', effortSrc: 'default', controlSrc: 'default',
        num: nextNum(), tags: [], createdAt: now, updatedAt: now };
      // New captures go to the top of the Inbox
      const firstInbox = state.cards.findIndex((c) => c.columnId === 'inbox');
      state.cards.splice(firstInbox === -1 ? state.cards.length : firstInbox, 0, card);
      // A search or tag filter could still hide the fresh card — clear those so
      // the capture never vanishes silently.
      if (!matchesFilters(card)) {
        filters.search = '';
        filters.tags.clear();
        $('#search').value = '';
      }
      commit(`Added ${qLabel(card)} “${short(title)}”`);
      announce(`Added “${title}” to Inbox`);
      const fresh = $('#board .quick-add input');
      if (fresh) fresh.focus();
    });
    return form;
  }

  // Rubber-stamp badge for a card's type — always neutral ink.
  function typeBadge(card) {
    const badge = document.createElement('span');
    badge.className = `badge type-${card.type}`;
    badge.textContent = `${TYPE_META[card.type].glyph} ${TYPE_META[card.type].label}`;
    return badge;
  }

  function cardAria(card) {
    const cat = card.category ? `, ${catLabel(card.category)}` : '';
    return `${qLabel(card)}: ${card.title} — ${TYPE_META[card.type].label}${cat}, in ${columnTitle(card.columnId)}`;
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
    num.textContent = qLabel(card);
    top.append(num);

    if (card.notes.trim()) {
      const dot = document.createElement('span');
      dot.className = 'notes-dot';
      dot.title = 'Has notes';
      dot.textContent = '¶';
      top.append(dot);
    }

    top.append(typeBadge(card));

    const title = document.createElement('p');
    title.className = 'card-title';
    title.textContent = card.title;

    el.append(top, title);

    if (card.category || card.tags.length) {
      const tags = document.createElement('div');
      tags.className = 'card-tags';
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

    el.addEventListener('click', () => openDialog(card.id));
    el.addEventListener('keydown', (e) => onCardKeydown(e, card.id));

    el.addEventListener('dragstart', (e) => {
      draggedId = card.id;
      e.dataTransfer.setData('text/plain', card.id);
      e.dataTransfer.effectAllowed = 'move';
      requestAnimationFrame(() => el.classList.add('dragging'));
    });
    el.addEventListener('dragend', () => {
      draggedId = null;
      el.classList.remove('dragging');
      clearDropIndicator();
      document.querySelectorAll('.cards.drop-target').forEach((z) => z.classList.remove('drop-target'));
    });

    return el;
  }

  // The category rail — coloured index-tabs, one per life area. "All" opens
  // every drawer at once (the whole-life visualization); clicking a category
  // filters the board to that drawer; ✎ Edit manages the registry itself.
  function renderCatRail() {
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

  function renderTagBar() {
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

  // --------------------------------------------------------------------------
  // Backlog view — the Inbox as a scannable ledger register
  // --------------------------------------------------------------------------

  function renderBacklog() {
    const sheet = document.createElement('div');
    sheet.className = 'backlog-sheet';

    const visible = columnCards('inbox').filter(matchesFilters);

    const head = document.createElement('div');
    head.className = 'backlog-head';

    const title = document.createElement('h2');
    title.className = 'backlog-title';
    title.textContent = 'Inbox backlog';

    const count = document.createElement('span');
    count.className = 'backlog-count';
    count.textContent = `${visible.length} ${visible.length === 1 ? 'card' : 'cards'}`;

    head.append(title, count);

    if (visible.length > 1) {
      const sortBtn = document.createElement('button');
      sortBtn.className = 'sort-btn';
      sortBtn.textContent = 'Sort by type ⇅';
      sortBtn.title = 'Group the backlog by type';
      sortBtn.addEventListener('click', () => sortColumnByType('inbox'));
      head.append(sortBtn);
    }

    sheet.append(head, renderQuickAdd());

    const list = document.createElement('div');
    list.className = 'backlog-list';

    if (visible.length === 0) {
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = filtersActive() ? 'No cards match' : 'Write down your first card above';
      list.append(hint);
    } else {
      for (const card of visible) list.append(renderBacklogRow(card));
    }

    sheet.append(list);
    return sheet;
  }

  function renderBacklogRow(card) {
    const row = document.createElement('article');
    row.className = 'backlog-row';
    row.dataset.id = card.id;
    row.tabIndex = 0;
    row.style.setProperty('--cat', catColor(card.category));
    if (card.category) row.classList.add('categorized');
    row.setAttribute('aria-label', cardAria(card));

    const num = document.createElement('span');
    num.className = 'row-num';
    num.textContent = qLabel(card);

    const badge = typeBadge(card);

    const main = document.createElement('div');
    main.className = 'row-main';

    const title = document.createElement('p');
    title.className = 'row-title';
    title.textContent = card.title;
    main.append(title);

    if (card.category || card.tags.length) {
      const tags = document.createElement('div');
      tags.className = 'card-tags';
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
      main.append(tags);
    }

    const notes = document.createElement('span');
    notes.className = 'row-notes';
    if (card.notes.trim()) {
      notes.textContent = '¶';
      notes.title = 'Has notes';
    }

    row.append(num, badge, main, notes);
    row.addEventListener('click', () => openDialog(card.id));
    row.addEventListener('keydown', (e) => onCardKeydown(e, card.id));
    return row;
  }

  // --------------------------------------------------------------------------
  // Plotted views — shared "stamped question dots" on the engineering grid.
  // Overview (a semantic map) and the Matrix both place cards as dots that
  // reveal an index-card tooltip on hover and open the full editor on click.
  // Each dot is inked in its category's colour, so the map reads by life area.
  // --------------------------------------------------------------------------

  const IU_LABEL = { high: 'High', low: 'Low', '': 'not set' };

  function dotAriaLabel(card) {
    let s = cardAria(card);
    if (card.importance || card.urgency) {
      s += `, importance ${IU_LABEL[iuVal(card.importance)]}, urgency ${IU_LABEL[iuVal(card.urgency)]}`;
    }
    return s;
  }

  // One shared tooltip, moved to whichever dot is hovered or focused.
  let plotTip = null;
  function ensurePlotTip() {
    if (plotTip) return plotTip;
    plotTip = document.createElement('div');
    plotTip.className = 'plot-tip';
    plotTip.hidden = true;
    document.body.append(plotTip);
    return plotTip;
  }

  function showPlotTip(card, dotEl) {
    const tip = ensurePlotTip();
    tip.innerHTML = '';

    const head = document.createElement('div');
    head.className = 'plot-tip-head';
    const num = document.createElement('span');
    num.className = 'card-num';
    num.textContent = qLabel(card);
    head.append(num, typeBadge(card));

    const title = document.createElement('p');
    title.className = 'plot-tip-title';
    title.textContent = card.title;

    const meta = document.createElement('p');
    meta.className = 'plot-tip-meta';
    meta.textContent = `in ${columnTitle(card.columnId)}`;
    if (card.category) meta.textContent += ` · ${catLabel(card.category)}`;
    if (card.importance || card.urgency) {
      meta.textContent += ` · importance ${IU_LABEL[iuVal(card.importance)]} · urgency ${IU_LABEL[iuVal(card.urgency)]}`;
    }

    tip.append(head, title, meta);

    if (card.notes.trim()) {
      const notes = document.createElement('p');
      notes.className = 'plot-tip-notes';
      notes.textContent = card.notes.trim();
      tip.append(notes);
    }
    if (card.tags.length) {
      const tags = document.createElement('div');
      tags.className = 'card-tags';
      for (const t of card.tags) {
        const chip = document.createElement('span');
        chip.className = 'card-tag';
        chip.textContent = t;
        tags.append(chip);
      }
      tip.append(tags);
    }

    tip.hidden = false;
    positionPlotTip(dotEl);
  }

  function positionPlotTip(dotEl) {
    if (!plotTip || plotTip.hidden) return;
    const r = dotEl.getBoundingClientRect();
    const t = plotTip.getBoundingClientRect();
    let left = r.left + r.width / 2 - t.width / 2;
    let top = r.top - t.height - 10;
    if (top < 8) top = r.bottom + 10; // flip below when there's no room above
    left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
    plotTip.style.left = `${left}px`;
    plotTip.style.top = `${top}px`;
  }

  const hidePlotTip = () => { if (plotTip) plotTip.hidden = true; };

  function renderPlotDot(card, leftPct, topPct) {
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'plot-dot';
    dot.dataset.id = card.id;
    dot.dataset.col = card.columnId;
    // Overview passes fractions to place dots absolutely; the Matrix omits them
    // and lets the dots flow inside their quadrant (positioned via CSS).
    if (leftPct != null) dot.style.left = `${leftPct}%`;
    if (topPct != null) dot.style.top = `${topPct}%`;
    dot.style.setProperty('--dot', catColor(card.category));
    dot.setAttribute('aria-label', dotAriaLabel(card));

    const n = document.createElement('span');
    n.className = 'plot-dot-num';
    n.textContent = String(card.num);
    dot.append(n);

    dot.addEventListener('click', () => openDialog(card.id));
    dot.addEventListener('mouseenter', () => showPlotTip(card, dot));
    dot.addEventListener('mouseleave', hidePlotTip);
    dot.addEventListener('focus', () => showPlotTip(card, dot));
    dot.addEventListener('blur', hidePlotTip);
    return dot;
  }

  function renderPlotLegend() {
    const legend = document.createElement('div');
    legend.className = 'plot-legend';
    const inUse = new Set(state.cards.map((c) => c.category));
    const entries = categories.filter((c) => inUse.has(c.id)).map((c) => [c.id, c.label]);
    if (inUse.has('')) entries.push(['', 'Uncategorized']);
    for (const [id, label] of entries) {
      const item = document.createElement('span');
      item.className = 'plot-legend-item';
      item.style.setProperty('--dot', catColor(id));
      item.textContent = label;
      legend.append(item);
    }
    return legend;
  }

  function plotEmptyHint(text) {
    const hint = document.createElement('div');
    hint.className = 'empty-hint plot-empty';
    hint.textContent = text;
    return hint;
  }

  // --- Embeddings + PCA -----------------------------------------------------
  // Each question becomes a vector; PCA projects those vectors to two dimensions
  // (PC-1, PC-2). Real semantic vectors come from a HuggingFace model
  // (Transformers.js) loaded lazily from a CDN; until it's ready — or if it
  // can't load (offline) — a deterministic keyword vector stands in, so the map
  // always renders and never needs the network.

  const EMBED_DIM = 128;
  const cardText = (card) => `${card.title} ${card.notes}`.trim();

  function textHash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(36);
  }

  function l2normalize(v) {
    let s = 0;
    for (let i = 0; i < v.length; i++) s += v[i] * v[i];
    s = Math.sqrt(s);
    if (s > 0) for (let i = 0; i < v.length; i++) v[i] /= s;
    return v;
  }

  function localEmbed(text) {
    const v = new Float64Array(EMBED_DIM);
    const tokens = String(text).toLowerCase().match(/[a-z0-9]+/g) || [];
    for (const tok of tokens) {
      let h = 2166136261;
      for (let k = 0; k < tok.length; k++) { h ^= tok.charCodeAt(k); h = Math.imul(h, 16777619); }
      v[(h >>> 0) % EMBED_DIM] += 1;
    }
    return l2normalize(v);
  }

  const vdot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };

  // Dominant eigenvector of the centred data's covariance, via power iteration;
  // pass `deflate` to get the next component orthogonal to the first.
  function powerIteration(X, d, deflate) {
    const n = X.length;
    let v = new Float64Array(d);
    for (let j = 0; j < d; j++) v[j] = ((Math.imul(j + 1, 2654435761) >>> 0) % 2000) / 1000 - 1; // deterministic seed
    l2normalize(v);
    for (let iter = 0; iter < 60; iter++) {
      if (deflate) { const p = vdot(v, deflate); for (let j = 0; j < d; j++) v[j] -= p * deflate[j]; }
      const Xv = new Float64Array(n);
      for (let i = 0; i < n; i++) Xv[i] = vdot(X[i], v);
      const w = new Float64Array(d);
      for (let i = 0; i < n; i++) { const row = X[i], c = Xv[i]; for (let j = 0; j < d; j++) w[j] += row[j] * c; }
      if (deflate) { const p = vdot(w, deflate); for (let j = 0; j < d; j++) w[j] -= p * deflate[j]; }
      if (vdot(w, w) === 0) break;
      l2normalize(w);
      v = w;
    }
    return v;
  }

  function pca2(vectors) {
    const n = vectors.length, d = vectors[0].length;
    const mean = new Float64Array(d);
    for (const v of vectors) for (let j = 0; j < d; j++) mean[j] += v[j];
    for (let j = 0; j < d; j++) mean[j] /= n;
    const X = vectors.map((v) => { const r = new Float64Array(d); for (let j = 0; j < d; j++) r[j] = v[j] - mean[j]; return r; });
    const pc1 = powerIteration(X, d, null);
    const pc2 = powerIteration(X, d, pc1);
    return X.map((r) => [vdot(r, pc1), vdot(r, pc2)]);
  }

  // Map raw 2-D points to plotting fractions in [0.06, 0.94], centred so the
  // data mean sits at the middle crosshair. A degenerate spread falls back to a ring.
  function normalizePoints(cards, pts) {
    const coords = new Map();
    const n = cards.length;
    let mx = 0, my = 0;
    for (const [x, y] of pts) { mx += x; my += y; }
    mx /= n; my /= n;
    let sx = 0, sy = 0;
    for (const [x, y] of pts) { sx = Math.max(sx, Math.abs(x - mx)); sy = Math.max(sy, Math.abs(y - my)); }
    if (Math.max(sx, sy) < 1e-9) {
      cards.forEach((c, i) => {
        const a = (i / n) * Math.PI * 2;
        coords.set(c.id, { x: 0.5 + 0.32 * Math.cos(a), y: 0.5 + 0.32 * Math.sin(a) });
      });
      return coords;
    }
    cards.forEach((c, i) => {
      const [x, y] = pts[i];
      coords.set(c.id, {
        x: 0.5 + 0.44 * (x - mx) / (sx || 1),
        y: 0.5 - 0.44 * (y - my) / (sy || 1), // invert so PC-2 grows upward
      });
    });
    return coords;
  }

  let semanticState = 'idle'; // idle | loading | ready | unavailable
  const semanticCache = new Map(); // textHash -> Float32Array
  let extractorPromise = null;

  // --- Projection toggle: PCA (fast, global axes) or t-SNE (local
  // neighbourhoods — clusters of related thoughts pull together). t-SNE is
  // computed from the same vectors, seeded so the same cards always land in
  // the same spots, and cached per exact set of vectors.
  const PROJ_KEY = 'question-board:proj';
  const PROJ_LABEL = { pca: 'PCA', tsne: 't-SNE' };
  let projection = 'pca';
  try { const p = localStorage.getItem(PROJ_KEY); if (Object.hasOwn(PROJ_LABEL, p)) projection = p; } catch { /* private mode */ }

  function mulberry32(seed) {
    let a = seed | 0;
    return () => {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  let tsneRun = 0; // bumping this aborts any in-flight gradient loop
  let tsneBusyKey = null;
  const tsneCache = new Map(); // vector-set hash -> Map(cardId -> [x, y])

  const tsneKey = (cards, useSemantic) =>
    textHash(cards.map((c) => textHash(cardText(c))).join('|')) + (useSemantic ? ':s' : ':k');

  const tsneReady = (cards, key) => {
    const cached = tsneCache.get(key);
    return Boolean(cached) && cards.every((c) => cached.has(c.id));
  };

  // Exact t-SNE — O(n²) per iteration is fine at personal-board sizes. Runs in
  // chunks (yields to the browser every 40 iterations) so the tab never
  // freezes; when it converges the dots slide over via updateOverviewPlot.
  async function computeTsne(cards, vecs, initPts, key) {
    if (tsneBusyKey === key) return;
    tsneBusyKey = key;
    const runId = ++tsneRun;
    try {
      const n = vecs.length;
      const perplexity = Math.max(1, Math.min(15, Math.floor((n - 1) / 3)));
      const D = new Float64Array(n * n);
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const a = vecs[i], b = vecs[j];
        let s = 0;
        for (let k = 0; k < a.length; k++) { const d = a[k] - b[k]; s += d * d; }
        D[i * n + j] = s; D[j * n + i] = s;
      }
      // Per-point gaussian bandwidth matched to the target perplexity.
      const P = new Float64Array(n * n);
      const logU = Math.log(perplexity);
      const row = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        let beta = 1, betaMin = -Infinity, betaMax = Infinity;
        for (let t = 0; t < 50; t++) {
          let sum = 0;
          for (let j = 0; j < n; j++) { row[j] = j === i ? 0 : Math.exp(-D[i * n + j] * beta); sum += row[j]; }
          if (sum <= 0) sum = 1e-12;
          let H = 0;
          for (let j = 0; j < n; j++) if (row[j] > 0) { const p = row[j] / sum; H -= p * Math.log(p); }
          const diff = H - logU;
          if (Math.abs(diff) < 1e-5) break;
          if (diff > 0) { betaMin = beta; beta = betaMax === Infinity ? beta * 2 : (beta + betaMax) / 2; }
          else { betaMax = beta; beta = betaMin === -Infinity ? beta / 2 : (beta + betaMin) / 2; }
        }
        let sum = 0;
        for (let j = 0; j < n; j++) sum += row[j];
        if (sum <= 0) sum = 1e-12;
        for (let j = 0; j < n; j++) P[i * n + j] = row[j] / sum;
      }
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const p = Math.max((P[i * n + j] + P[j * n + i]) / (2 * n), 1e-12);
        P[i * n + j] = p; P[j * n + i] = p;
      }
      // Init from the PCA layout (scaled down, seeded whisper of noise to
      // break ties) so t-SNE refines the map instead of reshuffling it.
      const rand = mulberry32(0x10de57a2 ^ n);
      let spread = 0;
      for (const [x, y] of initPts) spread = Math.max(spread, Math.abs(x), Math.abs(y));
      const scale = spread > 0 ? 1e-2 / spread : 1e-2;
      const Y = new Float64Array(n * 2);
      const dY = new Float64Array(n * 2);
      const gains = new Float64Array(n * 2).fill(1);
      for (let i = 0; i < n; i++) {
        Y[i * 2] = initPts[i][0] * scale + (rand() - 0.5) * 1e-4;
        Y[i * 2 + 1] = initPts[i][1] * scale + (rand() - 0.5) * 1e-4;
      }
      const ITER = 350, EXAG_UNTIL = 100, ETA = 150;
      const Qnum = new Float64Array(n * n);
      for (let it = 0; it < ITER; it++) {
        if (runId !== tsneRun) return; // superseded by a newer vector set
        const exag = it < EXAG_UNTIL ? 12 : 1;
        const momentum = it < EXAG_UNTIL ? 0.5 : 0.8;
        let qsum = 0;
        for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
          const dx = Y[i * 2] - Y[j * 2], dy = Y[i * 2 + 1] - Y[j * 2 + 1];
          const q = 1 / (1 + dx * dx + dy * dy);
          Qnum[i * n + j] = q; Qnum[j * n + i] = q; qsum += 2 * q;
        }
        for (let i = 0; i < n; i++) {
          let gx = 0, gy = 0;
          for (let j = 0; j < n; j++) {
            if (i === j) continue;
            const q = Qnum[i * n + j];
            const mult = (exag * P[i * n + j] - Math.max(q / qsum, 1e-12)) * q;
            gx += 4 * mult * (Y[i * 2] - Y[j * 2]);
            gy += 4 * mult * (Y[i * 2 + 1] - Y[j * 2 + 1]);
          }
          const k = i * 2;
          gains[k] = Math.max(0.01, (gx > 0) === (dY[k] > 0) ? gains[k] * 0.8 : gains[k] + 0.2);
          gains[k + 1] = Math.max(0.01, (gy > 0) === (dY[k + 1] > 0) ? gains[k + 1] * 0.8 : gains[k + 1] + 0.2);
          dY[k] = momentum * dY[k] - ETA * gains[k] * gx;
          dY[k + 1] = momentum * dY[k + 1] - ETA * gains[k + 1] * gy;
        }
        let cx = 0, cy = 0;
        for (let i = 0; i < n; i++) { Y[i * 2] += dY[i * 2]; Y[i * 2 + 1] += dY[i * 2 + 1]; cx += Y[i * 2]; cy += Y[i * 2 + 1]; }
        cx /= n; cy /= n;
        for (let i = 0; i < n; i++) { Y[i * 2] -= cx; Y[i * 2 + 1] -= cy; }
        if (it % 40 === 39) await new Promise((r) => setTimeout(r, 0));
      }
      if (runId !== tsneRun) return;
      const pts = new Map();
      cards.forEach((c, i) => pts.set(c.id, [Y[i * 2], Y[i * 2 + 1]]));
      tsneCache.set(key, pts);
      if (tsneCache.size > 8) tsneCache.delete(tsneCache.keys().next().value);
      updateOverviewPlot(); // dots transition to their t-SNE spots
    } finally {
      if (tsneBusyKey === key) tsneBusyKey = null;
    }
  }

  function projectionSuffix() {
    if (projection !== 'tsne') return '';
    const cards = state.cards;
    if (cards.length <= 3) return ' · too few cards for t-SNE — PCA layout';
    const useSemantic = semanticState === 'ready' && haveSemanticFor(cards);
    return tsneReady(cards, tsneKey(cards, useSemantic)) ? ' · t-SNE layout' : ' · t-SNE settling…';
  }

  function overviewStatusText() {
    let base;
    switch (semanticState) {
      case 'ready': base = 'positioned by meaning · MiniLM sentence embeddings'; break;
      case 'loading': base = 'positioned by keyword overlap — reading the questions…'; break;
      case 'unavailable': base = 'positioned by keyword overlap — language model offline'; break;
      default: base = 'positioned by keyword overlap';
    }
    return base + projectionSuffix();
  }

  const haveSemanticFor = (cards) =>
    cards.length > 0 && cards.every((c) => semanticCache.has(textHash(cardText(c))));

  // Lay out ALL cards together so a dot keeps its place when tag/priority
  // filters hide its neighbours.
  function overviewCoords(cards) {
    if (cards.length === 0) return new Map();
    if (cards.length === 1) return new Map([[cards[0].id, { x: 0.5, y: 0.5 }]]);
    const useSemantic = semanticState === 'ready' && haveSemanticFor(cards);
    const vecs = useSemantic
      ? cards.map((c) => semanticCache.get(textHash(cardText(c))))
      : cards.map((c) => localEmbed(cardText(c)));
    const pcaPts = pca2(vecs);
    if (projection === 'tsne' && cards.length > 3) { // 2-3 dots: t-SNE degenerates, PCA stands in
      const key = tsneKey(cards, useSemantic);
      if (tsneReady(cards, key)) {
        const cached = tsneCache.get(key);
        return normalizePoints(cards, cards.map((c) => cached.get(c.id)));
      }
      Promise.resolve().then(() => computeTsne(cards, vecs, pcaPts, key));
    }
    return normalizePoints(cards, pcaPts);
  }

  function getExtractor() {
    if (extractorPromise) return extractorPromise;
    extractorPromise = (async () => {
      const mod = await import('https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2');
      mod.env.allowLocalModels = false; // fetch weights from the HuggingFace hub
      return mod.pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2');
    })();
    return extractorPromise;
  }

  // Load the model once, embed any not-yet-embedded questions, then slide the
  // dots to their semantic positions. Every failure degrades to the keyword
  // layout — the network is never required. Set window.QBOARD_DISABLE_SEMANTIC
  // to force the offline path (the e2e suite does this to stay network-free).
  async function ensureSemanticLayout() {
    if (semanticState === 'unavailable') return;
    if (window.QBOARD_DISABLE_SEMANTIC) { semanticState = 'unavailable'; updateOverviewStatus(); return; }
    const cards = state.cards.slice();
    if (haveSemanticFor(cards)) {
      if (semanticState !== 'ready') { semanticState = 'ready'; updateOverviewPlot(); }
      return;
    }
    if (semanticState === 'loading') return;
    semanticState = 'loading';
    updateOverviewStatus();
    try {
      const extractor = await getExtractor();
      for (const c of cards) {
        const key = textHash(cardText(c));
        if (semanticCache.has(key)) continue;
        const out = await extractor(cardText(c) || ' ', { pooling: 'mean', normalize: true });
        semanticCache.set(key, Float32Array.from(out.data));
      }
      semanticState = 'ready';
    } catch (err) {
      console.warn('Semantic layout unavailable — keeping the keyword-overlap map.', err);
      semanticState = 'unavailable';
    }
    updateOverviewPlot();
  }

  function updateOverviewStatus() {
    const s = document.querySelector('#board .plot-status');
    if (s) s.textContent = overviewStatusText();
  }

  // Reposition the existing dots (they CSS-transition to their new spots)
  // rather than re-render, so the shift to the semantic layout animates.
  function updateOverviewPlot() {
    if (view !== 'overview') return;
    const field = document.querySelector('#board .plot-field');
    if (!field) return;
    const coords = overviewCoords(state.cards);
    for (const d of field.querySelectorAll('.plot-dot')) {
      const c = coords.get(d.dataset.id);
      if (c) { d.style.left = `${c.x * 100}%`; d.style.top = `${c.y * 100}%`; }
    }
    updateOverviewStatus();
  }

  function buildCrosshair(xLabel, yLabel) {
    const cross = document.createElement('div');
    cross.className = 'plot-cross';
    const vx = document.createElement('span'); vx.className = 'plot-cross-x';
    const vy = document.createElement('span'); vy.className = 'plot-cross-y';
    const xl = document.createElement('span'); xl.className = 'plot-axis-x'; xl.textContent = `${xLabel} →`;
    const yl = document.createElement('span'); yl.className = 'plot-axis-y'; yl.textContent = `${yLabel} ↑`;
    cross.append(vx, vy, xl, yl);
    return cross;
  }

  function renderOverview() {
    const sheet = document.createElement('div');
    sheet.className = 'plot-sheet';

    const head = document.createElement('div');
    head.className = 'plot-head';
    const title = document.createElement('h2');
    title.className = 'plot-title';
    title.textContent = 'Overview';
    const caption = document.createElement('p');
    caption.className = 'plot-caption';
    caption.textContent = 'Everything on your mind, mapped by meaning — the closer two dots sit, the more alike they read.';
    const status = document.createElement('p');
    status.className = 'plot-status';
    status.textContent = overviewStatusText();

    const projToggle = document.createElement('div');
    projToggle.className = 'plot-proj-toggle';
    projToggle.setAttribute('role', 'group');
    projToggle.setAttribute('aria-label', 'Map projection');
    for (const p of Object.keys(PROJ_LABEL)) {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.proj = p;
      b.textContent = PROJ_LABEL[p];
      b.setAttribute('aria-pressed', String(projection === p));
      b.addEventListener('click', () => {
        if (projection === p) return;
        projection = p;
        try { localStorage.setItem(PROJ_KEY, p); } catch { /* private mode */ }
        render();
        announce(`Overview projection: ${PROJ_LABEL[p]}`);
      });
      projToggle.append(b);
    }
    head.append(title, caption, status, projToggle);
    sheet.append(head, renderPlotLegend());

    const field = document.createElement('div');
    field.className = 'plot-field';
    const tsneAxes = projection === 'tsne' && state.cards.length > 3;
    field.append(tsneAxes ? buildCrosshair('t-SNE-1', 't-SNE-2') : buildCrosshair('PC-1', 'PC-2'));

    const all = state.cards;
    if (all.length === 0) {
      field.append(plotEmptyHint('Add a card and it will appear on the map'));
      sheet.append(field);
      return sheet;
    }

    const visible = all.filter(matchesFilters);
    const coords = overviewCoords(all);
    for (const card of visible) {
      const c = coords.get(card.id);
      if (c) field.append(renderPlotDot(card, c.x * 100, c.y * 100));
    }
    if (visible.length === 0) field.append(plotEmptyHint('No cards match'));

    sheet.append(field);
    // Run after this sheet is attached to #board so status/position updates land.
    Promise.resolve().then(ensureSemanticLayout); // upgrade to semantic positions in the background
    return sheet;
  }

  // --------------------------------------------------------------------------
  // Matrix view — four lenses on the same cards. Importance is always the
  // vertical axis; the picker swaps what it is crossed with: urgency
  // (Eisenhower), effort (Leverage), control (Serenity) or age since last
  // touch (Follow-through). One quadrant grid serves them all.
  // --------------------------------------------------------------------------

  const DAY = 86400000;
  const ageBucket = (card) => {
    const age = Date.now() - (card.updatedAt || card.createdAt || Date.now());
    return age < 14 * DAY ? 'fresh' : age < 45 * DAY ? 'aging' : 'stale';
  };

  // Urgent sits on the LEFT (the classic Eisenhower orientation): the eye
  // lands on "Answer now" first. Cell keys are `${importance}|${column}`.
  const MATRICES = {
    eisenhower: {
      label: 'Eisenhower',
      caption: 'The Eisenhower matrix — importance against urgency, urgent on the left. Set both on a card to place it here.',
      axisX: '← URGENCY',
      cols: ['high', 'low'],
      colOf: (c) => c.urgency,
      placed: (c) => Boolean(c.importance && c.urgency),
      pending: (cards) => cards.filter((c) => !(c.importance && c.urgency)).length,
      awaiting: 'importance & urgency',
      cells: {
        'high|high': { verb: 'Answer now', sub: 'important · urgent', accent: 'var(--high)' },
        'high|low':  { verb: 'Schedule', sub: 'important · not urgent', accent: 'var(--ink-blue)' },
        'low|high':  { verb: 'Delegate', sub: 'not important · urgent', accent: 'var(--ink-amber)' },
        'low|low':   { verb: 'Drop', sub: 'not important · not urgent', accent: 'var(--ink-soft)' },
      },
    },
    leverage: {
      label: 'Leverage',
      caption: 'Importance against effort — where a little work moves a lot, and which chores quietly eat a week.',
      axisX: 'EFFORT →',
      cols: ['low', 'medium', 'high'],
      colOf: (c) => effortVal(c.effort),
      placed: (c) => Boolean(c.importance),
      pending: (cards) => cards.filter((c) => !c.importance).length,
      awaiting: 'importance',
      cells: {
        'high|low':    { verb: 'Quick win', sub: 'important · low effort', accent: 'var(--high)' },
        'high|medium': { verb: 'Solid bet', sub: 'important · medium effort', accent: 'var(--ink-blue)' },
        'high|high':   { verb: 'Big bet', sub: 'important · high effort', accent: 'var(--ink-amber)' },
        'low|low':     { verb: 'Fill-in', sub: 'minor · low effort', accent: 'var(--ink-soft)' },
        'low|medium':  { verb: 'Meh', sub: 'minor · medium effort', accent: 'var(--ink-soft)' },
        'low|high':    { verb: 'Time sink', sub: 'minor · high effort', accent: 'var(--ink-amber)' },
      },
    },
    serenity: {
      label: 'Serenity',
      caption: 'Importance against control — what deserves action, and what you are allowed to put down.',
      axisX: 'CONTROL →',
      cols: ['act', 'influence', 'none'],
      colOf: (c) => controlVal(c.control),
      placed: (c) => Boolean(c.importance),
      pending: (cards) => cards.filter((c) => !c.importance).length,
      awaiting: 'importance',
      cells: {
        'high|act':       { verb: 'Act now', sub: 'important · in your hands', accent: 'var(--high)' },
        'high|influence': { verb: 'Nudge', sub: 'important · can influence', accent: 'var(--ink-blue)' },
        'high|none':      { verb: 'Accept & plan', sub: 'important · out of your hands', accent: 'var(--ink-amber)' },
        'low|act':        { verb: 'Easy win', sub: 'minor · in your hands', accent: 'var(--ink-blue)' },
        'low|influence':  { verb: 'Mention it', sub: 'minor · can influence', accent: 'var(--ink-soft)' },
        'low|none':       { verb: 'Let go', sub: 'minor · out of your hands', accent: 'var(--ink-soft)' },
      },
    },
    followthrough: {
      label: 'Follow-through',
      caption: 'Importance against time since a card was last touched. Answered cards rest; everything else ages.',
      axisX: 'AGE →',
      cols: ['fresh', 'aging', 'stale'],
      colOf: ageBucket,
      placed: (c) => Boolean(c.importance) && c.columnId !== 'answered',
      pending: (cards) => cards.filter((c) => c.columnId !== 'answered' && !c.importance).length,
      awaiting: 'importance',
      cells: {
        'high|fresh': { verb: 'On it', sub: 'important · touched < 2 weeks', accent: 'var(--ink-blue)' },
        'high|aging': { verb: 'Watch', sub: 'important · 2–6 weeks old', accent: 'var(--ink-amber)' },
        'high|stale': { verb: 'Rescue', sub: 'important · > 6 weeks untouched', accent: 'var(--high)' },
        'low|fresh':  { verb: 'Fine', sub: 'minor · recently touched', accent: 'var(--ink-soft)' },
        'low|aging':  { verb: 'Fine', sub: 'minor · 2–6 weeks old', accent: 'var(--ink-soft)' },
        'low|stale':  { verb: 'Let go?', sub: 'minor · > 6 weeks untouched', accent: 'var(--ink-amber)' },
      },
    },
  };

  const MATRIX_KEY = 'question-board:matrix';
  let matrixLens = 'eisenhower';
  try {
    const m = localStorage.getItem(MATRIX_KEY);
    if (Object.hasOwn(MATRICES, m)) matrixLens = m;
  } catch (_) { /* private mode */ }

  function matrixAxis(className, text) {
    const el = document.createElement('span');
    el.className = className;
    el.textContent = text;
    return el;
  }

  function renderMatrix() {
    const lens = MATRICES[matrixLens];
    const sheet = document.createElement('div');
    sheet.className = 'plot-sheet matrix-plate';

    const head = document.createElement('div');
    head.className = 'plot-head';
    const title = document.createElement('h2');
    title.className = 'plot-title';
    title.textContent = 'Matrix';

    const pick = document.createElement('div');
    pick.className = 'matrix-switch';
    pick.setAttribute('role', 'group');
    pick.setAttribute('aria-label', 'Choose a matrix');
    for (const [id, m] of Object.entries(MATRICES)) {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.matrix = id;
      b.textContent = m.label;
      b.setAttribute('aria-pressed', String(id === matrixLens));
      b.addEventListener('click', () => {
        if (matrixLens === id) return;
        matrixLens = id;
        try { localStorage.setItem(MATRIX_KEY, id); } catch (_) { /* private mode */ }
        render();
        announce(`${m.label} matrix`);
      });
      pick.append(b);
    }

    const caption = document.createElement('p');
    caption.className = 'plot-caption';
    caption.textContent = lens.caption;
    const status = document.createElement('p');
    status.className = 'plot-status';
    const placed = state.cards.filter((c) => lens.placed(c) && matchesFilters(c));
    const awaiting = lens.pending(state.cards);
    status.textContent = `${placed.length} placed`
      + (awaiting ? ` · ${awaiting} awaiting ${lens.awaiting}` : '');
    head.append(title, pick, caption, status);
    sheet.append(head, renderPlotLegend());

    const grid = document.createElement('div');
    grid.className = 'matrix-grid';
    grid.style.gridTemplateColumns = `24px repeat(${lens.cols.length}, 1fr)`;
    grid.append(matrixAxis('matrix-axis-imp', 'IMPORTANCE ↑'));
    const axisX = matrixAxis('matrix-axis-urg', lens.axisX);
    axisX.style.gridColumn = `2 / span ${lens.cols.length}`;
    axisX.style.gridRow = '3';
    grid.append(axisX);

    for (const imp of ['high', 'low']) {
      lens.cols.forEach((col, x) => {
        const q = lens.cells[`${imp}|${col}`];
        const cell = document.createElement('section');
        cell.className = 'matrix-quad';
        cell.dataset.imp = imp;
        cell.dataset.x = col;
        if (matrixLens === 'eisenhower') cell.dataset.urg = col; // test-stable selector
        cell.style.gridColumn = String(2 + x);
        cell.style.gridRow = imp === 'high' ? '1' : '2';
        cell.style.setProperty('--quad', q.accent);
        cell.setAttribute('aria-label', `${q.verb} — ${q.sub}`);

        const qhead = document.createElement('div');
        qhead.className = 'matrix-quad-head';
        const verb = document.createElement('span');
        verb.className = 'matrix-quad-verb';
        verb.textContent = q.verb;
        const sub = document.createElement('span');
        sub.className = 'matrix-quad-sub';
        sub.textContent = q.sub;
        qhead.append(verb, sub);

        const cards = placed.filter((c) => c.importance === imp && lens.colOf(c) === col);
        const count = document.createElement('span');
        count.className = 'matrix-quad-count';
        count.textContent = cards.length;
        qhead.append(count);
        cell.append(qhead);

        const dots = document.createElement('div');
        dots.className = 'matrix-quad-dots';
        for (const card of cards) dots.append(renderPlotDot(card));
        cell.append(dots);
        grid.append(cell);
      });
    }

    sheet.append(grid);
    return sheet;
  }

  // --------------------------------------------------------------------------
  // Areas view — one small-multiples tile per life area plus an attention
  // wheel, answering "which part of my life is starved?" at a glance. A tile
  // click focuses the area: the category filter follows and a category-aware
  // detail panel (cooling-off, learning, serenity, staleness) opens below.
  // --------------------------------------------------------------------------

  const SVGNS = 'http://www.w3.org/2000/svg';
  const WEEK = 7 * DAY;
  const isOpen = (c) => c.columnId !== 'answered';

  function humanAge(ms) {
    const d = Math.floor(ms / DAY);
    if (d < 1) return 'today';
    if (d < 14) return `${d} day${d === 1 ? '' : 's'}`;
    if (d < 61) return `${Math.round(d / 7)} weeks`;
    if (d < 365) return `${Math.round(d / 30.4)} months`;
    return `${+(d / 365).toFixed(1)} years`;
  }

  function areaStats(catId) {
    const cards = state.cards.filter((c) => c.category === catId);
    const open = cards.filter(isOpen);
    const oldest = open.reduce((m, c) => Math.min(m, c.createdAt), Infinity);
    const top = open.slice().sort((a, b) =>
      (b.importance === 'high') - (a.importance === 'high') || a.createdAt - b.createdAt)[0];
    return { cards, open, oldestAge: open.length ? Date.now() - oldest : 0, top };
  }

  // Attention wheel — spoke length is open-question mass (high importance
  // counts double). Purely derived from the board: no scoring ritual to keep up.
  function renderWheel(cats) {
    const SIZE = 260, CX = SIZE / 2, CY = SIZE / 2, R = 88;
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'wheel');
    svg.setAttribute('viewBox', `0 0 ${SIZE} ${SIZE}`);
    svg.setAttribute('width', SIZE);
    svg.setAttribute('height', SIZE);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Attention wheel — open questions per life area');

    for (const f of [0.5, 1]) {
      const ring = document.createElementNS(SVGNS, 'circle');
      ring.setAttribute('cx', CX); ring.setAttribute('cy', CY); ring.setAttribute('r', R * f);
      ring.setAttribute('class', 'wheel-ring');
      svg.append(ring);
    }

    const masses = cats.map(({ stats }) =>
      stats.open.reduce((s, c) => s + (c.importance === 'high' ? 2 : 1), 0));
    const maxMass = Math.max(1, ...masses);
    const pts = [];
    cats.forEach(({ cat, stats }, i) => {
      const a = -Math.PI / 2 + (i / cats.length) * Math.PI * 2;
      const frac = 0.1 + 0.9 * (masses[i] / maxMass);
      const x = CX + Math.cos(a) * R * frac, y = CY + Math.sin(a) * R * frac;
      pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);

      const spoke = document.createElementNS(SVGNS, 'line');
      spoke.setAttribute('x1', CX); spoke.setAttribute('y1', CY);
      spoke.setAttribute('x2', CX + Math.cos(a) * R); spoke.setAttribute('y2', CY + Math.sin(a) * R);
      spoke.setAttribute('class', 'wheel-spoke');
      svg.append(spoke);

      const dot = document.createElementNS(SVGNS, 'circle');
      dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1)); dot.setAttribute('r', 3);
      dot.style.fill = catColor(cat.id);
      svg.append(dot);

      const lx = CX + Math.cos(a) * (R + 18), ly = CY + Math.sin(a) * (R + 18);
      const label = document.createElementNS(SVGNS, 'text');
      label.setAttribute('x', lx.toFixed(1)); label.setAttribute('y', ly.toFixed(1));
      label.setAttribute('text-anchor', Math.abs(Math.cos(a)) < 0.3 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end');
      label.setAttribute('dominant-baseline', 'middle');
      label.style.fill = catColor(cat.id);
      label.textContent = `${cat.label} ${stats.open.length}`;
      svg.append(label);
    });

    if (cats.length > 1) {
      const poly = document.createElementNS(SVGNS, 'polygon');
      poly.setAttribute('points', pts.join(' '));
      poly.setAttribute('class', 'wheel-shape');
      // insert under the dots/labels so it never obscures them
      svg.insertBefore(poly, svg.children[2]);
    }
    return svg;
  }

  // 12-week activity sparkline: cards created or touched per week.
  function renderSparkline(cards) {
    const W = 120, H = 26, BINS = 12;
    const now = Date.now();
    const bins = new Array(BINS).fill(0);
    for (const c of cards) {
      const wc = Math.floor((now - c.createdAt) / WEEK);
      if (wc >= 0 && wc < BINS) bins[BINS - 1 - wc]++;
      const wu = Math.floor((now - c.updatedAt) / WEEK);
      if (wu >= 0 && wu < BINS && wu !== wc) bins[BINS - 1 - wu]++;
    }
    const max = Math.max(1, ...bins);
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'area-spark');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    svg.setAttribute('aria-hidden', 'true');
    const line = document.createElementNS(SVGNS, 'polyline');
    line.setAttribute('points', bins.map((v, i) =>
      `${(i * W / (BINS - 1)).toFixed(1)},${(H - 3 - (v / max) * (H - 6)).toFixed(1)}`).join(' '));
    svg.append(line);
    return svg;
  }

  function renderAreas() {
    const sheet = document.createElement('div');
    sheet.className = 'plot-sheet areas-sheet';

    const inUse = categories
      .map((cat) => ({ cat, stats: areaStats(cat.id) }))
      .filter(({ stats }) => stats.cards.length > 0);
    const openTotal = inUse.reduce((s, { stats }) => s + stats.open.length, 0);

    const head = document.createElement('div');
    head.className = 'plot-head';
    const title = document.createElement('h2');
    title.className = 'plot-title';
    title.textContent = 'Areas';
    const caption = document.createElement('p');
    caption.className = 'plot-caption';
    caption.textContent = 'Every life area side by side — a lopsided wheel means a starved corner. Click an area for a closer look.';
    const status = document.createElement('p');
    status.className = 'plot-status';
    status.textContent = `${openTotal} open across ${inUse.length} area${inUse.length === 1 ? '' : 's'}`;
    head.append(title, caption, status);
    sheet.append(head);

    if (inUse.length === 0) {
      sheet.append(plotEmptyHint('Give a card a category and its life area appears here'));
      return sheet;
    }

    const wheelWrap = document.createElement('div');
    wheelWrap.className = 'wheel-wrap';
    wheelWrap.append(renderWheel(inUse));
    sheet.append(wheelWrap);

    const grid = document.createElement('div');
    grid.className = 'areas-grid';
    for (const { cat, stats } of inUse) {
      const tile = document.createElement('button');
      tile.type = 'button';
      tile.className = 'area-tile';
      tile.dataset.cat = cat.id;
      tile.style.setProperty('--cat', catColor(cat.id));
      // ink-fade staleness tint: fully saturated at 6 months of carrying
      tile.style.setProperty('--stale', Math.min(1, stats.oldestAge / (180 * DAY)).toFixed(2));
      tile.setAttribute('aria-pressed', String(filters.category === cat.id));

      const th = document.createElement('span');
      th.className = 'area-tile-head';
      const name = document.createElement('span');
      name.className = 'area-tile-name';
      name.textContent = cat.label;
      const count = document.createElement('span');
      count.className = 'area-tile-count';
      count.textContent = `${stats.open.length} open`;
      th.append(name, count);

      const age = document.createElement('span');
      age.className = 'area-tile-age';
      age.textContent = stats.open.length
        ? (stats.oldestAge < DAY ? 'all fresh today' : `carrying ${humanAge(stats.oldestAge)}`)
        : 'all answered';

      tile.append(th, age);
      if (stats.top) {
        const top = document.createElement('span');
        top.className = 'area-tile-top';
        top.textContent = stats.top.title;
        tile.append(top);
      }
      tile.append(renderSparkline(stats.cards));

      tile.addEventListener('click', () => {
        filters.category = filters.category === cat.id ? '' : cat.id;
        render();
        announce(filters.category ? `${cat.label} in focus` : 'Area focus cleared');
      });
      grid.append(tile);
    }
    sheet.append(grid);

    if (filters.category && inUse.some(({ cat }) => cat.id === filters.category)) {
      sheet.append(renderAreaDetail(filters.category));
    }
    return sheet;
  }

  function detailPanel(heading, hint) {
    const panel = document.createElement('section');
    panel.className = 'area-panel';
    const h = document.createElement('h4');
    h.textContent = heading;
    panel.append(h);
    if (hint) {
      const p = document.createElement('p');
      p.className = 'panel-hint';
      p.textContent = hint;
      panel.append(p);
    }
    return panel;
  }

  function areaRow(card) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'area-row';
    b.dataset.id = card.id;
    const num = document.createElement('span');
    num.className = 'card-num';
    num.textContent = qLabel(card);
    const t = document.createElement('span');
    t.className = 'area-row-title';
    t.textContent = card.title;
    b.append(num, t);
    b.addEventListener('click', () => openDialog(card.id));
    return b;
  }

  function renderAreaDetail(catId) {
    const wrap = document.createElement('section');
    wrap.className = 'area-detail';
    wrap.style.setProperty('--cat', catColor(catId));
    const h = document.createElement('h3');
    h.textContent = `${catLabel(catId)} — a closer look`;
    wrap.append(h);

    const cards = state.cards.filter((c) => c.category === catId);
    const open = cards.filter(isOpen);

    const purchases = open.filter((c) => c.tags.includes('purchase'));
    if (purchases.length) wrap.append(renderCooloffPanel(catId, purchases));
    if (catId === 'mind' && cards.some((c) => c.tags.length)) wrap.append(renderLearningPanel(cards));
    const problems = open.filter((c) => c.type === 'problem');
    if (problems.length) wrap.append(renderSerenityStrip(problems));
    wrap.append(renderStalenessPanel(open));
    return wrap;
  }

  // 30-day rule: a wanted thing waits a month; still wanted when the window
  // matures ("decide now") is a real want, everything let go in Trash counts
  // as money left unspent.
  function renderCooloffPanel(catId, purchases) {
    const panel = detailPanel('Cooling-off', 'The 30-day rule — want it just as much a month later? Then decide.');
    panel.classList.add('area-cooloff');
    const list = document.createElement('div');
    list.className = 'cooloff-list';
    for (const c of purchases.slice().sort((a, b) => a.createdAt - b.createdAt)) {
      const row = areaRow(c);
      row.classList.add('cooloff-row');
      const left = 30 - Math.floor((Date.now() - c.createdAt) / DAY);
      const days = document.createElement('span');
      days.className = 'cooloff-days';
      if (left > 0) days.textContent = `${left} d left`;
      else { days.textContent = 'decide now'; days.dataset.due = 'true'; }
      row.append(days);
      list.append(row);
    }
    panel.append(list);

    const resisted = document.createElement('p');
    resisted.className = 'cooloff-resisted';
    resisted.hidden = true;
    panel.append(resisted);
    fetchTrash().then((trash) => {
      const n = trash.filter((c) => c.category === catId && c.tags.includes('purchase')).length;
      if (n > 0) {
        resisted.textContent = `${n} resisted — sent to Trash unbought.`;
        resisted.hidden = false;
      }
    });
    return panel;
  }

  // Learning progress — per co-tag answered-vs-open bars plus a burn-up of
  // questions asked vs answered over time.
  function renderLearningPanel(cards) {
    const panel = detailPanel('Learning progress', 'Per topic: answered vs still open.');
    panel.classList.add('area-learning');

    const byTag = new Map();
    for (const c of cards) for (const t of c.tags) {
      const e = byTag.get(t) || { open: 0, done: 0 };
      if (isOpen(c)) e.open++; else e.done++;
      byTag.set(t, e);
    }
    const bars = document.createElement('div');
    bars.className = 'learn-bars';
    const ranked = [...byTag].sort((a, b) => (b[1].open + b[1].done) - (a[1].open + a[1].done));
    for (const [tag, e] of ranked) {
      const total = e.open + e.done;
      const row = document.createElement('div');
      row.className = 'learn-row';
      const name = document.createElement('span');
      name.className = 'learn-tag';
      name.textContent = tag;
      const bar = document.createElement('div');
      bar.className = 'learn-bar';
      const done = document.createElement('span');
      done.className = 'learn-done';
      done.style.width = `${(e.done / total) * 100}%`;
      const openSeg = document.createElement('span');
      openSeg.className = 'learn-open';
      openSeg.style.width = `${(e.open / total) * 100}%`;
      bar.append(done, openSeg);
      const n = document.createElement('span');
      n.className = 'learn-count';
      n.textContent = `${e.done}/${total}`;
      row.append(name, bar, n);
      bars.append(row);
    }
    panel.append(bars, renderBurnup(cards));
    return panel;
  }

  // Burn-up: cumulative asked (soft ink) vs cumulative answered (category ink).
  function renderBurnup(cards) {
    const W = 260, H = 60, PAD = 4;
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'learn-burnup');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    svg.setAttribute('aria-hidden', 'true');
    const t0 = Math.min(...cards.map((c) => c.createdAt));
    const span = Math.max(1, Date.now() - t0);
    const created = cards.map((c) => c.createdAt).sort((a, b) => a - b);
    const answered = cards.filter((c) => !isOpen(c)).map((c) => c.updatedAt).sort((a, b) => a - b);
    const total = created.length;
    const lineFor = (events, cls) => {
      const line = document.createElementNS(SVGNS, 'polyline');
      line.setAttribute('class', cls);
      const pts = [];
      const STEPS = 24;
      for (let s = 0; s <= STEPS; s++) {
        const t = t0 + (span * s) / STEPS;
        let n = 0;
        while (n < events.length && events[n] <= t) n++;
        pts.push(`${(PAD + ((W - 2 * PAD) * s) / STEPS).toFixed(1)},${(H - PAD - ((H - 2 * PAD) * n) / total).toFixed(1)}`);
      }
      line.setAttribute('points', pts.join(' '));
      return line;
    };
    svg.append(lineFor(created, 'burnup-asked'), lineFor(answered, 'burnup-answered'));
    return svg;
  }

  // CBT worry triage: what you can act on, what you can only nudge, and what
  // is out of your hands — the last pile is for the weekly worry window.
  function renderSerenityStrip(problems) {
    const panel = detailPanel('Serenity check',
      'Problems sorted by what you can do about them. Visit the “out of my hands” pile once, in a weekly worry window — on schedule, not on loop.');
    panel.classList.add('serenity-strip');
    const groups = document.createElement('div');
    groups.className = 'serenity-groups';
    for (const ctl of ['act', 'influence', 'none']) {
      const group = document.createElement('div');
      group.className = 'serenity-group';
      group.dataset.control = ctl;
      const gh = document.createElement('h5');
      gh.textContent = CONTROL_LABEL[ctl];
      group.append(gh);
      for (const c of problems.filter((p) => controlVal(p.control) === ctl)) {
        group.append(areaRow(c));
      }
      groups.append(group);
    }
    panel.append(groups);
    return panel;
  }

  // Personal-CRM "last touched": the open cards this area is silently dropping.
  function renderStalenessPanel(open) {
    const panel = detailPanel('Last touched', 'Oldest first — what this area might be silently dropping.');
    panel.classList.add('area-stale');
    if (!open.length) {
      const p = document.createElement('p');
      p.className = 'panel-hint';
      p.textContent = 'Nothing open here — clean desk.';
      panel.append(p);
      return panel;
    }
    const list = document.createElement('div');
    list.className = 'stale-list';
    for (const c of open.slice().sort((a, b) => a.updatedAt - b.updatedAt)) {
      const row = areaRow(c);
      row.classList.add('stale-row');
      const age = document.createElement('span');
      age.className = 'stale-age';
      const ms = Date.now() - c.updatedAt;
      age.textContent = ms < DAY ? 'today' : `${humanAge(ms)} ago`;
      row.append(age);
      list.append(row);
    }
    panel.append(list);
    return panel;
  }

  // --------------------------------------------------------------------------
  // Review view — GTD's weekly review as a screen: the ritual that keeps every
  // other view trustworthy. Stat tiles, week-over-week drift per area, the
  // neglect list, and three resurfaced old thoughts (deterministic per day, so
  // the same ritual shows the same cards all day).
  // --------------------------------------------------------------------------

  const REVIEW_KEY = 'question-board:reviewed';
  const RESURFACE_KEY = 'question-board:resurface';
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
    num.textContent = qLabel(card);
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
        commit(`Reviewed ${qLabel(c)} — still matters`);
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

  function renderReview() {
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

  // --------------------------------------------------------------------------
  // Assistant view — chat with the brain service via /api/agent/chat
  // --------------------------------------------------------------------------

  const assistantState = { messages: [], busy: false };

  function renderAssistant() {
    const sheet = document.createElement('section');
    sheet.className = 'assistant-sheet';

    const heading = document.createElement('h2');
    heading.textContent = 'Assistant';
    sheet.appendChild(heading);

    const log = document.createElement('div');
    log.className = 'chat-log';
    if (!assistantState.messages.length) {
      const hint = document.createElement('p');
      hint.className = 'chat-status';
      hint.textContent = 'Ask about your board — research a question, triage the inbox, or find connections.';
      log.appendChild(hint);
    }
    for (const msg of assistantState.messages) {
      const el = document.createElement('div');
      el.className = `chat-msg ${msg.role}${msg.error ? ' error' : ''}`;
      el.textContent = msg.content;
      if (msg.steps && msg.steps.length) {
        const steps = document.createElement('div');
        steps.className = 'chat-steps';
        for (const step of msg.steps) {
          const chip = document.createElement('span');
          chip.className = 'chat-step';
          chip.textContent = step.tool;
          steps.appendChild(chip);
        }
        el.appendChild(steps);
      }
      log.appendChild(el);
    }
    sheet.appendChild(log);

    const status = document.createElement('div');
    status.className = 'chat-status';
    status.textContent = assistantState.busy ? 'Thinking…' : '';
    sheet.appendChild(status);

    const form = document.createElement('form');
    form.className = 'chat-composer';
    const input = document.createElement('textarea');
    input.id = 'chat-input';
    input.placeholder = 'Message the assistant…';
    input.disabled = assistantState.busy;
    const send = document.createElement('button');
    send.type = 'submit';
    send.id = 'chat-send';
    send.className = 'btn primary';
    send.textContent = 'Send';
    send.disabled = assistantState.busy;
    form.appendChild(input);
    form.appendChild(send);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text && !assistantState.busy) sendChat(text);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    sheet.appendChild(form);

    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    return sheet;
  }

  async function sendChat(text) {
    assistantState.messages.push({ role: 'user', content: text });
    assistantState.busy = true;
    render();
    try {
      const history = assistantState.messages
        .filter((m) => !m.error && (m.role === 'user' || m.role === 'assistant'))
        .map(({ role, content }) => ({ role, content }));
      const res = await fetch('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history }),
      });
      if (!res.ok) throw new Error(`agent ${res.status}`);
      const data = await res.json();
      assistantState.messages.push({
        role: 'assistant',
        content: data.reply || '',
        steps: data.steps || [],
      });
      if (data.mutated) await adoptServerBoard();
      announce('Assistant replied');
    } catch {
      assistantState.messages.push({
        role: 'assistant',
        content: 'The assistant is unavailable right now. Check that the brain service is running.',
        error: true,
      });
      announce('Assistant unavailable');
    }
    assistantState.busy = false;
    render();
    const nextInput = document.getElementById('chat-input');
    if (nextInput) nextInput.focus();
  }

  async function adoptServerBoard() {
    // The agent mutated the DB through the Node API; adopt the server's board so a
    // debounced local push can't overwrite the agent's change with stale state.
    try {
      const res = await fetch(API);
      if (!res.ok) return;
      const data = await res.json();
      if (data && Array.isArray(data.cards)) {
        const cats = sanitizeCategories(data.categories);
        if (cats) categories = cats;
        state.cards = data.cards;
        saveState();
      }
    } catch { /* offline — keep the local board */ }
  }

  // --------------------------------------------------------------------------
  // Drag & drop
  // --------------------------------------------------------------------------

  const dropIndicator = document.createElement('div');
  dropIndicator.className = 'drop-indicator';

  const clearDropIndicator = () => dropIndicator.remove();

  function getCardAfterPointer(container, y) {
    const cards = [...container.querySelectorAll('.card:not(.dragging)')];
    let closest = { offset: -Infinity, el: null };
    for (const el of cards) {
      const box = el.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, el };
    }
    return closest.el;
  }

  function wireDropZone(cardsEl) {
    cardsEl.addEventListener('dragover', (e) => {
      if (!draggedId) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      cardsEl.classList.add('drop-target');
      const after = getCardAfterPointer(cardsEl, e.clientY);
      if (after) cardsEl.insertBefore(dropIndicator, after);
      else cardsEl.append(dropIndicator);
    });

    cardsEl.addEventListener('dragleave', (e) => {
      if (!cardsEl.contains(e.relatedTarget)) {
        cardsEl.classList.remove('drop-target');
        if (dropIndicator.parentElement === cardsEl) clearDropIndicator();
      }
    });

    cardsEl.addEventListener('drop', (e) => {
      e.preventDefault();
      const id = draggedId || e.dataTransfer.getData('text/plain');
      if (!id) return;
      const after = getCardAfterPointer(cardsEl, e.clientY);
      clearDropIndicator();
      const card = getCard(id);
      moveCard(id, cardsEl.dataset.col, after ? after.dataset.id : null);
      if (card) announce(`Moved “${card.title}” to ${columnTitle(cardsEl.dataset.col)}`);
    });
  }

  // --------------------------------------------------------------------------
  // Keyboard support
  // --------------------------------------------------------------------------

  function onCardKeydown(e, cardId) {
    const card = getCard(cardId);
    if (!card) return;

    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openDialog(cardId);
      return;
    }

    if (e.key === 'Delete' || (e.key === 'Backspace' && e.metaKey)) {
      e.preventDefault();
      deleteCard(cardId);
      return;
    }

    if (e.key === '[' || e.key === ']') {
      e.preventDefault();
      const next = columnIndex(card.columnId) + (e.key === ']' ? 1 : -1);
      if (next < 0 || next >= COLUMNS.length) return;
      focusCardId = cardId;
      moveCard(cardId, COLUMNS[next].id);
      announce(`Moved “${card.title}” to ${COLUMNS[next].title}`);
      return;
    }

    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      const visible = columnCards(card.columnId).filter(matchesFilters);
      const i = visible.findIndex((c) => c.id === cardId);
      if (i === -1) return;
      let beforeId;
      if (e.key === 'ArrowUp') {
        if (i === 0) return;
        beforeId = visible[i - 1].id;
      } else {
        if (i === visible.length - 1) return;
        beforeId = i + 2 < visible.length ? visible[i + 2].id : null;
      }
      focusCardId = cardId;
      moveCard(cardId, card.columnId, beforeId);
      announce(`Moved “${card.title}” ${e.key === 'ArrowUp' ? 'up' : 'down'}`);
    }
  }

  // --------------------------------------------------------------------------
  // Card actions
  // --------------------------------------------------------------------------

  async function deleteCard(cardId) {
    const card = getCard(cardId);
    if (!card) return;
    const sure = await ask({
      title: 'Delete this card?',
      message: `${qLabel(card)} “${card.title}” will be moved off the board. It stays recoverable — bring it back with Undo, or from the History panel — until you delete it permanently there.`,
      okLabel: 'Delete card',
      danger: true,
    });
    if (!sure) return;
    state.cards = state.cards.filter((c) => c.id !== cardId);
    commit(`Deleted ${qLabel(card)} “${short(card.title)}”`);
    announce(`Deleted “${card.title}”`);
  }

  function sortColumnByType(columnId) {
    const sorted = columnCards(columnId).sort((a, b) => TYPE_RANK[a.type] - TYPE_RANK[b.type]);
    state.cards = [...state.cards.filter((c) => c.columnId !== columnId), ...sorted];
    commit(`Sorted ${columnTitle(columnId)} by type`);
    announce(`Sorted ${columnTitle(columnId)} by type`);
  }

  // --------------------------------------------------------------------------
  // Edit dialog
  // --------------------------------------------------------------------------

  const dialog = $('#card-dialog');
  const form = $('#card-form');
  let editingId = null;

  // The type picker is built once (types are fixed); the category picker is
  // rebuilt on every open, because the registry is the user's to change.
  (() => {
    const typeWrap = $('#type-picker-options');
    for (const t of TYPES) {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'type';
      input.value = t;
      const span = document.createElement('span');
      span.className = `badge type-${t}`;
      span.textContent = `${TYPE_META[t].glyph} ${TYPE_META[t].label}`;
      label.append(input, span);
      typeWrap.append(label);
    }
  })();

  function rebuildCategoryPicker() {
    const catWrap = $('#category-picker-options');
    catWrap.innerHTML = '';
    const mkCat = (value, text, color) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'category';
      input.value = value;
      const span = document.createElement('span');
      span.className = 'cat-swatch';
      span.style.setProperty('--cat', color);
      span.textContent = text;
      label.append(input, span);
      return label;
    };
    catWrap.append(mkCat('', 'None', 'var(--ink-soft)'));
    for (const c of categories) catWrap.append(mkCat(c.id, c.label, catColor(c.id)));
  }

  // Decisional balance — on a card tagged "decision", notes lines beginning
  // with + / − (or -) read back as a live two-column pro/con sheet under the
  // notes box. Pure rendering: the notes text stays the single source.
  function updateBalancePreview() {
    const preview = $('#balance-preview');
    const tags = $('#card-tags').value.split(',').map((t) => t.trim().toLowerCase());
    const pros = [], cons = [];
    if (tags.includes('decision')) {
      for (const line of $('#card-notes').value.split('\n')) {
        const t = line.trim();
        if (t.startsWith('+')) pros.push(t.slice(1).trim());
        else if (t.startsWith('-') || t.startsWith('−')) cons.push(t.slice(1).trim());
      }
    }
    const show = pros.length > 0 || cons.length > 0;
    preview.hidden = !show;
    if (!show) return;
    const fill = (colSel, items) => {
      const ul = $(`${colSel} ul`, preview);
      ul.innerHTML = '';
      for (const text of items) {
        const li = document.createElement('li');
        li.textContent = text;
        ul.append(li);
      }
    };
    fill('.balance-pro', pros);
    fill('.balance-con', cons);
  }
  $('#card-notes').addEventListener('input', updateBalancePreview);
  $('#card-tags').addEventListener('input', updateBalancePreview);

  function openDialog(cardId) {
    const card = getCard(cardId);
    if (!card) return;
    editingId = cardId;
    rebuildCategoryPicker();
    $('#card-title').value = card.title;
    $('#card-notes').value = card.notes;
    $('#card-tags').value = card.tags.join(', ');
    updateBalancePreview();
    $('#card-importance').value = iuVal(card.importance);
    $('#card-urgency').value = iuVal(card.urgency);
    $('#card-effort').value = effortVal(card.effort);
    $('#card-control').value = controlVal(card.control);
    for (const radio of form.elements.type) radio.checked = radio.value === card.type;
    for (const radio of form.elements.category) radio.checked = radio.value === (card.category || '');
    const fmt = (ts) => new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    $('#card-meta').textContent =
      `${qLabel(card)} · in ${columnTitle(card.columnId)} · added ${fmt(card.createdAt)} · updated ${fmt(card.updatedAt)}`;
    dialog.showModal();
    $('#card-title').focus();
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const card = getCard(editingId);
    if (card) {
      card.title = $('#card-title').value.trim() || card.title;
      card.notes = $('#card-notes').value;
      card.type = typeVal(form.elements.type.value);
      card.category = catVal(form.elements.category.value);
      card.importance = iuVal($('#card-importance').value);
      card.urgency = iuVal($('#card-urgency').value);
      // A changed effort/control is a human judgment — record the provenance so
      // the brain's future estimator knows never to overwrite it.
      const effort = effortVal($('#card-effort').value);
      if (effort !== effortVal(card.effort)) { card.effort = effort; card.effortSrc = 'user'; }
      const control = controlVal($('#card-control').value);
      if (control !== controlVal(card.control)) { card.control = control; card.controlSrc = 'user'; }
      card.tags = $('#card-tags').value
        .split(',')
        .map((t) => t.trim().toLowerCase())
        .filter(Boolean);
      card.updatedAt = Date.now();
      commit(`Edited ${qLabel(card)} “${short(card.title)}”`);
    }
    dialog.close();
  });

  $('#cancel-dialog').addEventListener('click', () => dialog.close());

  $('#delete-card').addEventListener('click', () => {
    const id = editingId;
    dialog.close();
    deleteCard(id);
  });

  dialog.addEventListener('close', () => { editingId = null; });

  // --------------------------------------------------------------------------
  // Categories editor — the ✎ tab on the rail. Add a life area (name + hue) or
  // remove one; removing never touches cards, they just become uncategorized.
  // --------------------------------------------------------------------------

  const catsDialog = $('#cats-dialog');

  function openCatsDialog() {
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
    categories = categories.filter((c) => c.id !== id);
    if (filters.category === id) filters.category = '';
    const now = Date.now();
    for (const c of state.cards) if (c.category === id) { c.category = ''; c.updatedAt = now; }
    commit(`Removed category “${cat.label}”`);
    announce(`Removed category “${cat.label}”`);
    renderCatsList();
    renderHuePicker();
  }

  $('#close-cats').addEventListener('click', () => catsDialog.close());

  // --------------------------------------------------------------------------
  // Toolbar: search, type filter, the actions menu, theme
  // --------------------------------------------------------------------------

  $('#search').addEventListener('input', (e) => {
    filters.search = e.target.value.trim().toLowerCase();
    render();
  });

  $('#type-filter').addEventListener('change', (e) => {
    filters.type = e.target.value;
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

  const exportDialog = $('#export-dialog');
  const exportJson = () => JSON.stringify({ ...state, categories }, null, 2);

  $('#export-btn').addEventListener('click', () => {
    $('#export-json').value = exportJson();
    exportDialog.showModal();
  });
  $('#cancel-export').addEventListener('click', () => exportDialog.close());

  $('#download-export').addEventListener('click', () => {
    const blob = new Blob([exportJson()], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'lodestar.json';
    a.rel = 'noopener';
    document.body.append(a);
    a.click();
    // Revoke/remove on a later tick — doing it synchronously can cancel the
    // download before the browser has started it (notably Firefox/Safari).
    setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 1000);
    exportDialog.close();
    announce('Exported board as lodestar.json');
  });

  $('#copy-export').addEventListener('click', async () => {
    const btn = $('#copy-export');
    const done = () => {
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = 'Copy JSON'; }, 1600);
      announce('Board JSON copied to clipboard');
    };
    try {
      await navigator.clipboard.writeText(exportJson());
      done();
    } catch (_) {
      // Clipboard API blocked (e.g. sandboxed embed) — select the JSON and
      // try the legacy path; worst case the text is left selected to copy.
      const ta = $('#export-json');
      ta.focus();
      ta.select();
      if (document.execCommand('copy')) {
        done();
      } else {
        btn.textContent = 'Press ⌘C / Ctrl+C';
        setTimeout(() => { btn.textContent = 'Copy JSON'; }, 2500);
        announce('JSON selected — press Ctrl+C or Cmd+C to copy');
      }
    }
  });

  // The import dialog doubles as the file-format reference: the schema below is
  // shown verbatim and copyable, so a valid file can be written by hand or by an AI.
  const IMPORT_SCHEMA = `{
  "version": 1,
  "cards": [
    {
      "title": "The card's text (required)",
      "columnId": "inbox | in-progress | answered",
      "type": "question | problem | task | idea | plan",
      "category": "one of your category ids — work, love, family, health, mind, music, travel, home, money by default  (optional)",
      "importance": "high | low  (optional — for the Matrix)",
      "urgency": "high | low  (optional — for the Matrix)",
      "effort": "low | medium | high  (optional — defaults to medium)",
      "control": "act | influence | none  (optional — defaults to influence)",
      "notes": "Optional free-form notes or the answer",
      "tags": ["optional", "lowercase", "tags"],
      "num": 12,
      "createdAt": 1721606400000,
      "updatedAt": 1721606400000
    }
  ],
  "categories": [
    { "id": "work", "label": "Work", "h": 255 }
  ]
}`;

  const importDialog = $('#import-dialog');
  $('#import-schema').textContent = IMPORT_SCHEMA;

  $('#import-btn').addEventListener('click', () => importDialog.showModal());
  $('#cancel-import').addEventListener('click', () => importDialog.close());
  $('#choose-import-file').addEventListener('click', () => $('#import-input').click());

  $('#copy-schema').addEventListener('click', async () => {
    const btn = $('#copy-schema');
    try {
      await navigator.clipboard.writeText(IMPORT_SCHEMA);
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = 'Copy schema'; }, 1600);
      announce('Schema copied to clipboard');
    } catch (_) {
      // Clipboard unavailable (e.g. non-secure context) — select the text instead
      const range = document.createRange();
      range.selectNodeContents($('#import-schema'));
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      announce('Schema selected — copy it manually');
    }
  });

  // A parsed file waits here while the add-or-substitute dialog is open
  let pendingImport = null;
  const importModeDialog = $('#import-mode-dialog');

  $('#import-input').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    importDialog.close();
    if (!file) return;
    try {
      pendingImport = parseState(await file.text());
      const n = pendingImport.cards.length;
      $('#import-mode-copy').textContent =
        `The file contains ${n} card${n === 1 ? '' : 's'}. Add ${n === 1 ? 'it' : 'them'} to the current board, ` +
        `or substitute the whole board with the file's contents?`;
      importModeDialog.showModal();
    } catch (err) {
      pendingImport = null;
      ask({
        title: 'Could not import this file',
        message: 'It does not match the Lodestar format — open Import JSON to see (and copy) the expected schema.',
        cancelLabel: null,
      });
    }
  });

  $('#cancel-import-mode').addEventListener('click', () => {
    pendingImport = null;
    importModeDialog.close();
  });

  $('#import-add').addEventListener('click', () => {
    if (!pendingImport) return;
    // Categories the file defines but this board doesn't yet: adopt them, so
    // the imported cards keep their labels and colours.
    for (const cat of pendingImport.categories || []) {
      if (!catById(cat.id) && categories.length < CAT_LIMIT) categories.push({ ...cat });
    }
    // Fresh ids and ledger numbers so the same file can be imported twice safely
    const added = pendingImport.cards.map((c) => ({ ...c, id: uid(), num: 0 }));
    state.cards = ensureNums([...state.cards, ...added]);
    pendingImport = null;
    importModeDialog.close();
    dealCards = true;
    commit(`Imported ${added.length} card(s), added to the board`);
    announce(`Added ${added.length} imported card(s) to the board`);
  });

  $('#import-replace').addEventListener('click', async () => {
    if (!pendingImport) return;
    const n = pendingImport.cards.length;
    const sure = await ask({
      title: 'Are you sure?',
      message: `This substitutes the whole board — your current ${state.cards.length} card(s) ` +
        `will be replaced by the ${n} from the file. You can still roll back from History.`,
      okLabel: 'Substitute board',
      danger: true,
    });
    if (!sure || !pendingImport) return;
    if (pendingImport.categories) categories = pendingImport.categories.map((c) => ({ ...c }));
    state = { version: 1, columns: COLUMNS, cards: pendingImport.cards };
    pendingImport = null;
    importModeDialog.close();
    dealCards = true; // deal the imported cards in like a fresh sheet
    commit(`Imported ${n} card(s), substituted the board`);
    announce('Board substituted with the imported cards');
  });

  // --------------------------------------------------------------------------
  // Undo & history dialog
  // --------------------------------------------------------------------------

  $('#undo-btn').addEventListener('click', () => {
    if (timeline.index <= 0) return;
    const undone = timeline.entries[timeline.index].action;
    restoreEntry(timeline.index - 1, `Undid “${undone}”`);
  });

  const historyDialog = $('#history-dialog');

  $('#history-btn').addEventListener('click', () => {
    renderHistory();
    historyDialog.showModal();
  });

  $('#close-history').addEventListener('click', () => historyDialog.close());

  function renderHistory() {
    const list = $('#history-list');
    list.innerHTML = '';
    const fmt = (ts) =>
      new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

    for (let i = timeline.entries.length - 1; i >= 0; i--) {
      const entry = timeline.entries[i];
      const row = document.createElement('div');
      row.className = 'history-row' + (i === timeline.index ? ' current' : '');

      const time = document.createElement('span');
      time.className = 'history-time';
      time.textContent = fmt(entry.ts);

      const main = document.createElement('div');
      main.className = 'history-main';

      const action = document.createElement('p');
      action.className = 'history-action';
      action.textContent = entry.action;

      const meta = document.createElement('span');
      meta.className = 'history-meta';
      meta.textContent = `${entry.cards.length} card${entry.cards.length === 1 ? '' : 's'}`;

      main.append(action, meta);
      row.append(time, main);

      if (i === timeline.index) {
        const mark = document.createElement('span');
        mark.className = 'history-current';
        mark.textContent = 'current';
        row.append(mark);
      } else {
        const btn = document.createElement('button');
        btn.className = 'btn ghost history-restore';
        btn.textContent = 'Restore';
        btn.addEventListener('click', () => {
          restoreEntry(i, `Restored board to “${entry.action}”`);
          renderHistory(); // keep the dialog open, move the “current” mark
        });
        row.append(btn);
      }

      list.append(row);
    }

    refreshTrash(); // populate the "Deleted questions" section from the server
  }

  // Fill the Trash section of the History dialog with the server's soft-deleted
  // questions. Hidden entirely when there's no backend or nothing is trashed.
  async function refreshTrash() {
    const section = $('#trash-section');
    const list = $('#trash-list');
    if (!section || !list) return;
    if (!serverAvailable) { section.hidden = true; return; }

    const trashed = await fetchTrash();
    if (!trashed.length) { section.hidden = true; list.innerHTML = ''; return; }
    section.hidden = false;
    list.innerHTML = '';

    for (const card of trashed) {
      const row = document.createElement('div');
      row.className = 'history-row';

      const label = document.createElement('span');
      label.className = 'history-time';
      label.textContent = qLabel(card);

      const main = document.createElement('div');
      main.className = 'history-main';
      const title = document.createElement('p');
      title.className = 'history-action';
      title.textContent = card.title;
      const meta = document.createElement('span');
      meta.className = 'history-meta';
      meta.textContent = card.tags && card.tags.length ? card.tags.map((t) => '#' + t).join(' ') : 'no tags';
      main.append(title, meta);

      const actions = document.createElement('div');
      actions.className = 'trash-actions';

      const restore = document.createElement('button');
      restore.className = 'btn ghost history-restore';
      restore.textContent = 'Restore';
      restore.addEventListener('click', () => {
        restoreFromTrash(card);
        row.remove(); // optimistic — the server clears deleted_at on the next push
        if (!list.children.length) section.hidden = true;
      });

      const purge = document.createElement('button');
      purge.className = 'btn danger history-restore';
      purge.textContent = 'Delete permanently';
      purge.addEventListener('click', async () => {
        if (await purgeFromTrash(card)) {
          row.remove();
          if (!list.children.length) section.hidden = true;
        }
      });

      actions.append(restore, purge);
      row.append(label, main, actions);
      list.append(row);
    }
  }

  const THEMES = ['light', 'white', 'sepia', 'dark'];
  const themeSelect = $('#theme-select');

  function applyTheme(theme) {
    if (!THEMES.includes(theme)) theme = 'light';
    document.documentElement.dataset.theme = theme;
    themeSelect.value = theme;
  }

  themeSelect.addEventListener('change', () => {
    applyTheme(themeSelect.value);
    try { localStorage.setItem(THEME_KEY, themeSelect.value); } catch (_) { /* private mode */ }
  });

  let savedTheme = null;
  try { savedTheme = localStorage.getItem(THEME_KEY); } catch (_) { /* private mode */ }
  applyTheme(savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

  // --------------------------------------------------------------------------
  // View switch: Board ↔ Backlog
  // --------------------------------------------------------------------------

  const viewButtons = [...document.querySelectorAll('.view-switch button')];

  function syncViewButtons() {
    for (const btn of viewButtons) btn.setAttribute('aria-pressed', String(btn.dataset.view === view));
  }

  function setView(next) {
    if (next === view || !VIEWS.includes(next)) return;
    view = next;
    try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* private mode */ }
    syncViewButtons();
    dealCards = true; // re-deal for a gentle transition between views
    render();
    announce(`${VIEW_LABELS[view]} view`);
  }

  for (const btn of viewButtons) btn.addEventListener('click', () => setView(btn.dataset.view));
  syncViewButtons();

  // --------------------------------------------------------------------------
  // Go
  // --------------------------------------------------------------------------

  render();          // instant paint from localStorage
  initServerSync();  // then reconcile with the SQLite backend if one is running
})();
