(() => {
  'use strict';

  const STORAGE_KEY = 'question-board:v1';
  const THEME_KEY = 'question-board:theme';
  const VIEW_KEY = 'question-board:view';
  const HISTORY_KEY = 'question-board:history';
  const HISTORY_LIMIT = 50; // snapshots kept; oldest fall off like a rotated log

  const COLUMNS = [
    { id: 'inbox', title: 'Inbox' },
    { id: 'to-research', title: 'To Research' },
    { id: 'in-progress', title: 'In Progress' },
    { id: 'answered', title: 'Answered' },
  ];

  const PRIORITIES = ['high', 'medium', 'low'];
  const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };

  // --------------------------------------------------------------------------
  // State & persistence
  // --------------------------------------------------------------------------

  const uid = () =>
    (crypto.randomUUID ? crypto.randomUUID() : 'id-' + Math.random().toString(36).slice(2) + Date.now());

  function seedCards() {
    const now = Date.now();
    const mk = (title, columnId, priority, tags, notes = '') =>
      ({ id: uid(), columnId, title, notes, priority, tags, createdAt: now, updatedAt: now });
    return [
      mk('What should we build next quarter?', 'inbox', 'high', ['planning', 'product']),
      mk('How do we make our weekly reviews shorter?', 'inbox', 'medium', ['process']),
      mk('Which tool should we adopt for shared notes?', 'to-research', 'medium', ['tools', 'planning']),
      mk('How much runway do we have at the current burn rate?', 'to-research', 'low', ['finance']),
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

  function sanitizeCard(raw) {
    if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) return null;
    return {
      id: typeof raw.id === 'string' && raw.id ? raw.id : uid(),
      columnId: COLUMNS.some((c) => c.id === raw.columnId) ? raw.columnId : 'inbox',
      title: raw.title.trim(),
      notes: typeof raw.notes === 'string' ? raw.notes : '',
      priority: PRIORITIES.includes(raw.priority) ? raw.priority : 'medium',
      num: Number.isInteger(raw.num) && raw.num > 0 ? raw.num : 0,
      tags: Array.isArray(raw.tags) ? raw.tags.map((t) => String(t).trim().toLowerCase()).filter(Boolean) : [],
      createdAt: typeof raw.createdAt === 'number' ? raw.createdAt : Date.now(),
      updatedAt: typeof raw.updatedAt === 'number' ? raw.updatedAt : Date.now(),
    };
  }

  function parseState(json) {
    const data = JSON.parse(json);
    if (!data || data.version !== 1 || !Array.isArray(data.cards)) throw new Error('Unrecognized data format');
    return {
      version: 1,
      columns: COLUMNS,
      cards: ensureNums(data.cards.map(sanitizeCard).filter(Boolean)),
    };
  }

  let loadedFromStorage = false; // true when this browser already had a saved board

  function loadState() {
    try {
      const json = localStorage.getItem(STORAGE_KEY);
      if (json) {
        const saved = parseState(json);
        loadedFromStorage = true;
        return saved;
      }
    } catch (err) {
      console.warn('Could not load saved board, starting fresh.', err);
    }
    return { version: 1, columns: COLUMNS, cards: seedCards() };
  }

  let state = loadState();
  const filters = { search: '', priority: '', tags: new Set() };
  let focusCardId = null; // restore focus after re-render (keyboard moves)
  let draggedId = null;
  let dealCards = true; // deal-in animation runs on first render only

  let view = 'board'; // 'board' | 'backlog'
  try {
    const v = localStorage.getItem(VIEW_KEY);
    if (v === 'board' || v === 'backlog') view = v;
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
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
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
    cards.map((c) => [c.id, c.columnId, c.title, c.notes, c.priority, c.num, (c.tags || []).join('|')].join('␟')).join('␞');

  function pushToServer() {
    if (!serverAvailable) return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(async () => {
      try {
        const res = await fetch(API, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: 1, cards: state.cards }),
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

    // A browser that already has its own board wins on load — this guarantees
    // unsynced local edits are never clobbered — and we converge the server to
    // it. A fresh browser (no local board) instead loads from the database.
    if (loadedFromStorage && state.cards.length > 0) {
      pushToServer();
      return;
    }

    if (board.cards.length === 0) {
      if (state.cards.length > 0) pushToServer(); // fresh DB — save our seed board
      return;
    }

    const incoming = ensureNums(board.cards.map(sanitizeCard).filter(Boolean));
    if (boardFingerprint(incoming) === boardFingerprint(state.cards)) return; // already in sync

    // Fresh browser, and the database has a board — adopt it as the source of truth.
    state = { version: 1, columns: COLUMNS, cards: incoming };
    saveState();
    timeline.entries.push({ ts: Date.now(), action: `Loaded ${incoming.length} question(s) from the server`, cards: snapshot(incoming) });
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
  // Helpers
  // --------------------------------------------------------------------------

  const $ = (sel, root = document) => root.querySelector(sel);

  const getCard = (id) => state.cards.find((c) => c.id === id);
  const columnCards = (columnId) => state.cards.filter((c) => c.columnId === columnId);
  const columnIndex = (columnId) => COLUMNS.findIndex((c) => c.id === columnId);
  const columnTitle = (columnId) => COLUMNS[columnIndex(columnId)].title;

  function matchesFilters(card) {
    if (filters.priority && card.priority !== filters.priority) return false;
    if (filters.tags.size && ![...filters.tags].every((t) => card.tags.includes(t))) return false;
    if (filters.search) {
      const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
      if (!haystack.includes(filters.search)) return false;
    }
    return true;
  }

  const filtersActive = () => Boolean(filters.search || filters.priority || filters.tags.size);

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
    board.className = view === 'backlog' ? 'backlog' : 'board';
    board.innerHTML = '';
    if (view === 'backlog') {
      board.appendChild(renderBacklog());
    } else {
      for (const col of COLUMNS) board.appendChild(renderColumn(col));
    }
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
      sortBtn.title = 'Sort this column by priority';
      sortBtn.addEventListener('click', () => sortColumnByPriority(col.id));
      header.append(sortBtn);
    }

    section.append(header);

    if (col.id === 'inbox') section.append(renderQuickAdd());

    const cardsEl = document.createElement('div');
    cardsEl.className = 'cards';
    cardsEl.dataset.col = col.id;

    if (visible.length === 0) {
      const emptyCopy = {
        'inbox': 'Write down your first question above',
        'to-research': 'File questions here when they’re worth digging into',
        'in-progress': 'Drag a question here when you start on it',
        'answered': 'Answered questions land here',
      };
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = filtersActive() ? 'No questions match' : emptyCopy[col.id];
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
    input.placeholder = 'Write down a question…';
    input.setAttribute('aria-label', 'Add a question to the Inbox');

    const btn = document.createElement('button');
    btn.type = 'submit';
    btn.textContent = '+';
    btn.setAttribute('aria-label', 'Add question');

    form.append(input, btn);
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const title = input.value.trim();
      if (!title) return;
      const now = Date.now();
      const card = { id: uid(), columnId: 'inbox', title, notes: '', priority: 'medium', num: nextNum(), tags: [], createdAt: now, updatedAt: now };
      // New captures go to the top of the Inbox
      const firstInbox = state.cards.findIndex((c) => c.columnId === 'inbox');
      state.cards.splice(firstInbox === -1 ? state.cards.length : firstInbox, 0, card);
      commit(`Added ${qLabel(card)} “${short(title)}”`);
      announce(`Added “${title}” to Inbox`);
      const fresh = $('#board .quick-add input');
      if (fresh) fresh.focus();
    });
    return form;
  }

  function renderCard(card) {
    const el = document.createElement('article');
    el.className = 'card';
    el.dataset.id = card.id;
    el.draggable = true;
    el.tabIndex = 0;
    el.setAttribute('aria-label', `${qLabel(card)}: ${card.title} — ${card.priority} priority, in ${columnTitle(card.columnId)}`);

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

    const badge = document.createElement('span');
    badge.className = `badge ${card.priority}`;
    badge.textContent = card.priority;
    top.append(badge);

    const title = document.createElement('p');
    title.className = 'card-title';
    title.textContent = card.title;

    el.append(top, title);

    if (card.tags.length) {
      const tags = document.createElement('div');
      tags.className = 'card-tags';
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
    count.textContent = `${visible.length} ${visible.length === 1 ? 'question' : 'questions'}`;

    head.append(title, count);

    if (visible.length > 1) {
      const sortBtn = document.createElement('button');
      sortBtn.className = 'sort-btn';
      sortBtn.textContent = 'Sort by priority ⇅';
      sortBtn.title = 'Sort the backlog by priority';
      sortBtn.addEventListener('click', () => sortColumnByPriority('inbox'));
      head.append(sortBtn);
    }

    sheet.append(head, renderQuickAdd());

    const list = document.createElement('div');
    list.className = 'backlog-list';

    if (visible.length === 0) {
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = filtersActive() ? 'No questions match' : 'Write down your first question above';
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
    row.setAttribute('aria-label', `${qLabel(card)}: ${card.title} — ${card.priority} priority`);

    const num = document.createElement('span');
    num.className = 'row-num';
    num.textContent = qLabel(card);

    const badge = document.createElement('span');
    badge.className = `badge ${card.priority}`;
    badge.textContent = card.priority;

    const main = document.createElement('div');
    main.className = 'row-main';

    const title = document.createElement('p');
    title.className = 'row-title';
    title.textContent = card.title;
    main.append(title);

    if (card.tags.length) {
      const tags = document.createElement('div');
      tags.className = 'card-tags';
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
      title: 'Delete this question?',
      message: `${qLabel(card)} “${card.title}” will be removed from the board. You can bring it back with Undo or History.`,
      okLabel: 'Delete question',
      danger: true,
    });
    if (!sure) return;
    state.cards = state.cards.filter((c) => c.id !== cardId);
    commit(`Deleted ${qLabel(card)} “${short(card.title)}”`);
    announce(`Deleted “${card.title}”`);
  }

  function sortColumnByPriority(columnId) {
    const sorted = columnCards(columnId).sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority]);
    state.cards = [...state.cards.filter((c) => c.columnId !== columnId), ...sorted];
    commit(`Sorted ${columnTitle(columnId)} by priority`);
    announce(`Sorted ${columnTitle(columnId)} by priority`);
  }

  // --------------------------------------------------------------------------
  // Edit dialog
  // --------------------------------------------------------------------------

  const dialog = $('#card-dialog');
  const form = $('#card-form');
  let editingId = null;

  function openDialog(cardId) {
    const card = getCard(cardId);
    if (!card) return;
    editingId = cardId;
    $('#card-title').value = card.title;
    $('#card-notes').value = card.notes;
    $('#card-tags').value = card.tags.join(', ');
    for (const radio of form.elements.priority) radio.checked = radio.value === card.priority;
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
      card.priority = form.elements.priority.value || card.priority;
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
  // Toolbar: search, priority filter, export/import, theme
  // --------------------------------------------------------------------------

  $('#search').addEventListener('input', (e) => {
    filters.search = e.target.value.trim().toLowerCase();
    render();
  });

  $('#priority-filter').addEventListener('change', (e) => {
    filters.priority = e.target.value;
    render();
  });

  const exportDialog = $('#export-dialog');
  const exportJson = () => JSON.stringify(state, null, 2);

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
    a.download = 'questions.json';
    a.rel = 'noopener';
    document.body.append(a);
    a.click();
    // Revoke/remove on a later tick — doing it synchronously can cancel the
    // download before the browser has started it (notably Firefox/Safari).
    setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 1000);
    exportDialog.close();
    announce('Exported board as questions.json');
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
      "title": "The question, as plain text (required)",
      "columnId": "inbox | to-research | in-progress | answered",
      "priority": "high | medium | low",
      "notes": "Optional free-form notes or the answer",
      "tags": ["optional", "lowercase", "tags"],
      "num": 12,
      "createdAt": 1721606400000,
      "updatedAt": 1721606400000
    }
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
        `The file contains ${n} question${n === 1 ? '' : 's'}. Add ${n === 1 ? 'it' : 'them'} to the current board, ` +
        `or substitute the whole board with the file's contents?`;
      importModeDialog.showModal();
    } catch (err) {
      pendingImport = null;
      ask({
        title: 'Could not import this file',
        message: 'It does not match the Question Board format — open Import JSON to see (and copy) the expected schema.',
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
    // Fresh ids and ledger numbers so the same file can be imported twice safely
    const added = pendingImport.cards.map((c) => ({ ...c, id: uid(), num: 0 }));
    state.cards = ensureNums([...state.cards, ...added]);
    pendingImport = null;
    importModeDialog.close();
    dealCards = true;
    commit(`Imported ${added.length} question(s), added to the board`);
    announce(`Added ${added.length} imported question(s) to the board`);
  });

  $('#import-replace').addEventListener('click', async () => {
    if (!pendingImport) return;
    const n = pendingImport.cards.length;
    const sure = await ask({
      title: 'Are you sure?',
      message: `This substitutes the whole board — your current ${state.cards.length} question(s) ` +
        `will be replaced by the ${n} from the file. You can still roll back from History.`,
      okLabel: 'Substitute board',
      danger: true,
    });
    if (!sure || !pendingImport) return;
    state = pendingImport;
    pendingImport = null;
    importModeDialog.close();
    dealCards = true; // deal the imported cards in like a fresh sheet
    commit(`Imported ${n} question(s), substituted the board`);
    announce('Board substituted with the imported questions');
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
      meta.textContent = `${entry.cards.length} question${entry.cards.length === 1 ? '' : 's'}`;

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
    if (next === view || (next !== 'board' && next !== 'backlog')) return;
    view = next;
    try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* private mode */ }
    syncViewButtons();
    dealCards = true; // re-deal for a gentle transition between views
    render();
    announce(view === 'backlog' ? 'Backlog view' : 'Board view');
  }

  for (const btn of viewButtons) btn.addEventListener('click', () => setView(btn.dataset.view));
  syncViewButtons();

  // --------------------------------------------------------------------------
  // Go
  // --------------------------------------------------------------------------

  render();          // instant paint from localStorage
  initServerSync();  // then reconcile with the SQLite backend if one is running
})();
