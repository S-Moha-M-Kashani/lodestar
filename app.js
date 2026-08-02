(() => {
  'use strict';

  // Local keys were prefixed 'question-board:' until the board's word became
  // "card". Several of them hold data that lives nowhere else — the undo
  // timeline, Review state, the model picks — so the rename copies the old
  // values across once instead of stranding them. Delete migrateStorageKeys and
  // LEGACY_* once no browser in use predates the rename.
  const KEY_PREFIX = 'lodestar:';
  const LEGACY_PREFIX = 'question-board:';
  const LEGACY_SUFFIXES = ['v1', 'theme', 'view', 'history', 'habit-mute',
    'proj', 'matrix', 'reviewed', 'resurface', 'models'];

  function migrateStorageKeys() {
    try {
      for (const suffix of LEGACY_SUFFIXES) {
        const old = localStorage.getItem(LEGACY_PREFIX + suffix);
        // Test for null, not falsiness: '' is a real stored value. And skip any
        // key that already exists, or a boot would undo a change made since.
        if (old !== null && localStorage.getItem(KEY_PREFIX + suffix) === null) {
          localStorage.setItem(KEY_PREFIX + suffix, old);
        }
      }
    } catch (_) { /* private mode */ }
  }
  migrateStorageKeys();

  const STORAGE_KEY = KEY_PREFIX + 'v1';
  const THEME_KEY = KEY_PREFIX + 'theme';
  const VIEW_KEY = KEY_PREFIX + 'view';
  const HISTORY_KEY = KEY_PREFIX + 'history';
  const HISTORY_LIMIT = 50; // snapshots kept; oldest fall off like a rotated log

  const COLUMNS = [
    { id: 'inbox', title: 'Inbox' },
    { id: 'in-progress', title: 'In Progress' },
    // 'answered' is the id every stored card and saved board already carries;
    // only the label changed, because the column finishes tasks and habits too.
    { id: 'answered', title: 'Done' },
  ];

  // What kind of thing a card is — stamped on the card like the old priority
  // stamp, but neutral ink: colour on this board always means category.
  const TYPES = ['question', 'problem', 'task', 'idea', 'plan', 'habit'];
  const TYPE_META = {
    question: { glyph: '?', label: 'question' },
    problem:  { glyph: '!', label: 'problem' },
    task:     { glyph: '✓', label: 'task' },
    idea:     { glyph: '✦', label: 'idea' },
    plan:     { glyph: '→', label: 'plan' },
    habit:    { glyph: '↻', label: 'habit' },
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

  // Importance & urgency are each High, Low, or unset ('') — a card needs
  // both to be placed on the Eisenhower matrix.
  const iuVal = (v) => (v === 'high' || v === 'low' ? v : '');

  // A deadline is an ISO calendar date ('YYYY-MM-DD') or unset (''). The
  // toISOString round-trip rejects shape-valid impossibilities (2026-13-45).
  const deadlineVal = (v) => {
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return '';
    const d = new Date(v + 'T00:00:00Z');
    return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v ? v : '';
  };

  // Automatic priority, derived from the same two judgements the Matrix uses —
  // never stored. 1 urgent+important · 2 urgent only · 3 important only ·
  // 4 neither; 0 (no label) until both judgements are set.
  const priorityOf = (c) => {
    if (!c.importance || !c.urgency) return 0;
    if (c.urgency === 'high') return c.importance === 'high' ? 1 : 2;
    return c.importance === 'high' ? 3 : 4;
  };
  const PRIO_TITLE = ['', 'Urgent & important — answer now', 'Urgent, not important',
    'Important, not urgent', 'Neither urgent nor important'];

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
  // Habits — the one card type that is not finished but repeated.
  //
  // `habitCount` times per `habitFreq` calendar period; `habitTimes` are
  // optional clock slots that decide when the reminder fires, never the target
  // (which is why "2× per year" needs no clock at all). `habitHistory` maps a
  // period id to the instants it was done. None of it is cleared when the card
  // changes type: a mis-stamp must not cost a year of completions.
  // --------------------------------------------------------------------------

  const HABIT_FREQS = ['daily', 'weekly', 'monthly', 'yearly'];
  const HABIT_MAX_COUNT = 99;
  const HABIT_MAX_PERIODS = 400;
  const HABIT_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  const HABIT_PERIOD_RE =
    /^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?|-W(0[1-9]|[1-4]\d|5[0-3]))?$/;
  // "2× per day" reads better than "2× per daily"; "0/2 today" better than
  // "0/2 this day".
  const HABIT_EVERY = { daily: 'day', weekly: 'week', monthly: 'month', yearly: 'year' };
  const HABIT_NOW = { daily: 'today', weekly: 'this week', monthly: 'this month', yearly: 'this year' };

  const habitFreqVal = (v) => (HABIT_FREQS.includes(v) ? v : '');
  const habitCountVal = (v) => {
    const n = Math.trunc(Number(v));
    return Number.isFinite(n) ? Math.min(HABIT_MAX_COUNT, Math.max(1, n)) : 1;
  };
  const habitTimesVal = (v, count) => {
    if (!Array.isArray(v)) return [];
    const seen = new Set();
    for (const t of v) if (typeof t === 'string' && HABIT_TIME_RE.test(t)) seen.add(t);
    return [...seen].sort().slice(0, count);
  };
  function habitHistoryVal(v) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
    const kept = [];
    for (const [period, stamps] of Object.entries(v)) {
      if (!HABIT_PERIOD_RE.test(period) || !Array.isArray(stamps)) continue;
      const clean = stamps.filter((t) => Number.isFinite(t)).sort((a, b) => a - b);
      if (clean.length) kept.push([period, clean]);
    }
    kept.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return Object.fromEntries(kept.slice(-HABIT_MAX_PERIODS));
  }

  const pad2 = (n) => String(n).padStart(2, '0');

  /** ISO week (Monday-based; the Thursday in the week decides the year). */
  function isoWeek(date) {
    const t = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
    t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
    const jan1 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return { year: t.getUTCFullYear(), week: Math.ceil(((t - jan1) / 86400000 + 1) / 7) };
  }

  /** The calendar period a moment falls in — local time, always. */
  function habitPeriod(freq, date = new Date()) {
    const y = date.getFullYear();
    if (freq === 'yearly') return String(y);
    if (freq === 'monthly') return `${y}-${pad2(date.getMonth() + 1)}`;
    if (freq === 'weekly') {
      const { year, week } = isoWeek(date);
      return `${year}-W${pad2(week)}`;
    }
    return `${y}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  /** The last `n` period ids, oldest first, ending with the one we are in. */
  function habitPeriodsBack(freq, n, from = new Date()) {
    // Monthly and yearly steps walk from the 1st, so stepping back from the
    // 31st cannot skip a short month.
    const byDay = freq === 'daily' || freq === 'weekly';
    const d = new Date(from.getFullYear(), from.getMonth(), byDay ? from.getDate() : 1);
    const out = [];
    for (let i = 0; i < n; i++) {
      out.unshift(habitPeriod(freq, d));
      if (freq === 'yearly') d.setFullYear(d.getFullYear() - 1);
      else if (freq === 'monthly') d.setMonth(d.getMonth() - 1);
      else if (freq === 'weekly') d.setDate(d.getDate() - 7);
      else d.setDate(d.getDate() - 1);
    }
    return out;
  }

  const isHabit = (card) => card.type === 'habit' && Boolean(card.habitFreq);
  const habitDoneIn = (card, period) => (card.habitHistory?.[period] || []).length;
  const habitDoneNow = (card) => habitDoneIn(card, habitPeriod(card.habitFreq));
  /** Retired: a habit parked in Done stops counting and stops reminding. */
  const habitRetired = (card) => card.columnId === 'answered';
  const habitDue = (card) =>
    isHabit(card) && !habitRetired(card) && habitDoneNow(card) < card.habitCount;
  const habitCards = () => state.cards.filter(isHabit);

  const cadenceText = (card) => {
    const base = `${card.habitCount}× per ${HABIT_EVERY[card.habitFreq]}`;
    return card.habitTimes.length ? `${base} · ${card.habitTimes.join(', ')}` : base;
  };
  const habitTally = (card) =>
    `${habitDoneNow(card)}/${card.habitCount} ${HABIT_NOW[card.habitFreq]}`;

  /** Record one repetition, now. Returns false when the period is already full. */
  function punchHabit(card) {
    const period = habitPeriod(card.habitFreq);
    const stamps = (card.habitHistory[period] || []).slice();
    if (stamps.length >= card.habitCount) return false;
    stamps.push(Date.now());
    card.habitHistory = { ...card.habitHistory, [period]: stamps };
    card.updatedAt = Date.now();
    return true;
  }

  /** Take the newest repetition back — a mis-tap must be undoable, or the
   *  history stops being a record of what actually happened. */
  function unpunchHabit(card) {
    const period = habitPeriod(card.habitFreq);
    const stamps = (card.habitHistory[period] || []).slice();
    if (!stamps.length) return false;
    stamps.pop();
    const next = { ...card.habitHistory };
    if (stamps.length) next[period] = stamps;
    else delete next[period];
    card.habitHistory = next;
    card.updatedAt = Date.now();
    return true;
  }

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
         deadline: '', habitFreq: '', habitCount: 1, habitTimes: [], habitHistory: {},
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

  // Every card keeps a permanent ledger number (C-001, C-002, …) in capture order.
  function ensureNums(cards) {
    let max = cards.reduce((m, c) => Math.max(m, c.num || 0), 0);
    [...cards]
      .filter((c) => !c.num)
      .sort((a, b) => a.createdAt - b.createdAt)
      .forEach((c) => { c.num = ++max; });
    return cards;
  }

  const cardLabel = (card) => 'C-' + String(card.num).padStart(3, '0');

  function sanitizeCard(raw, reg = categories) {
    if (!raw || typeof raw !== 'object' || typeof raw.title !== 'string' || !raw.title.trim()) return null;
    const habitCount = habitCountVal(raw.habitCount);
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
      deadline: deadlineVal(raw.deadline),
      habitFreq: habitFreqVal(raw.habitFreq),
      habitCount,
      habitTimes: habitTimesVal(raw.habitTimes, habitCount),
      habitHistory: habitHistoryVal(raw.habitHistory),
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
  const filters = { search: '', type: '', category: '', prio: '', tags: new Set() };
  let focusCardId = null; // restore focus after re-render (keyboard moves)
  let draggedId = null;
  let dealCards = true; // deal-in animation runs on first render only

  // 'raglab' is a page, not a tab: it is developer tooling for tuning diary
  // retrieval, reached from a button in the Assistant rather than the view
  // switcher, so the seven life-facing views stay seven. It is still a real
  // view — the switcher simply has no button for it, which syncViewButtons
  // already tolerates.
  const VIEWS = ['board', 'backlog', 'overview', 'matrix', 'areas', 'review', 'assistant', 'raglab'];
  const VIEW_LABELS = { board: 'Board', backlog: 'Backlog', overview: 'Overview', matrix: 'Matrix', areas: 'Areas', review: 'Review', assistant: 'Assistant', raglab: 'RAG lab' };
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
  // whole board is pushed on every change, so deleting a card is the only
  // thing that removes its row server-side.
  // --------------------------------------------------------------------------

  const API = '/api/state';
  let serverAvailable = false;
  let serverOffline = false; // true once a push has failed, to warn only once
  let pushTimer = null;

  // Order-sensitive fingerprint, to skip redundant work when nothing changed.
  const boardFingerprint = (cards) =>
    cards.map((c) => [c.id, c.columnId, c.title, c.notes, c.type, c.category || '', c.importance || '', c.urgency || '',
      c.effort || '', c.control || '', c.effortSrc || '', c.controlSrc || '', c.deadline || '',
      // Habit completions belong here: a board that differs only by a punch is
      // not "already in sync", and skipping the adopt would lose the tick.
      c.habitFreq || '', c.habitCount || 1, (c.habitTimes || []).join('|'),
      JSON.stringify(c.habitHistory || {}),
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
  // Trash — deleting a card from the board only hides it; the server keeps
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
    commit(`Restored ${cardLabel(revived)} “${short(revived.title)}”`); // re-adds the row server-side (clears deleted_at)
    announce(`Restored “${revived.title}”`);
  }

  async function purgeFromTrash(card) {
    const sure = await ask({
      title: 'Delete permanently?',
      message: `${cardLabel(card)} “${card.title}” will be erased from the database for good. This is the only action that truly deletes it, and it cannot be undone.`,
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

  // Once a card is purged, drop it from every local history snapshot too, so
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
    if (filters.prio) {
      const p = priorityOf(card); // 0 = unlabelled (either judgement missing)
      if (filters.prio === 'none' ? p !== 0 : String(p) !== filters.prio) return false;
    }
    if (filters.tags.size && ![...filters.tags].every((t) => card.tags.includes(t))) return false;
    if (filters.search) {
      const haystack = (card.title + ' ' + card.notes + ' ' + card.tags.join(' ')).toLowerCase();
      if (!haystack.includes(filters.search)) return false;
    }
    return true;
  }

  const filtersActive = () => Boolean(filters.search || filters.type || filters.category || filters.prio || filters.tags.size);

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
    commit(`Moved ${cardLabel(card)} to ${columnTitle(columnId)}`);
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
    } else if (view === 'raglab') {
      board.appendChild(renderRagLab());
    } else {
      for (const col of COLUMNS) board.appendChild(renderColumn(col));
      const rail = renderHabitRail();
      if (rail) {
        board.appendChild(rail);
        board.classList.add('has-habit-rail');
      }
    }
    renderCatRail();
    renderTagBar();
    renderHabitBanner();
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
  function typeBadge(card) {
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

  // --------------------------------------------------------------------------
  // Habit UI — the punch strip, the history tape, the rail and the reminder.
  //
  // The strip is the signature object: one box per repetition the period asks
  // for, stamped in the card's own category ink. It is the count, the progress
  // and the control at once, so there is no second widget to keep in step.
  // --------------------------------------------------------------------------

  const PUNCH_MAX_BOXES = 8;  // past this a strip stops being readable at a glance
  const TAPE_PERIODS = { daily: 21, weekly: 12, monthly: 12, yearly: 6 };
  const openTapes = new Set(); // card ids whose history is expanded

  function punchStrip(card) {
    const strip = document.createElement('div');
    strip.className = 'habit-punch';
    const done = habitDoneNow(card);
    const shown = Math.min(card.habitCount, PUNCH_MAX_BOXES);

    for (let i = 0; i < shown; i++) {
      const stamped = i < done;
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'punch-box' + (stamped ? ' done' : '') + (i === done ? ' next' : '');
      box.textContent = stamped ? '✓' : '';
      // Only the newest stamp can be taken back — a stack, so the history keeps
      // matching the order things actually happened in.
      box.disabled = stamped && i < done - 1;
      box.title = stamped
        ? (box.disabled ? 'Recorded' : 'Take this one back')
        : 'Record one now';
      box.setAttribute('aria-label',
        `${card.title}: ${box.title.toLowerCase()} (${done} of ${card.habitCount} ${HABIT_NOW[card.habitFreq]})`);
      box.addEventListener('click', (e) => {
        e.stopPropagation(); // the card itself opens the edit dialog
        const target = getCard(card.id);
        if (!target) return;
        const undo = i < habitDoneNow(target);
        if (undo ? unpunchHabit(target) : punchHabit(target)) {
          commit(`${undo ? 'Took back' : 'Recorded'} “${short(target.title)}”`);
          announce(`${target.title}: ${habitTally(getCard(card.id))}`);
        }
      });
      strip.append(box);
    }

    if (card.habitCount > shown) {
      const more = document.createElement('span');
      more.className = 'punch-more';
      more.textContent = `${done}/${card.habitCount}`;
      strip.append(more);
    }
    return strip;
  }

  /** The history, run sideways: one cell per past period, carrying the number
   *  punched into it, dotted where the period was missed. */
  function habitTape(card) {
    const n = TAPE_PERIODS[card.habitFreq] || 21;
    const periods = habitPeriodsBack(card.habitFreq, n);
    const current = habitPeriod(card.habitFreq);

    const wrap = document.createElement('div');
    wrap.className = 'habit-tape';

    const label = document.createElement('div');
    label.className = 'tape-label';
    label.textContent = `Last ${n} ${HABIT_EVERY[card.habitFreq]}s · oldest first`;

    const row = document.createElement('div');
    row.className = 'tape-row';
    let complete = 0, run = 0, best = 0;
    for (const period of periods) {
      const done = habitDoneIn(card, period);
      const full = done >= card.habitCount;
      if (full) { complete++; best = Math.max(best, ++run); }
      // An unfinished *current* period is not yet a broken run — the day isn't over.
      else if (period !== current) run = 0;

      const cell = document.createElement('span');
      cell.className = 'tape-cell ' + (full ? 'full' : done ? 'part' : 'miss');
      if (period === current) cell.classList.add('today');
      cell.textContent = done ? String(done) : '';
      cell.title = `${period} — ${done} of ${card.habitCount}`;
      row.append(cell);
    }

    const summary = document.createElement('div');
    summary.className = 'tape-summary';
    summary.textContent = `${complete} of ${n} complete · longest run ${best}`;

    wrap.append(label, row, summary);
    return wrap;
  }

  /** Everything a habit adds to its card: the cadence in words, the strip, and
   *  the history behind a button. */
  function habitCardParts(card, el) {
    const cadence = document.createElement('p');
    cadence.className = 'habit-cadence';
    cadence.textContent = habitRetired(card) ? `${cadenceText(card)} · retired` : cadenceText(card);
    el.append(cadence);

    const line = document.createElement('div');
    line.className = 'habit-line';
    if (!habitRetired(card)) line.append(punchStrip(card));

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'habit-history-toggle';
    toggle.textContent = '↻ history';
    toggle.title = 'Show what you have done';
    toggle.setAttribute('aria-expanded', String(openTapes.has(card.id)));
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (openTapes.has(card.id)) openTapes.delete(card.id);
      else openTapes.add(card.id);
      render();
    });
    line.append(toggle);
    el.append(line);

    if (openTapes.has(card.id)) el.append(habitTape(card));
  }

  /** The rail: today's habits beside the board. Absent until there is one — an
   *  empty panel would cost every non-habit user a column of space. */
  function renderHabitRail() {
    const habits = habitCards().filter((c) => !habitRetired(c));
    if (!habits.length) return null;

    const rail = document.createElement('aside');
    rail.className = 'habit-rail';
    rail.setAttribute('aria-label', 'Habits');

    const head = document.createElement('div');
    head.className = 'habit-rail-head';
    const title = document.createElement('h2');
    title.className = 'habit-rail-title';
    title.textContent = 'Habits';
    const sub = document.createElement('p');
    sub.className = 'habit-rail-sub';
    const due = habits.filter(habitDue).length;
    sub.textContent = due ? `${due} due` : 'All done';
    head.append(title, sub);
    rail.append(head);

    // Due first, finished after — a day's ledger, not a nag list.
    for (const card of [...habits].sort((a, b) => Number(habitDue(b)) - Number(habitDue(a)))) {
      const row = document.createElement('div');
      row.className = 'habit-rail-row' + (habitDue(card) ? '' : ' done');
      row.style.setProperty('--cat', catColor(card.category));
      if (card.category) row.classList.add('categorized');

      const top = document.createElement('div');
      top.className = 'habit-rail-name';
      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'habit-rail-open';
      name.textContent = card.title;
      name.title = 'Open this card';
      name.addEventListener('click', () => openDialog(card.id));
      const tally = document.createElement('span');
      tally.className = 'habit-rail-tally';
      tally.textContent = habitTally(card);
      top.append(name, tally);

      row.append(top, punchStrip(card));
      rail.append(row);
    }
    return rail;
  }

  // --- The reminder ---------------------------------------------------------
  // A banner that says what is due, and one short bip. The bip is a bonus:
  // browsers refuse audio before the first gesture, so the banner is the
  // channel that always works.

  const HABIT_MUTE_KEY = KEY_PREFIX + 'habit-mute';
  let habitMuted = localStorage.getItem(HABIT_MUTE_KEY) === '1';
  let habitBannerHidden = false; // dismissed for this session; a reload brings it back
  let audioCtx = null;
  // Keys of things already sounded, so the bip marks a change rather than
  // repeating on every render.
  const sounded = new Set();

  /** How many repetitions the clock has asked for so far. With no slots set the
   *  whole period is fair game, so the full target is expected from its start. */
  function habitExpectedBy(card, at = new Date()) {
    if (!card.habitTimes.length) return card.habitCount;
    const now = `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
    return card.habitTimes.filter((t) => t <= now).length;
  }
  const habitReminding = (card, at = new Date()) =>
    habitDue(card) && habitDoneNow(card) < habitExpectedBy(card, at);

  function bip() {
    if (habitMuted) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
      const t = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.07, t + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.2);
    } catch {
      // No audio device, or no user gesture yet. The banner still shows.
    }
  }

  function renderHabitBanner() {
    const slot = $('#habit-banner-slot');
    if (!slot) return;
    slot.innerHTML = '';
    if (view !== 'board') return;

    const at = new Date();
    const due = habitCards().filter((c) => habitReminding(c, at));
    if (!due.length) return;

    // One key per habit per period per slot reached, so a passing slot time
    // sounds again but a re-render never does.
    for (const c of due) {
      const key = `${c.id}@${habitPeriod(c.habitFreq)}@${habitExpectedBy(c, at)}`;
      if (sounded.has(key)) continue;
      sounded.add(key);
      habitBannerHidden = false; // something new came due — show it again
      bip();
    }
    if (habitBannerHidden) return;

    const banner = document.createElement('div');
    banner.className = 'habit-banner';

    const bell = document.createElement('span');
    bell.className = 'habit-banner-bell';
    bell.textContent = '🔔';

    const text = document.createElement('p');
    text.className = 'habit-banner-text';
    const list = due.map((c) => `${c.title} (${habitTally(c)})`).join(' · ');
    text.textContent = `${due.length} habit${due.length > 1 ? 's' : ''} due — ${list}`;

    const hide = document.createElement('button');
    hide.type = 'button';
    hide.className = 'habit-banner-hide';
    hide.textContent = 'Hide';
    hide.title = 'Hide until the next one comes due';
    hide.addEventListener('click', () => { habitBannerHidden = true; renderHabitBanner(); });

    banner.append(bell, text, hide);
    slot.append(banner);
  }

  function syncHabitMute() {
    const btn = $('#habit-mute');
    if (!btn) return;
    btn.textContent = habitMuted ? '🔇 Habit sound' : '🔊 Habit sound';
    btn.setAttribute('aria-pressed', String(!habitMuted));
    btn.title = habitMuted ? 'Habit reminders are silent' : 'Sound the reminder when a habit is due';
  }

  function cardAria(card) {
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

    if (visible.length > 1) head.append(sortMenu('inbox'));

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
    num.textContent = cardLabel(card);

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
  // Plotted views — shared "stamped card dots" on the engineering grid.
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
    num.textContent = cardLabel(card);
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
  // Each card becomes a vector; PCA projects those vectors to two dimensions
  // (PC-1, PC-2). Real semantic vectors come from a HuggingFace model
  // (Transformers.js) loaded lazily from a CDN; until it's ready — or if it
  // can't load (offline) — a deterministic keyword vector stands in, so the map
  // always renders and never needs the network.

  const EMBED_DIM = 128;

  // What a card *is*, as text: its own words plus the labels it was filed
  // under. The labels are part of the meaning — two cards that read alike but
  // sit in different life areas are not the same thought, and on title+notes
  // alone they landed on the same dot. The labels lead because the sentence is
  // truncated from the tail: a long note must never be able to push a card's
  // category out of its own vector. `catLabel` gives the word the user chose
  // ("Health"), not the id, since that is what an embedding model can read —
  // and renaming a category changes this text, which re-keys the caches below
  // and re-embeds the card, exactly as it should.
  const cardText = (card) => [
    (card.tags || []).join(' '),
    catLabel(card.category),
    card.type,
    card.title,
    card.notes,
  ].filter(Boolean).join(' ').trim();

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
  const PROJ_KEY = KEY_PREFIX + 'proj';
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
      case 'loading': base = 'positioned by keyword overlap — reading the cards…'; break;
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

  // Load the model once, embed any not-yet-embedded cards, then slide the
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

  const MATRIX_KEY = KEY_PREFIX + 'matrix';
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

  // Area names ring the wheel outside the plot, so the viewport has to reserve
  // room for them or a long name gets sliced off at the viewBox edge. Widths come
  // from an off-screen twin that inherits the same `.wheel text` type styles, so
  // the reservation tracks the real font instead of guessing at glyph advances.
  let wheelRuler = null;
  function wheelLabelWidth(text) {
    if (!wheelRuler) {
      const svg = document.createElementNS(SVGNS, 'svg');
      // Deliberately NOT class="wheel": that selector is test-stable API for the
      // one real wheel. `.wheel-ruler text` copies the type styles instead.
      svg.setAttribute('class', 'wheel-ruler');
      svg.setAttribute('aria-hidden', 'true');
      wheelRuler = document.createElementNS(SVGNS, 'text');
      svg.append(wheelRuler);
      document.body.append(svg);
    }
    wheelRuler.textContent = text;
    // getComputedTextLength() needs a rendered node; fall back to a mono estimate.
    return wheelRuler.getComputedTextLength() || text.length * 6;
  }

  // Attention wheel — spoke length is open-card mass (high importance
  // counts double). Purely derived from the board: no scoring ritual to keep up.
  function renderWheel(cats) {
    const SIZE = 260, CX = SIZE / 2, CY = SIZE / 2, R = 88, COLLAR = 18, MARGIN = 2;
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'wheel');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Attention wheel — open cards per life area');

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
    // The plot box is fixed; the label ink pushes these bounds outward instead.
    let minX = 0, maxX = SIZE;
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

      const lx = CX + Math.cos(a) * (R + COLLAR), ly = CY + Math.sin(a) * (R + COLLAR);
      const anchor = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end';
      const label = document.createElementNS(SVGNS, 'text');
      label.setAttribute('x', lx.toFixed(1)); label.setAttribute('y', ly.toFixed(1));
      label.setAttribute('text-anchor', anchor);
      label.setAttribute('dominant-baseline', 'middle');
      label.style.fill = catColor(cat.id);
      label.textContent = `${cat.label} ${stats.open.length}`;
      svg.append(label);

      const w = wheelLabelWidth(label.textContent);
      const left = anchor === 'start' ? lx : anchor === 'end' ? lx - w : lx - w / 2;
      minX = Math.min(minX, left - MARGIN);
      maxX = Math.max(maxX, left + w + MARGIN);
    });

    // Widen the viewport around the untouched plot box, so the ring keeps its
    // size and the names simply get the room they need on either side.
    svg.setAttribute('viewBox', `${minX.toFixed(1)} 0 ${(maxX - minX).toFixed(1)} ${SIZE}`);
    svg.setAttribute('width', Math.ceil(maxX - minX));
    svg.setAttribute('height', SIZE);

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
    num.textContent = cardLabel(card);
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
  // cards captured vs answered over time.
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

  // `draft` holds the composer text across re-renders — render() rebuilds the
  // textarea, so anything typed (or dictated) has to live in state, not the DOM.
  const assistantState = { messages: [], busy: false, draft: '', proposals: [] };

  // The transcript outlives the tab. Only `messages` is stored: `busy` must
  // never come back true — a reload landing mid-stream would restore a disabled
  // composer with nothing running to re-enable it, and the view would look hung
  // with no way out. Same write-behind-try/catch as the model picks: a private
  // window that refuses storage loses its history, never its session.
  const CHAT_KEY = KEY_PREFIX + 'chat';
  const CHAT_KEEP = 200;

  const persistChat = () => {
    try {
      localStorage.setItem(CHAT_KEY,
        JSON.stringify(assistantState.messages.slice(-CHAT_KEEP)));
    } catch { /* private mode or quota — the transcript still holds this session */ }
  };

  /** One stored turn, read back defensively. Anything whose role or content is
   *  not what it claims is dropped rather than rendered. */
  function restoredMessage(msg) {
    if (!msg || typeof msg !== 'object') return null;
    if (msg.role !== 'user' && msg.role !== 'assistant') return null;
    if (typeof msg.content !== 'string') return null;
    const out = { role: msg.role, content: msg.content };
    // `error` and `partial` are load-bearing, not decoration: sendChat filters
    // both out of the history it replays to the model. Persisting the text and
    // dropping the flag would silently undo that filter, and the model would be
    // asked to continue from something it never finished saying.
    if (msg.error) out.error = true;
    if (msg.partial) out.partial = true;
    if (Array.isArray(msg.steps)) out.steps = msg.steps;
    if (Array.isArray(msg.sources)) out.sources = msg.sources;
    if (msg.usage && typeof msg.usage === 'object') out.usage = msg.usage;
    // `running` is never restored. It names tools awaiting an answer from a
    // request that died with the old page, so a restored one is a spinner that
    // can never stop.
    return out;
  }

  try {
    const saved = JSON.parse(localStorage.getItem(CHAT_KEY) || '[]');
    if (Array.isArray(saved)) {
      assistantState.messages = saved.map(restoredMessage).filter(Boolean);
    }
  } catch { /* unreadable transcript — start empty rather than fail the boot */ }

  // Model choices for the brain, one per capability. Only the text pick has
  // an effect today — it rides along on every /api/agent/chat request (the
  // brain forwards it to OpenRouter). The omni pick is a stored preference
  // for the media-ingestion feature to come. Embedding is deliberately NOT a
  // pick: the brain runs exactly one embedder (BRAIN_EMBEDDER's default,
  // heydariAI/persian-embeddings), the old dropdown was a preference nothing
  // ever read, and a dead control teaches the user not to trust the live
  // ones — so the panel states the fixed model instead (renderChatSettings).
  // A saved `embed` key from that dropdown may linger in localStorage; the
  // load sweep below iterates DEFAULT_MODELS keys, so it is ignored, and the
  // next persist drops it.
  const MODELS_KEY = KEY_PREFIX + 'models';
  // Every omni option has to be a model that genuinely receives audio.
  // nemotron-3-nano-omni:free is listed as audio-capable but the provider
  // serving it discards the audio and answers an apology, and it was kept here
  // only for being free. It is gone: OpenRouter lists exactly one free
  // audio-input model and that was it, so "free" never meant "works". Free
  // dictation is the local Parakeet backend's job instead (BRAIN_TRANSCRIBER
  // defaults to it). voxtral-small replaced it, was retired for cost on
  // 2026-08-02 — audio at $100/M tokens against the default's $0.30/M, ~330x,
  // a rate its default-matching text prices hid — and was reinstated the same
  // day by explicit decision, cost accepted. The default stays the cheapest
  // usable audio model in the catalogue.
  const DEFAULT_MODELS = {
    // Local-first is the normal Assistant experience. Nano stays one click
    // away under the explicit OpenRouter provider selector below. The omni
    // pick is the remote route by definition — local dictation is Parakeet's
    // job inside the brain, which ignores this pick entirely.
    text: '4skl/gemma4-e2b-mtp',
    omni: 'google/gemini-2.5-flash-lite',
  };
  // The one embedder the brain actually runs — shown in the panel, never picked.
  const FIXED_EMBEDDER = 'heydariAI/persian-embeddings';
  const MODEL_PICKERS = [
    { key: 'text', id: 'model-text', label: 'Text generation',
      options: [DEFAULT_MODELS.text, 'gemma4:e2b', 'deepseek-r1:8b'] },
    { key: 'omni', id: 'model-omni', label: 'Audio → text (route: OpenRouter API)',
      options: [DEFAULT_MODELS.omni, 'openai/gpt-audio-mini',
                'mistralai/voxtral-small-24b-2507'] },
  ];
  const modelRoute = (slug) =>
    (slug.startsWith('4skl/') || !slug.includes('/')) ? 'local' : 'OpenRouter API';
  // Slugs retired *for cause* — not merely dropped from the preset list above.
  // Dropping a model from MODEL_PICKERS does not deselect it: a saved pick that
  // left the list is re-added as an option and stays selected. That is right for
  // a slug the user chose deliberately and wrong for one removed because it was
  // broken, and the difference is not visible in the options list alone. It cost
  // us a whole release: nemotron came out of the picker while the only browser
  // that had it selected kept dictating through the provider that discards the
  // audio, so the fix reached everyone except the person it was for.
  const RETIRED_MODELS = new Set([
    // Dictation: advertises audio input, but the provider serving it drops the
    // input_audio part and answers an invented apology instead of a transcript.
    'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free',
    // voxtral-small sat here for a few hours on 2026-08-02 (audio at $100/M
    // tokens, ~330x the default) and was reinstated by explicit decision, cost
    // accepted — so it is back in MODEL_PICKERS above, not retired.
    // Text. kimi-k3 stays here permanently: at $3/$15 per M tokens it was the
    // dearest model ever offered in this picker — 60x the default's input price,
    // ~50x per turn for the same work — and it is not in the OpenRouter key's
    // allowlist at all, so it now fails outright rather than merely costing more.
    // It also took ~11s against the default's under 3, and nothing streams, so
    // the wait was a motionless "Thinking…" that read as a hang. No free
    // replacement exists to weigh against it: every ':free' slug 404s under the
    // key's guardrail, so the cheapest reachable tool-calling model is the
    // default itself.
    'moonshotai/kimi-k3',
    'openai/gpt-4o-mini',
    // Deprecated upstream, and it routes per request to a model the brain never
    // reads back out of the response — so a slow turn is unattributable.
    'openrouter/auto',
  ]);
  const assistantModels = { ...DEFAULT_MODELS };
  assistantModels.provider = 'ollama';
  const TEXT_MODELS_BY_PROVIDER = {
    ollama: MODEL_PICKERS[0].options,
    openrouter: ['openai/gpt-5-nano'],
  };
  // What the brain says it can actually serve. Empty until asked, and only a
  // local backend ever answers with a list (see served_models in the brain):
  // OpenRouter is a paid API with hundreds of models, so nothing is probed there
  // and the curated list above stands.
  //
  // This exists because the text pick rides on every chat request. With
  // BRAIN_LLM=ollama the brain forwards that slug to a daemon that cannot load
  // `openai/gpt-5-nano`, so every turn would fail with a picker offering no way
  // out — the RETIRED_MODELS lesson again: a pick that cannot work has to be
  // deselected, not merely delisted.
  const brainModels = { provider: '', verified: false, models: [], default: '' };

  async function probeBrainModels() {
    let answered = false;
    try {
      const res = await fetch('/api/agent/models');
      if (res.ok) {
        Object.assign(brainModels, await res.json());
        answered = true;
      }
    } catch { /* brain down — the presets stand, and chat will say so itself */ }
    // A configured OpenRouter brain is still an explicit remote choice, but it
    // is a useful initial value for a fresh browser profile. Once saved, the
    // person's picker choice wins over a later server configuration change.
    if (!savedTextProvider && (brainModels.provider === 'ollama'
        || brainModels.provider === 'openrouter')) {
      assistantModels.provider = brainModels.provider;
      if (!pickerOptions(MODEL_PICKERS[0]).includes(assistantModels.text)) {
        assistantModels.text = pickerOptions(MODEL_PICKERS[0])[0];
      }
      persistModels();
    }
    if (!answered || !brainModels.verified || !brainModels.models.length) return;
    // The backend named its models, so an unservable text pick is switched to
    // one that works rather than left to fail on the next turn.
    let changed = false;
    if (!brainModels.models.includes(assistantModels.text)) {
      assistantModels.text = brainModels.models.includes(brainModels.default)
        ? brainModels.default : brainModels.models[0];
      persistModels();
      changed = true;
    }
    // Only when the answer changed something. Re-rendering unconditionally would
    // loop: the render triggers the probe that triggers the render.
    if (changed && view === 'assistant') render();
  }

  // The options for one picker: the backend's own list when it verified one,
  // otherwise the presets. Only the text pick is served by the chat model, so
  // the omni picker keeps its curated list either way.
  function pickerOptions(picker) {
    if (picker.key === 'text') {
      if (assistantModels.provider === 'openrouter') return TEXT_MODELS_BY_PROVIDER.openrouter;
      if (brainModels.provider === 'ollama' && brainModels.verified && brainModels.models.length) {
        return brainModels.models;
      }
      return TEXT_MODELS_BY_PROVIDER.ollama;
    }
    return picker.options;
  }
  const persistModels = () => {
    try { localStorage.setItem(MODELS_KEY, JSON.stringify(assistantModels)); }
    catch { /* private mode — the pick still applies to this session */ }
  };
  let savedTextProvider = false;
  try {
    const saved = JSON.parse(localStorage.getItem(MODELS_KEY) || '{}');
    let swept = false;
    for (const k of Object.keys(DEFAULT_MODELS)) {
      if (typeof saved[k] !== 'string' || !saved[k]) continue;
      // Leave the default in place for a retired pick; keep anything else,
      // including an off-list slug that was chosen on purpose.
      if (RETIRED_MODELS.has(saved[k])) { swept = true; continue; }
      assistantModels[k] = saved[k];
    }
    if (saved.provider === 'ollama' || saved.provider === 'openrouter') {
      assistantModels.provider = saved.provider;
      savedTextProvider = true;
    }
    // Write the sweep back rather than re-running it every load: left in storage,
    // a dead slug would return the moment a later version trimmed the list above.
    if (swept) persistModels();
  } catch { /* corrupted or private mode — keep defaults */ }

  function renderChatSettings() {
    const panel = document.createElement('fieldset');
    panel.className = 'chat-settings';
    const legend = document.createElement('legend');
    legend.textContent = 'Models';
    panel.appendChild(legend);
    const providerLabel = document.createElement('label');
    providerLabel.className = 'field';
    providerLabel.append('Text provider');
    const provider = document.createElement('select');
    provider.id = 'model-provider';
    for (const [value, label] of [['ollama', 'Ollama — local, free & private'],
                                  ['openrouter', 'OpenRouter — remote API']]) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = label;
      provider.append(opt);
    }
    provider.value = assistantModels.provider;
    provider.addEventListener('change', () => {
      assistantModels.provider = provider.value;
      const options = pickerOptions(MODEL_PICKERS[0]);
      if (!options.includes(assistantModels.text)) assistantModels.text = options[0];
      savedTextProvider = true;
      persistModels();
      render();
    });
    providerLabel.append(provider);
    panel.appendChild(providerLabel);
    for (const picker of MODEL_PICKERS) {
      const label = document.createElement('label');
      label.className = 'field';
      label.append(picker.label);
      const sel = document.createElement('select');
      sel.id = picker.id;
      // A previously saved slug that left the preset list still deserves to
      // show as selected, so it becomes an extra option instead of vanishing.
      const offered = pickerOptions(picker);
      const opts = offered.includes(assistantModels[picker.key])
        ? offered : [assistantModels[picker.key], ...offered];
      for (const slug of opts) {
        const opt = document.createElement('option');
        opt.value = slug;
        opt.textContent = `${slug} (${modelRoute(slug)})`;
        sel.append(opt);
      }
      sel.value = assistantModels[picker.key];
      sel.addEventListener('change', () => {
        assistantModels[picker.key] = sel.value;
        persistModels();
      });
      label.appendChild(sel);
      panel.appendChild(label);
    }
    // The embedder is a fact, not a pick: the brain runs exactly one model,
    // locally. A filled-in ledger cell where the dropdown used to stand —
    // same footprint as the selects, no affordance — so nobody hunts for a
    // control that shouldn't exist.
    const embedField = document.createElement('div');
    embedField.className = 'field model-fixed';
    embedField.id = 'model-embed-fixed';
    embedField.append('Text → embedding (route: local, fixed)');
    const embedValue = document.createElement('span');
    embedValue.className = 'model-fixed-value';
    const embedName = document.createElement('span');
    embedName.textContent = FIXED_EMBEDDER;
    const embedStamp = document.createElement('span');
    embedStamp.className = 'model-fixed-stamp';
    embedStamp.textContent = 'built-in';
    embedValue.append(embedName, embedStamp);
    const embedNote = document.createElement('span');
    embedNote.className = 'model-fixed-note';
    embedNote.textContent = 'Persian-tuned · embeds your cards inside the brain, never remote';
    embedField.append(embedValue, embedNote);
    panel.appendChild(embedField);
    const hint = document.createElement('p');
    hint.className = 'field-hint';
    hint.textContent = 'Text generation applies to the chat. Ollama uses models pulled on this machine; OpenRouter currently offers GPT-5 Nano and requires an API key. The omni model transcribes your voice, unless the brain is dictating locally with Parakeet — that ignores this pick.';
    panel.appendChild(hint);
    // Where the chat model runs, when the brain told us. Worth saying out loud:
    // a local backend is free and private but answers in tens of seconds, and
    // the list above is then the daemon's, not ours.
    if (brainModels.verified && brainModels.models.length) {
      const where = document.createElement('p');
      where.className = 'field-hint';
      where.textContent = `The configured backend serves local models through ${brainModels.provider} — free and private, and the list is whatever is pulled on this machine.`;
      panel.appendChild(where);
    }
    return panel;
  }

  // --------------------------------------------------------------------------
  // Voice input — speak into the composer instead of typing.
  //
  // MediaRecorder emits webm/opus, which the omni models don't accept, so the
  // blob is decoded and re-encoded here as 16 kHz mono WAV (a third of the
  // bytes of the 48 kHz source) and posted as base64 to the brain. The
  // transcript lands in the composer as editable text and is never auto-sent:
  // a misheard word must be fixable before it reaches the agent.
  // --------------------------------------------------------------------------

  const VOICE_RATE = 16000;      // Hz, mono — plenty for speech
  // 90s ≈ 3.8 MB of base64, comfortably inside the server's ~5 MB body cap.
  const VOICE_MAX_MS = 90_000;

  const voiceState = {
    phase: 'idle',               // 'idle' | 'recording' | 'transcribing'
    error: '',
    startedAt: 0,
    recorder: null,
    stream: null,
    chunks: [],
    timer: null,
  };

  function voiceSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      && window.MediaRecorder && (window.AudioContext || window.webkitAudioContext));
  }

  function formatElapsed(ms) {
    const total = Math.floor(ms / 1000);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }

  function releaseMic() {
    if (voiceState.stream) {
      for (const track of voiceState.stream.getTracks()) track.stop();
      voiceState.stream = null;
    }
  }

  function stopTimer() {
    if (voiceState.timer) clearInterval(voiceState.timer);
    voiceState.timer = null;
  }

  // Resolve once the recorder has flushed its final chunk.
  function flushRecorder() {
    return new Promise((resolve) => {
      const rec = voiceState.recorder;
      if (!rec || rec.state === 'inactive') return resolve();
      rec.addEventListener('stop', () => resolve(), { once: true });
      rec.stop();
    });
  }

  async function startRecording() {
    if (voiceState.phase !== 'idle' || assistantState.busy) return;
    voiceState.error = '';
    try {
      voiceState.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      voiceState.error = 'Microphone blocked — check your browser permissions.';
      render();
      announce('Microphone blocked');
      return;
    }
    voiceState.chunks = [];
    const rec = new MediaRecorder(voiceState.stream);
    rec.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size) voiceState.chunks.push(event.data);
    });
    voiceState.recorder = rec;
    rec.start();
    voiceState.phase = 'recording';
    voiceState.startedAt = Date.now();
    voiceState.timer = setInterval(tickRecording, 250);
    render();
    announce('Recording — press Stop when you are done');
  }

  function tickRecording() {
    const elapsed = Date.now() - voiceState.startedAt;
    const readout = document.querySelector('.chat-elapsed');
    if (readout) readout.textContent = formatElapsed(elapsed);
    // Stop ourselves rather than let the payload grow past what the server takes.
    if (elapsed >= VOICE_MAX_MS) stopRecording();
  }

  async function cancelRecording() {
    if (voiceState.phase !== 'recording') return;
    stopTimer();
    await flushRecorder();
    releaseMic();
    voiceState.chunks = [];
    voiceState.recorder = null;
    voiceState.phase = 'idle';
    voiceState.error = '';
    render();
    announce('Recording discarded');
  }

  async function stopRecording() {
    if (voiceState.phase !== 'recording') return;
    stopTimer();
    // mimeType is only meaningful once recording has started.
    const type = voiceState.recorder.mimeType || 'audio/webm';
    await flushRecorder();
    releaseMic();
    const blob = new Blob(voiceState.chunks, { type });
    voiceState.chunks = [];
    voiceState.recorder = null;
    voiceState.phase = 'transcribing';
    render();

    try {
      const audio = await encodeWav(blob);
      if (!audio) {
        throw explained('Nothing was recorded — check that your microphone is picking up sound.');
      }
      const res = await fetch('/api/agent/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio, format: 'wav', model: assistantModels.omni }),
      });
      if (res.status === 413) {
        throw explained('That recording was too long to transcribe — try a shorter one.');
      }
      if (!res.ok) {
        // The brain names the real cause — an omni model whose provider dropped
        // the audio, a payload it refused. Flattening every failure into "the
        // brain is down" sent the user debugging a service that was running fine.
        // A 503 comes from our own proxy and really does mean unreachable.
        const detail = res.status === 503 ? '' : await failureDetail(res);
        throw explained(detail
          ? `Couldn’t transcribe that — ${detail}`
          : 'Couldn’t transcribe that — check that the brain service is running.');
      }
      const data = await res.json();
      const text = (data.text || '').trim();
      if (!text) {
        voiceState.error = 'Didn’t catch that — nothing was transcribed.';
        announce('Nothing was transcribed');
      } else {
        appendToDraft(text);
        announce('Transcript added to the composer');
      }
    } catch (err) {
      voiceState.error = (err && err.userMessage)
        || 'Couldn’t transcribe that — check that the brain service is running.';
      announce('Transcription failed');
    }

    voiceState.phase = 'idle';
    render();
    const input = document.getElementById('chat-input');
    if (input) {
      input.focus();
      input.selectionStart = input.selectionEnd = input.value.length;
    }
  }

  // A failure we can already put in words, as opposed to an unexpected JS or
  // network error, which falls back to the generic message.
  function explained(message) {
    const err = new Error(message);
    err.userMessage = message;
    return err;
  }

  // FastAPI reports `detail`, our own Node proxy reports `error`.
  async function failureDetail(res) {
    try {
      const body = await res.json();
      const detail = body && (body.detail || body.error);
      return typeof detail === 'string' ? detail : '';
    } catch {
      return '';
    }
  }

  // Dictation adds to the draft rather than replacing it — never lose a thought.
  function appendToDraft(text) {
    const current = assistantState.draft;
    assistantState.draft = current && !/\s$/.test(current)
      ? `${current} ${text}`
      : `${current}${text}`;
  }

  // ---- WAV encoding (no dependencies) --------------------------------------

  async function encodeWav(blob) {
    if (!blob.size) return '';
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    let buffer;
    try {
      buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    } finally {
      ctx.close();
    }
    const samples = resample(downmix(buffer), buffer.sampleRate, VOICE_RATE);
    if (!samples.length) return '';
    return base64FromBytes(wavBytes(samples, VOICE_RATE));
  }

  function downmix(buffer) {
    const channels = buffer.numberOfChannels;
    const mono = new Float32Array(buffer.length);
    for (let c = 0; c < channels; c += 1) {
      const data = buffer.getChannelData(c);
      for (let i = 0; i < mono.length; i += 1) mono[i] += data[i];
    }
    if (channels > 1) for (let i = 0; i < mono.length; i += 1) mono[i] /= channels;
    return mono;
  }

  function resample(input, fromRate, toRate) {
    if (fromRate === toRate) return input;
    const ratio = fromRate / toRate;
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i += 1) {
      const pos = i * ratio;
      const low = Math.floor(pos);
      const high = Math.min(low + 1, input.length - 1);
      out[i] = input[low] + (input[high] - input[low]) * (pos - low);
    }
    return out;
  }

  function wavBytes(samples, rate) {
    const bytes = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(bytes);
    const ascii = (at, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i));
    };
    ascii(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, true);        // fmt chunk size
    view.setUint16(20, 1, true);         // PCM
    view.setUint16(22, 1, true);         // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);  // byte rate
    view.setUint16(32, 2, true);         // block align
    view.setUint16(34, 16, true);        // bits per sample
    ascii(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i += 1) {
      // Clamp: browsers can hand back samples slightly outside [-1, 1].
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Uint8Array(bytes);
  }

  function base64FromBytes(bytes) {
    // Chunked so a long recording can't blow the argument limit.
    const CHUNK = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  // Escape discards the take in progress. Registered once; harmless otherwise.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && voiceState.phase === 'recording') {
      event.preventDefault();
      cancelRecording();
    }
  });

  function renderMicButton() {
    const mic = document.createElement('button');
    mic.type = 'button';
    mic.id = 'chat-mic';
    mic.className = 'btn chat-mic';
    const recording = voiceState.phase === 'recording';
    mic.textContent = recording ? '■' : '\u{1F3A4}';
    mic.setAttribute('aria-pressed', recording ? 'true' : 'false');
    mic.setAttribute('aria-label', recording ? 'Stop recording' : 'Dictate a message');
    mic.title = recording ? 'Stop recording' : 'Dictate a message';
    mic.disabled = assistantState.busy || voiceState.phase === 'transcribing';
    mic.addEventListener('click', () => {
      if (voiceState.phase === 'recording') stopRecording();
      else startRecording();
    });
    return mic;
  }

  function renderRecordingBar() {
    const bar = document.createElement('div');
    bar.className = 'chat-recording';
    bar.setAttribute('role', 'status');

    const dot = document.createElement('span');
    dot.className = 'chat-rec-dot';
    dot.setAttribute('aria-hidden', 'true');

    const label = document.createElement('span');
    label.className = 'chat-rec-label';
    label.textContent = 'Recording…';

    const elapsed = document.createElement('span');
    elapsed.className = 'chat-elapsed';
    elapsed.textContent = formatElapsed(Date.now() - voiceState.startedAt);

    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'btn primary chat-stop';
    stop.textContent = 'Stop';
    stop.addEventListener('click', stopRecording);

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn chat-cancel';
    cancel.textContent = 'Cancel';
    cancel.title = 'Discard this recording (Esc)';
    cancel.addEventListener('click', cancelRecording);

    bar.append(dot, label, elapsed, stop, cancel);
    return bar;
  }

  function renderAssistant() {
    // Asked on entering the view rather than at load, and retried on every entry
    // until it answers — a brain started after the page must be found without a
    // reload, exactly like the RAG lab's own re-probe. Not awaited: the view
    // renders from the presets and re-renders only if the answer changes a pick.
    if (!brainModels.provider) probeBrainModels();
    const sheet = document.createElement('section');
    sheet.className = 'assistant-sheet';

    const head = document.createElement('div');
    head.className = 'assistant-head';
    const heading = document.createElement('h2');
    heading.textContent = 'Assistant';
    head.appendChild(heading);
    const labBtn = document.createElement('button');
    labBtn.type = 'button';
    labBtn.id = 'raglab-open';
    labBtn.className = 'btn ghost';
    labBtn.textContent = 'RAG test lab';
    labBtn.title = 'Tune and grade diary retrieval against the test fixtures';
    labBtn.addEventListener('click', () => setView('raglab'));
    head.appendChild(labBtn);
    // Offered even with an empty transcript: a disabled control that appears
    // only sometimes is harder to find than one that always sits in the same
    // place and exports nothing.
    const exportBtn = document.createElement('button');
    exportBtn.type = 'button';
    exportBtn.id = 'chat-export-btn';
    exportBtn.className = 'btn ghost';
    exportBtn.textContent = 'Export chat';
    exportBtn.title = 'Save this conversation as JSON or Markdown';
    exportBtn.addEventListener('click', () => openExportDialog('chat'));
    head.appendChild(exportBtn);
    // Import — the missing half of export: a saved JSON transcript goes into
    // the durable chat record (databases/assistant.db) through the Node API.
    const importBtn = document.createElement('button');
    importBtn.type = 'button';
    importBtn.id = 'chat-import-btn';
    importBtn.className = 'btn ghost';
    importBtn.textContent = 'Import chat';
    importBtn.title = 'Read a chat JSON export into the durable chat record';
    const importFile = document.createElement('input');
    importFile.type = 'file';
    importFile.id = 'chat-import-file';
    importFile.accept = 'application/json,.json';
    importFile.hidden = true;
    importFile.addEventListener('change', () => {
      const file = importFile.files && importFile.files[0];
      if (file) importChatFile(file);
      importFile.value = '';   // same file again must re-fire the change event
    });
    importBtn.addEventListener('click', () => importFile.click());
    head.appendChild(importBtn);
    head.appendChild(importFile);
    sheet.appendChild(head);

    sheet.appendChild(renderChatSettings());
    sheet.appendChild(renderRecallPanel());

    // Nothing proposed, nothing shown — the section must not sit there empty.
    if (assistantState.proposals.length) sheet.appendChild(renderProposals());

    const log = document.createElement('div');
    log.className = 'chat-log';
    if (!assistantState.messages.length) {
      const hint = document.createElement('p');
      hint.className = 'chat-status';
      hint.textContent = 'Ask about your board — research a question, triage the inbox, or find connections.';
      log.appendChild(hint);
    }
    for (const msg of assistantState.messages) log.appendChild(renderChatMessage(msg));
    sheet.appendChild(log);

    const status = document.createElement('div');
    status.className = 'chat-status';
    if (assistantState.busy) status.textContent = busyLabel();
    else if (voiceState.phase === 'transcribing') status.textContent = 'Transcribing…';
    else if (voiceState.phase === 'recording') status.textContent = 'Listening…';
    sheet.appendChild(status);

    if (voiceState.error) {
      const problem = document.createElement('p');
      problem.className = 'chat-voice-error';
      problem.setAttribute('role', 'alert');
      problem.textContent = voiceState.error;
      sheet.appendChild(problem);
    }

    if (voiceState.phase === 'recording') sheet.appendChild(renderRecordingBar());

    const form = document.createElement('form');
    form.className = 'chat-composer';
    const input = document.createElement('textarea');
    input.id = 'chat-input';
    input.placeholder = 'Message the assistant…';
    input.disabled = assistantState.busy;
    input.value = assistantState.draft;
    input.addEventListener('input', () => { assistantState.draft = input.value; });
    const actions = document.createElement('div');
    actions.className = 'composer-actions';
    if (voiceSupported()) actions.appendChild(renderMicButton());
    const send = document.createElement('button');
    send.type = 'submit';
    send.id = 'chat-send';
    send.className = 'btn primary';
    send.textContent = 'Send';
    send.disabled = assistantState.busy;
    actions.appendChild(send);
    form.appendChild(input);
    form.appendChild(actions);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text && !assistantState.busy) {
        assistantState.draft = '';
        sendChat(text);
      }
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

  // Chat memory has been searchable by HTTP since the brain gained a Chroma
  // store, and reachable from the UI only by asking the agent and hoping it
  // chose the tool. `matches: null` is "not asked yet", which is not the same
  // as "asked and found nothing".
  const recallState = { open: false, query: '', matches: null, memory: true,
                        busy: false, failed: false, focused: false };

  function renderRecallPanel() {
    const box = document.createElement('details');
    box.className = 'chat-recall';
    box.open = recallState.open;
    box.addEventListener('toggle', () => { recallState.open = box.open; });
    const name = document.createElement('summary');
    name.className = 'chat-recall-name';
    name.textContent = 'Search past conversations';
    box.appendChild(name);

    const form = document.createElement('form');
    form.className = 'chat-recall-form';
    const input = document.createElement('input');
    input.id = 'recall-input';
    input.type = 'search';
    input.placeholder = 'What did we say about…';
    input.value = recallState.query;
    input.addEventListener('input', () => { recallState.query = input.value; });
    input.addEventListener('focus', () => { recallState.focused = true; });
    input.addEventListener('blur', () => { recallState.focused = false; });
    const go = document.createElement('button');
    go.type = 'submit';
    go.id = 'recall-search';
    go.className = 'btn';
    go.textContent = 'Search';
    go.disabled = recallState.busy;
    form.append(input, go);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      recallChat(input.value.trim());
    });
    box.appendChild(form);
    box.appendChild(renderRecallResults());

    // render() rebuilds the whole sheet, and a streaming reply repaints it many
    // times a second — so without this, typing here while a reply arrives loses
    // the caret on the next frame.
    if (recallState.focused) {
      requestAnimationFrame(() => {
        const live = document.getElementById('recall-input');
        if (!live || document.activeElement === live) return;
        live.focus();
        live.setSelectionRange(live.value.length, live.value.length);
      });
    }
    return box;
  }

  function renderRecallResults() {
    const out = document.createElement('div');
    out.className = 'chat-recall-results';
    if (recallState.busy) { out.textContent = 'Searching…'; return out; }
    if (recallState.failed) {
      out.textContent = 'Could not reach the assistant to search.';
      return out;
    }
    if (recallState.matches === null) {
      out.textContent = 'Search what you and the assistant have said before.';
      return out;
    }
    if (!recallState.memory) {
      // Deliberately not "no matches": this is the service being switched off,
      // not the history being empty, and the two send you to different places.
      out.textContent = 'Chat memory is off, so nothing has been recorded. '
        + 'Start the Chroma container to keep conversations.';
      return out;
    }
    if (!recallState.matches.length) {
      out.textContent = 'Nothing recorded about that yet.';
      return out;
    }
    const list = document.createElement('ol');
    list.className = 'recall-hits';
    for (const hit of recallState.matches) {
      const item = document.createElement('li');
      item.className = 'recall-hit';
      const said = document.createElement('p');
      said.className = 'recall-hit-text';
      said.textContent = hit.text;
      const meta = document.createElement('p');
      meta.className = 'recall-hit-meta';
      meta.textContent = `${(hit.metadata && hit.metadata.role) || 'unknown'} · ${hit.score}`;
      item.append(said, meta);
      list.appendChild(item);
    }
    out.appendChild(list);
    return out;
  }

  async function recallChat(text) {
    if (!text) return;
    recallState.busy = true;
    recallState.failed = false;
    render();
    try {
      const res = await fetch('/api/rag/recall', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, k: 5 }),
      });
      if (!res.ok) throw new Error(`recall ${res.status}`);
      const data = await res.json();
      recallState.matches = data.matches || [];
      // Only an explicit false means off. A brain too old to send the field
      // cannot be reported as having memory switched off — that would be a
      // claim about the service made from its silence.
      recallState.memory = data.memory !== false;
    } catch {
      recallState.failed = true;
      recallState.matches = null;
    }
    recallState.busy = false;
    render();
  }

  function renderChatMessage(msg) {
    const el = document.createElement('div');
    el.className = `chat-msg ${msg.role}${msg.error ? ' error' : ''}`;
    // The text is its own node now: the steps below are elements, and setting
    // textContent on the parent would wipe them.
    const body = document.createElement('div');
    body.className = 'chat-text';
    appendLinked(body, msg.content);
    el.appendChild(body);

    const done = msg.steps || [];
    const running = msg.running || [];
    if (done.length || running.length) {
      const steps = document.createElement('div');
      steps.className = 'chat-steps';
      for (const step of done) steps.appendChild(renderChatStep(step, false));
      for (const call of running) steps.appendChild(renderChatStep(call, true));
      el.appendChild(steps);
    }
    const sources = sourcesOf(done);
    if (sources.length) el.appendChild(renderChatSources(sources));
    if (msg.usage) el.appendChild(renderChatUsage(msg.usage));
    return el;
  }

  // What the turn spent. Tokens only, deliberately no money figure: a price per
  // model is a number this app cannot verify, and one that has quietly gone
  // stale is worse than none at all. Absent, not zero, when the model reported
  // nothing — see _usage_from in the brain.
  function renderChatUsage(usage) {
    const line = document.createElement('p');
    line.className = 'chat-usage';
    const n = (v) => Number(v || 0).toLocaleString();
    line.textContent = `${n(usage.total_tokens)} tokens · ${n(usage.input_tokens)} in`
      + ` · ${n(usage.output_tokens)} out`;
    return line;
  }

  // Deliberately anchored on the scheme, so nothing but http(s) can become an
  // href — a linkifier that accepted any "word:" would happily build a
  // javascript: link out of a web-search snippet. The last character may not be
  // punctuation, or a url ending a sentence swallows the full stop.
  const URL_RE = /\bhttps?:\/\/[^\s<>()[\]{}"']*[^\s<>()[\]{}"'.,;:!?]/g;

  function appendLinked(parent, text) {
    let at = 0;
    for (const match of String(text || '').matchAll(URL_RE)) {
      if (match.index > at) {
        parent.appendChild(document.createTextNode(text.slice(at, match.index)));
      }
      const link = document.createElement('a');
      link.className = 'chat-link';
      link.href = match[0];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = match[0];
      parent.appendChild(link);
      at = match.index + match[0].length;
    }
    parent.appendChild(document.createTextNode(String(text || '').slice(at)));
  }

  // Where an answer came from. Each tool answers in its own shape, so the
  // reader lives next to the tool it understands rather than one function
  // guessing from the payload — a misread shape would cite the wrong thing,
  // which is worse than citing nothing.
  const SOURCE_READERS = {
    web_search: (rows) => rows.map((row) => ({
      label: row.title || row.url, url: row.url, note: row.snippet })),
    find_related: (rows) => rows.map((row) => ({
      label: (row.card && row.card.title) || '', cardId: row.card && row.card.id,
      note: row.card && row.card.columnId })),
    // No link: a recalled snippet is the transcript itself, and there is
    // nowhere to send the user that shows more of it than this does.
    recall_chat: (rows) => rows.map((row) => ({ label: row.text, note: '' })),
  };

  function sourcesOf(steps) {
    const found = [];
    const seen = new Set();
    for (const step of steps) {
      const read = SOURCE_READERS[step.tool];
      if (!read || !Array.isArray(step.result)) continue;
      for (const source of read(step.result)) {
        // Two searches often surface the same page; listing it twice would
        // read as two independent sources agreeing.
        const key = source.url || source.cardId || source.label;
        if (!source.label || seen.has(key)) continue;
        seen.add(key);
        found.push(source);
      }
    }
    return found;
  }

  function renderChatSources(sources) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-sources';
    const heading = document.createElement('p');
    heading.className = 'chat-sources-label';
    heading.textContent = sources.length === 1 ? '1 source' : `${sources.length} sources`;
    wrap.appendChild(heading);
    const list = document.createElement('ol');
    list.className = 'chat-source-list';
    for (const source of sources) list.appendChild(renderChatSource(source));
    wrap.appendChild(list);
    return wrap;
  }

  function renderChatSource(source) {
    const item = document.createElement('li');
    item.className = 'chat-source';
    if (source.url) {
      const link = document.createElement('a');
      link.className = 'chat-source-link';
      link.href = source.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = source.label;
      item.appendChild(link);
    } else if (source.cardId) {
      // A button, not a link: it opens the card's editor in place rather than
      // navigating, so the user does not lose the conversation to read it.
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'chat-source-link chat-source-card';
      open.textContent = source.label;
      open.addEventListener('click', () => openDialog(source.cardId));
      item.appendChild(open);
    } else {
      const said = document.createElement('span');
      said.className = 'chat-source-said';
      said.textContent = source.label;
      item.appendChild(said);
    }
    if (source.note) {
      const note = document.createElement('span');
      note.className = 'chat-source-note';
      note.textContent = source.note;
      item.appendChild(note);
    }
    return item;
  }

  // A tool call, collapsed to its name and openable for the evidence. Still
  // `.chat-step` — the class is test-stable API, so this adds to it rather than
  // renaming it.
  function renderChatStep(step, running) {
    const box = document.createElement('details');
    box.className = `chat-step${running ? ' chat-step-running' : ''}`;
    const name = document.createElement('summary');
    name.className = 'chat-step-name';
    name.textContent = running ? `${step.tool}…` : step.tool;
    box.appendChild(name);
    box.appendChild(chatStepField('arguments', step.arguments));
    // A running call has no result yet, and an empty "result" row would read as
    // a tool that answered with nothing.
    if (!running) box.appendChild(chatStepField('result', step.result));
    return box;
  }

  function chatStepField(label, value) {
    const row = document.createElement('div');
    row.className = 'chat-step-field';
    const key = document.createElement('span');
    key.className = 'chat-step-label';
    key.textContent = label;
    const val = document.createElement('pre');
    val.className = 'chat-step-value';
    val.textContent = typeof value === 'string' ? value
      : value === undefined ? '—' : JSON.stringify(value, null, 2);
    row.appendChild(key);
    row.appendChild(val);
    return row;
  }

  // What the assistant is doing right now, from the last event that arrived.
  // A label that names the running tool is the difference between waiting and
  // wondering whether it has hung.
  function busyLabel() {
    const last = assistantState.messages[assistantState.messages.length - 1];
    if (!last || last.role !== 'assistant') return 'Thinking…';
    const running = last.running || [];
    if (running.length) return `Running ${running[running.length - 1].tool}…`;
    return last.content ? 'Writing…' : 'Thinking…';
  }

  // Server-sent events over fetch, because EventSource is GET-only and the chat
  // turn is a POST. A frame can be split across reads, so nothing is parsed
  // until its blank-line terminator is in the buffer.
  async function* sseFrames(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let name = 'message';
        let data = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event: ')) name = line.slice(7);
          else if (line.startsWith('data: ')) data += line.slice(6);
        }
        if (data) yield { name, data: JSON.parse(data) };
      }
    }
  }

  // One repaint per frame at most. A token-per-render would rebuild the whole
  // view hundreds of times for one reply; coalescing costs nothing and keeps a
  // single rendering path rather than a second one that can drift from render().
  let chatPaint = 0;
  function paintChatSoon() {
    if (chatPaint) return;
    chatPaint = requestAnimationFrame(() => { chatPaint = 0; render(); });
  }

  // What to tell the user when a turn never started. Kept apart from the generic
  // message because a 429 is the board's own rate limit, not a dead brain: told
  // to check that the service is running, the user would go restart something
  // that works and never learn that waiting is the whole fix.
  const CHAT_UNAVAILABLE =
    'The assistant is unavailable right now. Check that the brain service is running.';
  const CHAT_REFUSALS = {
    429: 'Too many assistant requests in a row — wait a moment, then try again.',
    413: 'This conversation is too long for one turn — start a new chat.',
  };

  async function sendChat(text) {
    assistantState.messages.push({ role: 'user', content: text });
    // Stored before the request, not after it: a question that costs a reload
    // to lose is the thing this project promises not to do.
    persistChat();
    assistantState.busy = true;
    render();
    // The turn being streamed into. `running` holds tools the model has asked
    // for but that have not answered yet; `partial` marks a turn the stream
    // abandoned, so it is shown but never replayed to the model as history.
    const turn = { role: 'assistant', content: '', steps: [], running: [] };
    let failure = CHAT_UNAVAILABLE;
    try {
      const history = assistantState.messages
        .filter((m) => !m.error && !m.partial
          && (m.role === 'user' || m.role === 'assistant'))
        .map(({ role, content }) => ({ role, content }));
      const res = await fetch('/api/agent/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: history,
          model: assistantModels.text,
          // Always sent, including against a brain configured as 'fake'. The
          // offline contract is the server's to keep (make_chat_model checks
          // 'fake' before it reads this), and a client that decides when to omit
          // the field is a client deciding when the guard applies.
          provider: assistantModels.provider,
        }),
      });
      if (!res.ok || !res.body) {
        failure = CHAT_REFUSALS[res.status] || CHAT_UNAVAILABLE;
        throw new Error(`agent ${res.status}`);
      }
      assistantState.messages.push(turn);
      let data = null;
      let failed = '';
      for await (const { name, data: payload } of sseFrames(res)) {
        if (name === 'calling') turn.running.push(payload);
        // Steps answer in request order, so the oldest running call is this
        // one. See _steps_from in the brain for why that holds.
        else if (name === 'step') { turn.steps.push(payload); turn.running.shift(); }
        else if (name === 'token') turn.content += payload.text;
        else if (name === 'error') failed = payload.message;
        else if (name === 'done') data = payload;
        paintChatSoon();
      }
      if (failed) throw new Error(failed);
      if (!data) throw new Error('the stream ended without a result');
      // Tokens were provisional: text can arrive on a message that also
      // requested tools, and the step-limit path abandons the transcript
      // entirely. `done` is the record of the turn — see the brain's astream.
      turn.content = data.reply || '';
      turn.steps = data.steps || [];
      turn.running = [];
      // Only from `done`: the tokens of a turn that died mid-stream were spent,
      // but nothing on the wire says how many, and a partial count shown as the
      // total would be wrong in the direction that flatters us.
      turn.usage = data.usage || null;
      // Two distinct outcomes: an edit changed the board, a proposal did not.
      if (data.mutated) await adoptServerBoard();
      if (data.proposed) await refreshProposals();
      announce(data.proposed ? 'Assistant proposed a card for your approval'
        : 'Assistant replied');
    } catch {
      // Whatever arrived before the failure is kept — a long answer that dies
      // at the last frame should not vanish — but it is marked `partial` so a
      // truncated reply is never sent back as if the assistant had finished it.
      const arrived = assistantState.messages.indexOf(turn) !== -1;
      if (arrived && (turn.content || turn.steps.length)) {
        turn.running = [];
        turn.partial = true;
      } else if (arrived) {
        assistantState.messages.splice(assistantState.messages.indexOf(turn), 1);
      }
      assistantState.messages.push({ role: 'assistant', content: failure, error: true });
      announce(failure);
    }
    assistantState.busy = false;
    // The turn has settled — whether it answered, failed, or died partway. The
    // stream mutates `turn` in place, so this is the one point where what is
    // written is what the user will see on the next load.
    persistChat();
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
        // ensureNums matters here: a card the server created (an agent edit, or a
        // just-confirmed proposal) arrives with num 0, and without this it would
        // render as C-000 until the next reload.
        state.cards = ensureNums(data.cards);
        saveState();
      }
    } catch { /* offline — keep the local board */ }
  }

  // --------------------------------------------------------------------------
  // Proposals — cards the Assistant suggested, waiting for the user's yes.
  // They are stored server-side but stay off the board until confirmed, so they
  // live outside `state` and are fetched on their own.
  // --------------------------------------------------------------------------

  const PROPOSALS_API = '/api/proposals';

  async function refreshProposals() {
    try {
      const res = await fetch(PROPOSALS_API, { headers: { Accept: 'application/json' } });
      if (!res.ok) return;
      const data = await res.json();
      if (data && Array.isArray(data.cards)) {
        assistantState.proposals = data.cards;
        syncProposalBadge();
        render();
      }
    } catch { /* offline — leave the list as it was */ }
  }

  async function actOnProposal(id, action) {
    try {
      const res = await fetch(`${PROPOSALS_API}/${encodeURIComponent(id)}/${action}`,
        { method: 'POST' });
      if (!res.ok) throw new Error(`proposal ${res.status}`);
      // Approving adds a card to the board, so adopt server state before the
      // debounced local push can overwrite it with a board that lacks the card.
      if (action === 'confirm') await adoptServerBoard();
      await refreshProposals();
      announce(action === 'confirm' ? 'Proposal approved and added to the board'
        : 'Proposal rejected — it is recoverable from the Trash');
    } catch {
      announce('Could not reach the server — the proposal is unchanged');
    }
  }

  function renderProposals() {
    const wrap = document.createElement('section');
    wrap.className = 'proposals';
    const heading = document.createElement('h3');
    heading.className = 'proposals-heading';
    const n = assistantState.proposals.length;
    heading.textContent = `Proposed — ${n} card${n === 1 ? '' : 's'} awaiting your approval`;
    wrap.appendChild(heading);

    for (const card of assistantState.proposals) {
      const row = document.createElement('article');
      row.className = 'proposal';
      row.dataset.id = card.id;

      const title = document.createElement('p');
      title.className = 'proposal-title';
      title.textContent = card.title;
      row.appendChild(title);

      const meta = document.createElement('p');
      meta.className = 'proposal-meta';
      const cat = card.category ? ` · ${catLabel(card.category)}` : '';
      meta.textContent = `${TYPE_META[card.type].label}${cat} · would land in ${columnTitle(card.columnId)}`;
      row.appendChild(meta);

      if (card.notes) {
        const notes = document.createElement('p');
        notes.className = 'proposal-notes';
        notes.textContent = card.notes;
        row.appendChild(notes);
      }

      const actions = document.createElement('div');
      actions.className = 'proposal-actions';
      const approve = document.createElement('button');
      approve.type = 'button';
      approve.className = 'btn primary proposal-approve';
      approve.textContent = 'Approve';
      approve.addEventListener('click', () => actOnProposal(card.id, 'confirm'));
      const reject = document.createElement('button');
      reject.type = 'button';
      reject.className = 'btn ghost proposal-reject';
      reject.textContent = 'Reject';
      reject.title = 'Sends it to the Trash, where it stays recoverable';
      reject.addEventListener('click', () => actOnProposal(card.id, 'reject'));
      actions.appendChild(reject);
      actions.appendChild(approve);
      row.appendChild(actions);

      wrap.appendChild(row);
    }
    return wrap;
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
      message: `${cardLabel(card)} “${card.title}” will be moved off the board. It stays recoverable — bring it back with Undo, or from the History panel — until you delete it permanently there.`,
      okLabel: 'Delete card',
      danger: true,
    });
    if (!sure) return;
    state.cards = state.cards.filter((c) => c.id !== cardId);
    commit(`Deleted ${cardLabel(card)} “${short(card.title)}”`);
    announce(`Deleted “${card.title}”`);
  }

  // The sort menu's orders. Deadline: earliest first, undated at the back
  // (ISO dates compare correctly as strings; '' would sort first, so undated
  // cards get a sentinel past every real date). Priority: P1 → P4, unlabelled
  // last. Array.sort is stable, so ties keep their existing order.
  const SORTERS = {
    deadline: { label: 'By deadline', cmp: (a, b) => (a.deadline || '9999-12-31').localeCompare(b.deadline || '9999-12-31') },
    priority: { label: 'By priority', cmp: (a, b) => (priorityOf(a) || 5) - (priorityOf(b) || 5) },
    type:     { label: 'By type',     cmp: (a, b) => TYPE_RANK[a.type] - TYPE_RANK[b.type] },
    newest:   { label: 'Newest first', cmp: (a, b) => b.createdAt - a.createdAt },
    oldest:   { label: 'Oldest first', cmp: (a, b) => a.createdAt - b.createdAt },
  };

  function sortColumn(columnId, key) {
    const sorter = SORTERS[key];
    if (!sorter) return;
    const sorted = columnCards(columnId).sort(sorter.cmp);
    state.cards = [...state.cards.filter((c) => c.columnId !== columnId), ...sorted];
    commit(`Sorted ${columnTitle(columnId)} ${sorter.label.toLowerCase()}`);
    announce(`Sorted ${columnTitle(columnId)} ${sorter.label.toLowerCase()}`);
  }

  // A command-select: picking an order applies it once and the control snaps
  // back to its placeholder — it reads as a menu of actions, not a setting.
  function sortMenu(columnId) {
    const sel = document.createElement('select');
    sel.className = 'sort-select';
    sel.setAttribute('aria-label', `Sort ${columnTitle(columnId)}`);
    sel.title = 'Sort these cards';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Sort ⇅';
    sel.append(placeholder);
    for (const [key, sorter] of Object.entries(SORTERS)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = sorter.label;
      sel.append(opt);
    }
    sel.addEventListener('change', () => {
      const key = sel.value;
      sel.value = '';
      sortColumn(columnId, key);
    });
    return sel;
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
    // The cadence fields only make sense for a habit, so they appear with the
    // stamp rather than sitting empty on every other card.
    typeWrap.addEventListener('change', syncHabitFields);
  })();

  function syncHabitFields() {
    $('#card-habit').hidden = form.elements.type.value !== 'habit';
  }

  // Forgiving on the way in, strict on the way out: "7:30" is what people type.
  const padTime = (t) => (/^\d:\d\d$/.test(t) ? '0' + t : t);
  const readHabitTimes = (count) => habitTimesVal(
    $('#card-habit-times').value.split(',').map((t) => padTime(t.trim())).filter(Boolean), count);

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
    $('#card-deadline').value = deadlineVal(card.deadline);
    $('#card-effort').value = effortVal(card.effort);
    $('#card-control').value = controlVal(card.control);
    for (const radio of form.elements.type) radio.checked = radio.value === card.type;
    for (const radio of form.elements.category) radio.checked = radio.value === (card.category || '');
    // A card being stamped Habit for the first time starts at once a day.
    $('#card-habit-freq').value = card.habitFreq || 'daily';
    $('#card-habit-count').value = String(card.habitCount || 1);
    $('#card-habit-times').value = card.habitTimes.join(', ');
    syncHabitFields();
    const fmt = (ts) => new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    $('#card-meta').textContent =
      `${cardLabel(card)} · in ${columnTitle(card.columnId)} · added ${fmt(card.createdAt)} · updated ${fmt(card.updatedAt)}`;
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
      // Written only for a habit, so editing an ordinary card can never touch
      // a cadence — or a history — it was not asked about.
      if (card.type === 'habit') {
        card.habitFreq = habitFreqVal($('#card-habit-freq').value) || 'daily';
        card.habitCount = habitCountVal($('#card-habit-count').value);
        card.habitTimes = readHabitTimes(card.habitCount);
      }
      card.category = catVal(form.elements.category.value);
      card.importance = iuVal($('#card-importance').value);
      card.urgency = iuVal($('#card-urgency').value);
      card.deadline = deadlineVal($('#card-deadline').value);
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
      commit(`Edited ${cardLabel(card)} “${short(card.title)}”`);
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

  $('#prio-filter').addEventListener('change', (e) => {
    filters.prio = e.target.value;
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

  // Which subject the shared dialog is currently showing. Everything that
  // differs — the title, the blurb, the format switch, the filename, what Copy
  // puts on the clipboard — reads this, so the two exports cannot half-swap.
  let exportMode = 'board';

  const ROLE_LABEL = { user: 'You', assistant: 'Assistant' };

  const chatExportJson = () => JSON.stringify({
    exported: new Date().toISOString(),
    messages: assistantState.messages.map((m) => ({
      role: m.role,
      content: m.content,
      // Carried so an exported transcript cannot read as a clean conversation
      // when part of it failed. Absent, not false, on an ordinary turn.
      ...(m.error ? { error: true } : {}),
      ...(m.partial ? { partial: true } : {}),
    })),
  }, null, 2);

  const chatExportMarkdown = () => {
    const lines = ['# Lodestar assistant transcript', '',
                   `*Exported ${new Date().toLocaleString()}*`, ''];
    for (const m of assistantState.messages) {
      let heading = `## ${ROLE_LABEL[m.role] || m.role}`;
      if (m.error) heading += ' — failed';
      else if (m.partial) heading += ' — incomplete';
      lines.push(heading, '', m.content || '*(no text)*', '');
    }
    return lines.join('\n');
  };

  const chatExportText = () =>
    ($('#chat-export-format').value === 'markdown'
      ? chatExportMarkdown() : chatExportJson());

  /** What the dialog is currently offering, whichever subject it is showing. */
  const currentExportText = () =>
    (exportMode === 'chat' ? chatExportText() : exportJson());

  const currentExportName = () =>
    (exportMode !== 'chat' ? 'lodestar.json'
      : $('#chat-export-format').value === 'markdown'
        ? 'lodestar-chat.md' : 'lodestar-chat.json');

  // The copy button names what it will actually put on the clipboard. Left as a
  // fixed "Copy JSON" it would offer Markdown under a JSON label — a small lie,
  // but the kind the user only discovers after pasting.
  const copyLabel = () =>
    (exportMode === 'chat' && $('#chat-export-format').value === 'markdown'
      ? 'Copy Markdown' : 'Copy JSON');

  function openExportDialog(mode) {
    exportMode = mode;
    const chat = mode === 'chat';
    $('#chat-export-format-row').hidden = !chat;
    $('#export-title').textContent = chat ? 'Export chat' : 'Export board';
    $('#export-copy').textContent = chat
      ? 'Save the Assistant transcript. Markdown is for reading; JSON keeps the turn structure, including which turns failed. If your browser blocks the download, copy the text below instead.'
      : 'Save the whole board as lodestar.json. If your browser blocks the download (some embedded viewers do), copy the JSON below and paste it into a file instead.';
    $('#download-export').textContent = `Download ${currentExportName()}`;
    $('#copy-export').textContent = copyLabel();
    $('#export-json').value = currentExportText();
    exportDialog.showModal();
  }

  /** Import a chat JSON export into the durable record. Errored and partial
   *  turns are skipped exactly as they are withheld from the model (and from
   *  `remember`): the record must not carry text the assistant never
   *  successfully said. Import appends — it never rewrites what is there. */
  async function importChatFile(file) {
    let messages = null;
    try {
      const parsed = JSON.parse(await file.text());
      if (Array.isArray(parsed?.messages)) messages = parsed.messages;
    } catch { /* not JSON — announced below */ }
    if (!messages) {
      announce('That file is not a chat export — expected the JSON the export dialog saves');
      return;
    }
    const clean = messages
      .filter((m) => m && !m.error && !m.partial
        && (m.role === 'user' || m.role === 'assistant')
        && typeof m.content === 'string' && m.content.trim())
      .map((m) => ({ role: m.role, content: m.content }));
    const skipped = messages.length - clean.length;
    if (!clean.length) {
      announce('Nothing to import: that file has no completed turns');
      return;
    }
    const go = await ask({
      title: 'Import chat',
      message: `Import ${clean.length} messages into the chat record?`
        + (skipped ? ` ${skipped} failed, partial or empty turns will be skipped.` : ''),
      okLabel: 'Import',
    });
    if (!go) return;
    try {
      const res = await fetch('/api/chat/messages', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: clean }),
      });
      if (!res.ok) throw new Error(`the server refused the import (${res.status})`);
    } catch (err) {
      announce(`Import failed — ${err.message}`);
      return;
    }
    // The record is the truth; the recall index catches up here — or at the
    // brain's next start, which runs the same sync, if it is off or away.
    let indexed = ' (recall index catches up at the next brain start)';
    try {
      const rag = await (await fetch('/api/rag/chat/reindex', { method: 'POST' })).json();
      if (rag.memory) indexed = ` (${rag.indexed} indexed for recall)`;
    } catch { /* the note above already says what happens */ }
    announce(`Imported ${clean.length} messages into the chat record${indexed}`);
  }

  $('#export-btn').addEventListener('click', () => openExportDialog('board'));
  $('#cancel-export').addEventListener('click', () => exportDialog.close());

  // Switching format re-renders the same transcript; it never re-reads the
  // board, so the two subjects cannot bleed into one another.
  $('#chat-export-format').addEventListener('change', () => {
    $('#export-json').value = chatExportText();
    $('#download-export').textContent = `Download ${currentExportName()}`;
    $('#copy-export').textContent = copyLabel();
  });

  $('#download-export').addEventListener('click', () => {
    const name = currentExportName();
    const blob = new Blob([currentExportText()], {
      type: name.endsWith('.md') ? 'text/markdown' : 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.rel = 'noopener';
    document.body.append(a);
    a.click();
    // Revoke/remove on a later tick — doing it synchronously can cancel the
    // download before the browser has started it (notably Firefox/Safari).
    setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 1000);
    exportDialog.close();
    announce(`Exported ${exportMode === 'chat' ? 'chat' : 'board'} as ${name}`);
  });

  $('#copy-export').addEventListener('click', async () => {
    const btn = $('#copy-export');
    const done = () => {
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = copyLabel(); }, 1600);
      announce(exportMode === 'chat'
        ? 'Chat transcript copied to clipboard'
        : 'Board JSON copied to clipboard');
    };
    try {
      await navigator.clipboard.writeText(currentExportText());
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
        setTimeout(() => { btn.textContent = copyLabel(); }, 2500);
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
      "type": "question | problem | task | idea | plan | habit",
      "category": "one of your category ids — work, love, family, health, mind, music, travel, home, money by default  (optional)",
      "importance": "high | low  (optional — for the Matrix)",
      "urgency": "high | low  (optional — for the Matrix)",
      "effort": "low | medium | high  (optional — defaults to medium)",
      "control": "act | influence | none  (optional — defaults to influence)",
      "deadline": "YYYY-MM-DD  (optional)",
      "habitFreq": "daily | weekly | monthly | yearly  (habits only)",
      "habitCount": "how many times per period, 1-99  (habits only, defaults to 1)",
      "habitTimes": ["HH:MM reminder slots (habits only, optional)"],
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

  $('#habit-mute').addEventListener('click', () => {
    habitMuted = !habitMuted;
    localStorage.setItem(HABIT_MUTE_KEY, habitMuted ? '1' : '0');
    syncHabitMute();
    announce(habitMuted ? 'Habit reminders are silent' : 'Habit reminders will sound');
  });
  syncHabitMute();

  // A slot time passing is the other moment a habit comes due, so the reminder
  // is re-checked while the board is open, not only when it is opened.
  setInterval(renderHabitBanner, 30_000);

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

    refreshTrash(); // populate the "Deleted cards" section from the server
  }

  // Fill the Trash section of the History dialog with the server's soft-deleted
  // cards. Hidden entirely when there's no backend or nothing is trashed.
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
      label.textContent = cardLabel(card);

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
    syncProposalBadge();
  }

  // A count on the Assistant tab, so a proposal made while the user is on the
  // Board is still noticed. Absent entirely when nothing is pending.
  function syncProposalBadge() {
    const btn = viewButtons.find((b) => b.dataset.view === 'assistant');
    if (!btn) return;
    const n = assistantState.proposals.length;
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
    btn.setAttribute('aria-description', `${n} proposal${n === 1 ? '' : 's'} awaiting approval`);
  }

  // --------------------------------------------------------------------------
  // RAG lab page — tuning diary retrieval against the synthetic test fixtures.
  //
  // Developer tooling, not a life view: it talks to brain/tests/raglab through
  // the Node proxy (/api/raglab/*), so the browser still speaks to one origin.
  // The lab is usually not running, and that is a normal state, not an error —
  // the page says how to start it instead of showing a broken panel.
  // --------------------------------------------------------------------------
  const RAGLAB_CFG_KEY = 'lodestar-raglab-config';

  const ragState = {
    phase: 'idle',        // idle | loading | ready | absent
    options: null,
    problem: '',
    cfg: null,            // whatever the panel last had; server defaults fill it
    run: { ragas_mode: 'offline', limit: 0, types: [] },
    job: null,            // { stage, progress, kind }
    jobId: null,          // server job currently running or stopping
    result: null,
    runs: [],
    questions: [],        // ground truth without its answers, for the picker
    indexInfo: null,      // stats from the last build
    question: '',
    queryOut: null,
    queryProblem: '',
    busy: false,
  };

  // One row per knob. Kept declarative because the lab's whole point is that
  // these are swappable: a new strategy in the brain shows up as another option
  // from /api/raglab/options without touching this file.
  const RAG_FIELDS = [
    { group: 'index', key: 'chunker', label: 'Chunking', kind: 'select', from: 'chunkers' },
    { group: 'index', key: 'chunk_chars', label: 'Chunk chars', kind: 'number', min: 120, max: 2000, step: 20 },
    { group: 'index', key: 'overlap', label: 'Overlap', kind: 'number', min: 0, max: 800, step: 20 },
    // An embedder is a language model, so it lives with the other models in the
    // right-hand column rather than among the chunking knobs — still wearing the
    // index ink, because that is the step it decides.
    { group: 'index', key: 'embedder', label: 'Embedder', kind: 'embedder', panel: 'models' },
    { group: 'index', key: 'embed_model', label: 'Embedding model', kind: 'embed-model', when: 'Embedder = fastembed or sentence-transformers', panel: 'models' },
    { group: 'index', key: 'contextual', label: 'Contextual chunk headers', kind: 'check' },
    { group: 'retrieval', key: 'retriever', label: 'Retriever', kind: 'select', from: 'retrievers' },
    { group: 'retrieval', key: 'k', label: 'Contexts (k)', kind: 'number', min: 1, max: 40, step: 1 },
    { group: 'retrieval', key: 'candidates', label: 'Candidates', kind: 'number', min: 5, max: 200, step: 5 },
    { group: 'retrieval', key: 'reranker', label: 'Reranker', kind: 'select', from: 'rerankers' },
    { group: 'retrieval', key: 'rerank_depth', label: 'Rerank depth', kind: 'number', min: 5, max: 100, step: 5 },
    { group: 'retrieval', key: 'mmr_lambda', label: 'MMR λ (1 = off)', kind: 'number', min: 0.1, max: 1, step: 0.05 },
    { group: 'retrieval', key: 'grader', label: 'Relevance gate', kind: 'select', from: 'graders' },
    { group: 'retrieval', key: 'grade_threshold', label: 'Gate threshold', kind: 'number', min: 0, max: 1, step: 0.05 },
    { group: 'retrieval', key: 'time_filter', label: 'Farsi time-scope filter', kind: 'check' },
    { group: 'retrieval', key: 'multi_query', label: 'Multi-query expansion', kind: 'check' },
    { group: 'retrieval', key: 'hyde', label: 'HyDE (needs a model)', kind: 'check' },
    { group: 'generation', key: 'answerer', label: 'Answerer', kind: 'select', from: 'answerers' },
    { group: 'generation', key: 'key_facts_judge', label: 'LLM key-facts judge', kind: 'check' },
  ];

  // Every number the results screen prints is defined by the lab
  // (metrics.MEASURES and ragas_eval.RAGAS_MEASURES): its label, the step it
  // grades, the exact arithmetic and the library that ran it. Deliberately not a
  // list in this file any more — a label here could drift from the definition
  // there, and then the page would be explaining a different metric than it shows.
  function ragMeasures() {
    return (ragState.options && ragState.options.metrics) || [];
  }

  function ragMeasure(key) {
    return ragMeasures().find((measure) => measure.key === key)
      || { key, label: key, short: '', step: '' };
  }

  const ragNum = (v, digits = 3) =>
    v === null || v === undefined ? '—' : Number(v).toFixed(digits);

  // Where a model's weights stand is part of its name here: an open-weight model
  // can be run locally later, which is the direction the brain's LLMProvider seam
  // exists for. A model the lab has never run stays in the list as NA — dropping
  // it would hide the option instead of qualifying it.
  const RAG_LICENCE = {
    open: '(open source)', closed: '(closed source)',
    unknown: '(licence not recorded)', default: '',
  };

  function ragModelLabel(model) {
    const bits = [model.label, RAG_LICENCE[model.source] || ''];
    if (!model.available) bits.push('— NA, not verified here but worth checking');
    return bits.filter(Boolean).join(' ');
  }

  // Which languages an embedder can represent is the first fact about it, not a
  // footnote: on a Farsi diary an English-only model returns a full set of
  // confident numbers that measure nothing. So the coverage is in the option
  // text itself, where it cannot be missed while picking.
  function ragEmbedderLabel(hint) {
    const bits = [hint.kind, '—', hint.languages];
    // A backend can be unusable for two ordinary reasons — a missing extra, a
    // missing key — and both are worth saying before a run rather than during it.
    if (hint.available === false) bits.push('— NA, not installed or no key here');
    return bits.filter(Boolean).join(' ');
  }

  function ragEmbedModelLabel(model) {
    const bits = [model.label];
    // Which one to reach for is the question being asked while the dropdown is
    // open, so the standing goes here rather than into the explainer.
    if (model.tag) bits.push(`(${model.tag})`);
    bits.push('—', model.languages, RAG_LICENCE[model.source] || '');
    if (model.dim) bits.push(`· ${model.dim}d`);
    // Which backend serves it decides what picking it costs: an ONNX download, a
    // multi-gigabyte checkpoint, or an API bill.
    if (model.backend) bits.push(`· via ${model.backend}`);
    if (!model.available) bits.push('— NA, not served here but worth checking');
    return bits.filter(Boolean).join(' ');
  }

  // The three steps of the pipeline, in order, as the lab describes them
  // (config.STEPS). Each one is a colour on the page — the ink itself lives in
  // styles.css, keyed on data-step, so it can follow the theme like every other
  // colour here. The fallback only matters against a lab too old to serve them.
  function ragSteps() {
    const served = (ragState.options && ragState.options.steps) || [];
    if (served.length) return served;
    return [
      { key: 'index', short: 'Index', label: 'Index — what gets stored', note: '' },
      { key: 'retrieval', short: 'Retrieval', label: 'Retrieval & ranking', note: '' },
      { key: 'generation', short: 'Generation', label: 'Generation & scoring', note: '' },
    ];
  }

  // Every knob explains itself, from text the lab serves (config.HELP,
  // config.STEPS and models.ROLES) rather than a copy kept here. Closed until
  // asked, because twenty-eight paragraphs of prose is not a settings panel.
  function ragWhy(topic, host, text) {
    const help = text || ((ragState.options && ragState.options.help) || {})[topic];
    if (!help) return null;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'rag-why';
    btn.dataset.topic = topic;
    btn.textContent = '!';
    btn.setAttribute('aria-label', 'What is this?');
    btn.addEventListener('click', (event) => {
      // Inside a <label>, a plain click would also activate the labelled control
      // — the explainer for a checkbox would toggle the checkbox.
      event.preventDefault();
      event.stopPropagation();
      const open = host.nextElementSibling;
      if (open && open.classList.contains('rag-help')) { open.remove(); return; }
      const note = document.createElement('p');
      note.className = 'rag-help';
      note.textContent = help;
      host.insertAdjacentElement('afterend', note);
    });
    return btn;
  }

  async function ragApi(path, body) {
    const res = await fetch('/api/raglab' + path, body ? {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    } : undefined);
    const data = await res.json().catch(() => ({ error: res.statusText }));
    if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
    return data;
  }

  function ragConfig() {
    const defaults = ragState.options ? ragState.options.defaults : null;
    if (!defaults) return null;
    if (!ragState.cfg) {
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(RAGLAB_CFG_KEY) || 'null'); } catch (_) { saved = null; }
      // Server defaults are the base, so a knob added since the last visit
      // appears with its intended value rather than as undefined.
      ragState.cfg = {
        index: { ...defaults.index, ...(saved && saved.index) },
        retrieval: { ...defaults.retrieval, ...(saved && saved.retrieval) },
        generation: { ...defaults.generation, ...(saved && saved.generation) },
        label: (saved && saved.label) || '',
      };
    }
    return ragState.cfg;
  }

  function ragPersist() {
    try { localStorage.setItem(RAGLAB_CFG_KEY, JSON.stringify(ragState.cfg)); } catch (_) { /* private mode */ }
  }

  /** Persist, and repaint when this field decides whether another one is live.
   *  Only the owners repaint: a number input that owns nothing would otherwise
   *  rebuild the panel on every commit and take the caret with it. */
  function ragCommit(path) {
    ragPersist();
    const rules = (ragState.options && ragState.options.dependencies) || {};
    for (const key of Object.keys(rules)) {
      if (rules[key].field === path) { render(); return; }
    }
  }

  async function ragLoad() {
    ragState.phase = 'loading';
    render();
    try {
      ragState.options = await ragApi('/options');
      ragConfig();
      ragState.phase = 'ready';
      ragState.problem = '';
      // Both are conveniences: a lab with no run history and no question picker
      // is still fully usable, so neither failure blocks the page.
      try { ragState.runs = (await ragApi('/evaluations?limit=30')).runs; } catch (_) { ragState.runs = []; }
      try { ragState.questions = (await ragApi('/questions?limit=200')).questions; } catch (_) { ragState.questions = []; }
    } catch (error) {
      ragState.phase = 'absent';
      ragState.problem = error.message;
    }
    if (view === 'raglab') render();
  }

  // Runs outlive any sane HTTP timeout (a fastembed index plus 100 questions),
  // so the lab hands back a job id and the page polls it.
  async function ragPoll(jobId, onDone) {
    try {
      const job = await ragApi('/jobs/' + jobId);
      ragState.job = job;
      if (job.state === 'running' || job.state === 'cancelling') {
        if (view === 'raglab') render();
        setTimeout(() => ragPoll(jobId, onDone), 900);
        return;
      }
      ragState.busy = false;
      ragState.job = null;
      ragState.jobId = null;
      if (job.state === 'error') ragState.problem = job.error;
      else if (job.state === 'cancelled') {
        ragState.problem = 'Experiment stopped; no further model calls were started.';
      } else onDone(job.result);
    } catch (error) {
      ragState.busy = false;
      ragState.job = null;
      ragState.jobId = null;
      ragState.problem = error.message;
    }
    if (view === 'raglab') render();
  }

  // The lab's job kinds, and the collection each one is created in. Kept as a
  // map rather than '/' + kind: the two names stopped matching when the routes
  // became resource collections, and a concatenated path would have gone on
  // looking correct while 404ing.
  const RAG_COLLECTIONS = { index: '/indexes', run: '/evaluations' };

  async function ragStart(kind, extra) {
    if (ragState.busy) return;
    ragState.busy = true;
    ragState.problem = '';
    render();
    try {
      const { job_id: jobId } = await ragApi(RAG_COLLECTIONS[kind],
                                             { ...ragConfig(), ...extra });
      ragState.jobId = jobId;
      render();
      ragPoll(jobId, async (result) => {
        if (kind === 'run') {
          ragState.result = result;
          try { ragState.runs = (await ragApi('/evaluations?limit=30')).runs; } catch (_) { /* leaderboard is optional */ }
        } else {
          ragState.indexInfo = result;
        }
      });
    } catch (error) {
      ragState.busy = false;
      ragState.problem = error.message;
      render();
    }
  }

  async function ragCancel() {
    if (!ragState.jobId) return;
    ragState.problem = '';
    try {
      ragState.job = { ...(ragState.job || {}), kind: 'run', stage: 'stopping',
        progress: (ragState.job && ragState.job.progress) || 0,
        detail: 'stopping before the next model call' };
      render();
      await ragApi('/jobs/' + ragState.jobId + '/cancel', {});
    } catch (error) {
      ragState.problem = error.message;
      if (view === 'raglab') render();
    }
  }

  async function ragAsk() {
    const question = ragState.question.trim();
    if (!question || ragState.busy) return;
    ragState.busy = true;
    ragState.queryProblem = '';
    render();
    try {
      ragState.queryOut = await ragApi('/queries', { ...ragConfig(), question });
    } catch (error) {
      ragState.queryOut = null;
      ragState.queryProblem = error.message;
    }
    ragState.busy = false;
    render();
  }

  /** Is this control live under the current config, and if not, why not?
   *  The rules come from the lab (`/api/options` → dependencies), so the two
   *  panels and the pipeline agree by construction rather than by review. */
  function ragDependency(path, cfg) {
    const rule = (ragState.options && ragState.options.dependencies || {})[path];
    if (!rule) return null;
    const [group, name] = rule.field.split('.');
    const current = (cfg[group] || {})[name];
    const enabled = rule.on_true ? Boolean(current)
      : (rule.on || []).indexOf(current) !== -1;
    return { enabled, reason: rule.reason };
  }

  function ragFieldControl(field, cfg) {
    const options = ragState.options;
    const bag = cfg[field.group];
    if (field.kind === 'check') {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = Boolean(bag[field.key]);
      box.addEventListener('change', () => {
        bag[field.key] = box.checked;
        ragCommit(`${field.group}.${field.key}`);
      });
      return box;
    }
    if (field.kind === 'number') {
      const input = document.createElement('input');
      input.type = 'number';
      input.min = field.min;
      input.max = field.max;
      input.step = field.step;
      input.value = bag[field.key];
      input.addEventListener('change', () => {
        bag[field.key] = Number(input.value);
        ragCommit(`${field.group}.${field.key}`);
      });
      return input;
    }
    if (field.kind === 'embedder' || field.kind === 'embed-model') {
      // Both lists come from the lab (embedding.EMBEDDER_HINTS and
      // embedding.EMBED_MODELS), so a model added there shows up here untouched.
      const embedder = field.kind === 'embedder';
      const sel = document.createElement('select');
      sel.className = embedder ? 'rag-embedder' : 'rag-embed-model';
      const list = embedder
        ? (options.embedder_hints || (options.embedders || []).map((k) => ({ kind: k, languages: 'coverage not reported' })))
        : (options.embed_models || []);
      for (const entry of list) {
        const opt = document.createElement('option');
        opt.value = embedder ? entry.kind : entry.id;
        opt.textContent = embedder ? ragEmbedderLabel(entry) : ragEmbedModelLabel(entry);
        sel.appendChild(opt);
      }
      sel.value = bag[field.key] || '';
      // A saved model the lab no longer offers cannot be shown, so the config
      // follows the panel rather than the panel quietly lying about it.
      if (sel.value !== (bag[field.key] || '')) {
        bag[field.key] = sel.value;
        ragPersist();
      }
      sel.addEventListener('change', () => {
        bag[field.key] = sel.value;
        ragCommit(`${field.group}.${field.key}`);
      });
      return sel;
    }
    const sel = document.createElement('select');
    for (const value of options[field.from]) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = value;
      sel.appendChild(opt);
    }
    sel.value = bag[field.key];
    sel.addEventListener('change', () => {
      bag[field.key] = sel.value;
      ragCommit(`${field.group}.${field.key}`);
    });
    return sel;
  }

  // One row: the label, when the knob is consulted, its explainer, and the
  // control. Shared by the step panels and the model column, so a field renders
  // identically wherever it is placed.
  function ragFieldRow(field, cfg) {
    const label = document.createElement('label');
    label.className = field.kind === 'check' ? 'field rag-inline' : 'field';
    const control = ragFieldControl(field, cfg);
    const why = ragWhy(`${field.group}.${field.key}`, label);
    // Disabling the control is what stops the value being tuned; the class is
    // what stops the label reading as live text beside a dead input.
    const gate = ragDependency(`${field.group}.${field.key}`, cfg);
    if (gate && !gate.enabled) {
      control.disabled = true;
      label.classList.add('rag-field-off');
      label.title = `Disabled because ${gate.reason}`;
    }
    if (field.kind === 'check') {
      label.appendChild(control);
      label.append(' ' + field.label);
      if (why) label.appendChild(why);
      return label;
    }
    label.append(field.label);
    // A knob the current pipeline would ignore is turned off rather than merely
    // annotated. The old `when:` text was accurate and inert: it said "Embedder
    // = fastembed, sentence-transformers or openai" beside a control you could
    // still edit, so a value set under a hash embedder looked applied and was
    // silently dropped. The rule is served (options.dependencies), so this panel
    // and the standalone one cannot grey out different things.
    const dep = ragDependency(`${field.group}.${field.key}`, cfg);
    if (dep && !dep.enabled) {
      const when = document.createElement('span');
      when.className = 'rag-when';
      when.textContent = dep.reason;
      label.appendChild(when);
    }
    if (why) label.appendChild(why);
    label.appendChild(control);
    return label;
  }

  // One panel per step, wearing that step's ink. Models are not here: they all
  // live in the column on the right (see ragModelPanel).
  function ragFieldset(step, cfg) {
    const box = document.createElement('fieldset');
    box.className = 'rag-panel';
    box.dataset.step = step.key;
    const legend = document.createElement('legend');
    legend.textContent = step.label;
    box.appendChild(legend);
    const why = ragWhy('step.' + step.key, legend, step.note);
    if (why) legend.appendChild(why);
    for (const field of RAG_FIELDS.filter(
      (f) => f.group === step.key && f.panel !== 'models')) {
      box.appendChild(ragFieldRow(field, cfg));
    }
    return box;
  }

  function ragModelRow(role, cfg) {
    const options = ragState.options;
    const [group, key] = role.field.split('.');
    const label = document.createElement('label');
    label.className = 'field';
    label.append(role.label);
    // Same rule as the step knobs: a model picker for a stage that calls no
    // model is turned off, not merely captioned. `only_when` said "HyDE is on"
    // beside a live dropdown, so a model chosen with HyDE off was recorded in
    // the config and never used — and a run's label is the one thing a lab
    // must not get wrong.
    const gate = ragDependency(role.field, cfg);
    const when = document.createElement('span');
    when.className = 'rag-when';
    when.textContent = (gate && !gate.enabled) ? gate.reason : role.only_when;
    label.appendChild(when);
    const why = ragWhy('model.' + role.key, label);
    if (why) label.appendChild(why);
    const select = document.createElement('select');
    select.className = 'rag-model';
    select.dataset.role = role.key;
    for (const model of options.models || []) {
      const opt = document.createElement('option');
      opt.value = model.id;
      opt.textContent = ragModelLabel(model);
      select.appendChild(opt);
    }
    select.value = (cfg[group] && cfg[group][key]) || '';
    // A saved model the lab no longer offers cannot be shown, so the config
    // follows the panel rather than the panel quietly lying about it.
    if (cfg[group] && select.value !== (cfg[group][key] || '')) {
      cfg[group][key] = select.value;
      ragPersist();
    }
    select.addEventListener('change', () => {
      cfg[group][key] = select.value;
      ragCommit(role.field);
    });
    if (gate && !gate.enabled) {
      select.disabled = true;
      label.classList.add('rag-field-off');
      label.title = `Disabled because ${gate.reason}`;
    }
    label.appendChild(select);
    return label;
  }

  // Every model the lab can call, in one column: the seven LLM stages and the
  // embedder, which is a language model too and was the odd one out among the
  // chunking knobs. Grouped and tagged by the step each one serves, because a
  // model choice that has left its step panel must still say which stage it
  // changes — the ink is that answer. Roles come from the lab
  // (brain/tests/raglab/models.py), so a new stage appears here untouched.
  function ragModelPanel(cfg) {
    const options = ragState.options;
    const box = document.createElement('fieldset');
    box.className = 'rag-panel rag-models';
    const legend = document.createElement('legend');
    legend.textContent = 'Models — one per task';
    box.appendChild(legend);
    for (const step of ragSteps()) {
      const roles = (options.model_roles || []).filter(
        (role) => (role.step || role.field.split('.')[0]) === step.key);
      const fields = RAG_FIELDS.filter(
        (f) => f.panel === 'models' && f.group === step.key);
      if (!roles.length && !fields.length) continue;
      const group = document.createElement('div');
      group.className = 'rag-step';
      group.dataset.step = step.key;
      const tag = document.createElement('span');
      tag.className = 'rag-step-tag';
      tag.textContent = step.short || step.key;
      group.appendChild(tag);
      for (const field of fields) group.appendChild(ragFieldRow(field, cfg));
      for (const role of roles) group.appendChild(ragModelRow(role, cfg));
      box.appendChild(group);
    }
    return box;
  }

  // A metric's name wherever it appears outside a score card: the lab's label,
  // the step's ink, and the same explainer behind the same '!'.
  function ragMeasureCell(key) {
    const measure = ragMeasure(key);
    const cell = document.createElement('span');
    cell.className = 'rag-measure';
    if (measure.step) cell.dataset.step = measure.step;
    cell.textContent = measure.label;
    const why = ragWhy('metric.' + key, cell);
    if (why) cell.appendChild(why);
    return cell;
  }

  function ragTable(head, rows, className) {
    const table = document.createElement('table');
    table.className = 'rag-table' + (className ? ' ' + className : '');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    for (const cell of head) {
      const th = document.createElement('th');
      th.textContent = cell;
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const body = document.createElement('tbody');
    for (const row of rows) {
      const tr = document.createElement('tr');
      for (const cell of row) {
        const td = document.createElement('td');
        if (cell instanceof Node) td.appendChild(cell); else td.textContent = cell;
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
    table.appendChild(body);
    const scroll = document.createElement('div');
    scroll.className = 'rag-scroll';
    scroll.appendChild(table);
    return scroll;
  }

  function renderRagResult() {
    const result = ragState.result;
    const wrap = document.createElement('section');
    wrap.className = 'rag-results';
    const head = document.createElement('div');
    head.className = 'rag-results-head';
    const title = document.createElement('h3');
    title.textContent = 'Grades';
    head.appendChild(title);
    const meta = document.createElement('span');
    meta.className = 'rag-meta';
    meta.textContent = `${result.label || 'run'} · ${result.summary.n_questions} questions · `
      + `${result.index.chunks} chunks · ${result.seconds}s`;
    head.appendChild(meta);
    wrap.appendChild(head);

    for (const note of result.notes || []) {
      const line = document.createElement('p');
      line.className = 'rag-note';
      line.textContent = note;
      wrap.appendChild(line);
    }

    const scores = document.createElement('div');
    scores.className = 'rag-figures';
    const overall = result.summary.overall;
    for (const measure of ragMeasures()) {
      const key = measure.key;
      if (overall[key] === null || overall[key] === undefined) continue;
      const card = document.createElement('div');
      card.className = 'rag-figure';
      // The ink of the step it grades, so a retrieval number is the same green
      // wherever it appears on the page. '' = whole pipeline, left uncoloured.
      if (measure.step) card.dataset.step = measure.step;
      const name = document.createElement('span');
      name.className = 'rag-figure-label';
      name.textContent = measure.label;
      // A score nobody can check is worse than no score, so the formula and the
      // library that produced it are one click away — the same click as the knobs.
      const why = ragWhy('metric.' + key, name);
      if (why) name.appendChild(why);
      const value = document.createElement('b');
      value.textContent = key === 'latency_ms' ? Math.round(overall[key]) : ragNum(overall[key]);
      const hint = document.createElement('span');
      hint.className = 'rag-figure-foot';
      hint.textContent = measure.short;
      card.append(name, value, hint);
      if (key !== 'latency_ms') {
        const bar = document.createElement('div');
        bar.className = 'rag-bar';
        const fill = document.createElement('i');
        fill.style.width = `${Math.max(0, Math.min(1, overall[key])) * 100}%`;
        bar.appendChild(fill);
        card.appendChild(bar);
      }
      scores.appendChild(card);
    }
    wrap.appendChild(scores);

    const byType = Object.entries(result.summary.by_type);
    if (byType.length) {
      const caption = document.createElement('h4');
      caption.textContent = 'By question type';
      wrap.appendChild(caption);
      // Same metrics, so the same names the score cards used — a table header
      // that says "Quote" where the card says something else is two concepts.
      wrap.appendChild(ragTable(
        ['Type', 'n', ...['recall', 'quote_recall', 'ndcg', 'hit',
          'abstained_correctly', 'false_abstention'].map((k) => ragMeasure(k).label)],
        byType.map(([name, row]) => [name, String(row.n), ragNum(row.recall),
          ragNum(row.quote_recall), ragNum(row.ndcg), ragNum(row.hit),
          ragNum(row.abstained_correctly), ragNum(row.false_abstention)])));
    }

    const ragas = (result.ragas && result.ragas.metrics) || {};
    const caption = document.createElement('h4');
    caption.textContent = 'RAGAS';
    wrap.appendChild(caption);

    // The deciding score gets its own card ahead of the table, because it is the
    // one number that chose this architecture — and it lives in ragas.decision
    // rather than summary.overall, so the loop above never sees it.
    const decision = result.ragas ? result.ragas.decision : undefined;
    const deciders = (result.ragas && result.ragas.decision_metrics) || [];
    if (decision !== null && decision !== undefined) {
      const measure = ragMeasure('ragas_decision');
      const card = document.createElement('div');
      card.className = 'rag-figure rag-figure-decision';
      const name = document.createElement('span');
      name.className = 'rag-figure-label';
      name.textContent = measure.label;
      const why = ragWhy('metric.ragas_decision', name);
      if (why) name.appendChild(why);
      const value = document.createElement('b');
      value.textContent = ragNum(decision);
      // The error belongs beside the mean, not in a footnote: these candidates
      // sit within 0.01 of each other, and three decimal places with no spread
      // reads as precision the run does not have.
      const spread = (result.ragas && result.ragas.decision_spread) || {};
      if (spread.stderr !== null && spread.stderr !== undefined) {
        const error = document.createElement('span');
        error.className = 'rag-stderr';
        error.textContent = `± ${ragNum(spread.stderr)}`;
        value.appendChild(error);
      }
      const hint = document.createElement('span');
      hint.className = 'rag-figure-foot';
      hint.textContent = deciders.length
        ? `mean of ${deciders.length}: ${deciders.join(', ')}` : measure.short;
      if (spread.n) hint.textContent += ` · standard error over ${spread.n} questions`;
      const bar = document.createElement('div');
      bar.className = 'rag-bar';
      const fill = document.createElement('i');
      fill.style.width = `${Math.max(0, Math.min(1, decision)) * 100}%`;
      bar.appendChild(fill);
      card.append(name, value, hint, bar);
      wrap.appendChild(card);
    } else if (deciders.length) {
      const line = document.createElement('p');
      line.className = 'rag-note';
      line.textContent = 'Unranked: this run did not measure all four deciding '
        + `metrics (${deciders.join(', ')}), so it has no decision score. `
        + 'Only a judged run with an answerer can be ranked.';
      wrap.appendChild(line);
    }

    if (Object.keys(ragas).length) {
      // RAGAS's metrics are somebody else's definitions computed by somebody
      // else's code, which is exactly why they get the same explainer as ours.
      wrap.appendChild(ragTable(['Metric', 'Score'],
        Object.entries(ragas).map(([k, v]) => [ragMeasureCell(k), ragNum(v)])));
    } else {
      const none = document.createElement('p');
      none.className = 'rag-note';
      none.textContent = 'No RAGAS scores for this run.';
      wrap.appendChild(none);
    }
    for (const note of (result.ragas && result.ragas.notes) || []) {
      const line = document.createElement('p');
      line.className = 'rag-note';
      line.textContent = note;
      wrap.appendChild(line);
    }
    return wrap;
  }

  function renderRagQuery() {
    const box = document.createElement('section');
    box.className = 'rag-panel rag-query';
    const legend = document.createElement('h3');
    legend.textContent = 'Ask one question';
    box.appendChild(legend);

    const form = document.createElement('form');
    form.className = 'rag-ask';
    const input = document.createElement('input');
    input.type = 'text';
    input.id = 'raglab-question';
    input.className = 'rag-question';
    input.dir = 'rtl';
    input.placeholder = 'الان وضعیت کارم چیه؟';
    input.value = ragState.question;
    input.addEventListener('input', () => { ragState.question = input.value; });
    const pick = document.createElement('select');
    pick.className = 'rag-pick';
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = 'from the ground truth…';
    pick.appendChild(blank);
    for (const q of ragState.questions || []) {
      const opt = document.createElement('option');
      opt.value = q.question_fa;
      opt.textContent = `${q.id} · ${q.type} · ${q.question_en.slice(0, 54)}`;
      pick.appendChild(opt);
    }
    pick.addEventListener('change', () => {
      if (!pick.value) return;
      ragState.question = pick.value;
      input.value = pick.value;
    });
    const ask = document.createElement('button');
    ask.type = 'submit';
    ask.className = 'btn primary';
    ask.id = 'raglab-ask';
    ask.textContent = 'Retrieve';
    ask.disabled = ragState.busy;
    form.append(input, pick, ask);
    form.addEventListener('submit', (event) => { event.preventDefault(); ragAsk(); });
    box.appendChild(form);

    if (ragState.queryProblem) {
      const problem = document.createElement('p');
      problem.className = 'rag-note';
      problem.textContent = ragState.queryProblem;
      box.appendChild(problem);
    }

    const out = ragState.queryOut;
    if (out) {
      const diag = document.createElement('p');
      diag.className = 'rag-meta';
      const scope = out.time_scope
        ? `time scope ${out.time_scope.label} (${out.time_scope.from} → ${out.time_scope.to})`
        : 'no time scope detected';
      diag.textContent = `${scope} · ${out.diagnostics.candidates_in_scope} chunks in scope`
        + ` · dense ${out.diagnostics.dense_hits} · lexical ${out.diagnostics.bm25_hits}`
        + ` · graded out ${out.diagnostics.graded_out || 0}`;
      box.appendChild(diag);
      if (out.answer) {
        const answer = document.createElement('p');
        answer.className = 'rag-answer';
        answer.dir = 'rtl';
        answer.textContent = (out.abstained ? '(abstained) ' : '') + out.answer;
        box.appendChild(answer);
      }
      for (const context of out.contexts) {
        const item = document.createElement('div');
        item.className = 'rag-context';
        const meta = document.createElement('div');
        meta.className = 'rag-meta';
        // There is one kind of row in the index, so the chunk id and its date
        // are the whole of what identifies a hit.
        meta.textContent = `${context.chunk_id} · ${context.date}`
          + ` · score ${ragNum(context.score)}`;
        const text = document.createElement('div');
        text.className = 'rag-context-text';
        text.dir = 'rtl';
        text.textContent = context.text;
        item.append(meta, text);
        box.appendChild(item);
      }
    }
    return box;
  }

  function renderRagLab() {
    const sheet = document.createElement('section');
    sheet.className = 'raglab-sheet';

    const head = document.createElement('div');
    head.className = 'assistant-head';
    const heading = document.createElement('h2');
    heading.textContent = 'RAG test lab';
    head.appendChild(heading);
    const back = document.createElement('button');
    back.type = 'button';
    back.id = 'raglab-back';
    back.className = 'btn ghost';
    back.textContent = '← Assistant';
    back.addEventListener('click', () => setView('assistant'));
    head.appendChild(back);
    sheet.appendChild(head);

    const blurb = document.createElement('p');
    blurb.className = 'rag-blurb';
    blurb.textContent = 'Tune how diary conversations are chunked, stored and retrieved, '
      + 'then grade the result against the synthetic year of test data. Nothing here '
      + 'touches your board or your real chat memory.';
    sheet.appendChild(blurb);

    if (ragState.phase === 'idle') ragLoad();

    if (ragState.phase !== 'ready') {
      const card = document.createElement('div');
      card.className = 'rag-absent';
      const title = document.createElement('h3');
      title.textContent = ragState.phase === 'loading'
        ? 'Reaching the lab…' : 'The lab service is not running';
      card.appendChild(title);
      if (ragState.phase === 'absent') {
        const how = document.createElement('p');
        how.textContent = 'Start it in a terminal, then reload this page:';
        card.appendChild(how);
        const cmd = document.createElement('pre');
        cmd.className = 'rag-cmd';
        cmd.textContent = 'npm run raglab';
        card.appendChild(cmd);
        const why = document.createElement('p');
        why.className = 'rag-meta';
        why.textContent = `It serves on :9002 and this board proxies to it. (${ragState.problem})`;
        card.appendChild(why);
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn ghost';
        retry.id = 'raglab-retry';
        retry.textContent = 'Try again';
        retry.addEventListener('click', () => ragLoad());
        card.appendChild(retry);
      }
      sheet.appendChild(card);
      return sheet;
    }

    const options = ragState.options;
    const cfg = ragConfig();

    const facts = document.createElement('p');
    facts.className = 'rag-meta rag-corpus';
    const corpus = options.corpus;
    facts.textContent = `${corpus.sessions} sessions · ${corpus.messages} messages · `
      + `${corpus.from} → ${corpus.to} · ${corpus.questions} ground-truth questions · `
      + `asked as of ${corpus.query_date}`;
    sheet.appendChild(facts);

    const caps = document.createElement('div');
    caps.className = 'rag-caps';
    const capability = (on, text) => {
      const chip = document.createElement('span');
      chip.className = 'rag-cap ' + (on ? 'on' : 'off');
      chip.textContent = text;
      caps.appendChild(chip);
    };
    const c = options.capabilities;
    capability(c.fastembed, c.fastembed ? 'embeddings ready' : 'fastembed missing');
    capability(c.cross_encoder, c.cross_encoder ? 'cross-encoder ready' : 'cross-encoder missing');
    // Provider as well as model: on ollama the run is free and private, on
    // openrouter it is billed, and on the fake provider every LLM number on the
    // results screen is meaningless. One chip cannot say that if it only names
    // the slug.
    capability(c.llm, c.llm ? `${c.llm_provider} · ${c.llm_model}`
                            : 'no LLM backend');
    capability(c.ragas.installed, c.ragas.installed ? `ragas ${c.ragas.version}` : 'ragas missing');
    // Where the experiment lives, not which service holds it: the index is
    // process memory and the only thing kept is the JSON run. This chip used to
    // name a Chroma database, which is exactly the impression to avoid — that a
    // lab run leaves a store behind for the next one to find.
    capability(true, `index in memory · runs → ${c.storage.runs}`);
    sheet.appendChild(caps);

    // Steps on the left in pipeline order, every model on the right. The step
    // panels carry the ink; the model column repeats it per group, so a glance
    // says which stage a dropdown belongs to.
    const grid = document.createElement('div');
    grid.className = 'rag-grid';
    const steps = document.createElement('div');
    steps.className = 'rag-steps';
    const panels = {};
    for (const step of ragSteps()) {
      panels[step.key] = ragFieldset(step, cfg);
      steps.appendChild(panels[step.key]);
    }
    grid.appendChild(steps);

    // The one-run controls are not configuration, but they belong with the step
    // whose output they grade.
    const scoring = panels.generation || steps.lastElementChild;
    const ragasLabel = document.createElement('label');
    ragasLabel.className = 'field';
    ragasLabel.append('RAGAS');
    const ragasWhy = ragWhy('run.ragas_mode', ragasLabel);
    if (ragasWhy) ragasLabel.appendChild(ragasWhy);
    const ragasSel = document.createElement('select');
    for (const [value, text] of [['offline', 'offline (no model)'],
      ['llm', 'judged (needs a model)'], ['off', 'off']]) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = text;
      ragasSel.appendChild(opt);
    }
    ragasSel.value = ragState.run.ragas_mode;
    ragasSel.addEventListener('change', () => { ragState.run.ragas_mode = ragasSel.value; });
    ragasLabel.appendChild(ragasSel);
    scoring.appendChild(ragasLabel);

    const limitLabel = document.createElement('label');
    limitLabel.className = 'field';
    limitLabel.append('Questions (0 = all)');
    const limitWhy = ragWhy('run.limit', limitLabel);
    if (limitWhy) limitLabel.appendChild(limitWhy);
    const limitInput = document.createElement('input');
    limitInput.type = 'number';
    limitInput.min = 0;
    limitInput.max = corpus.questions;
    limitInput.value = ragState.run.limit;
    limitInput.addEventListener('change', () => { ragState.run.limit = Number(limitInput.value); });
    limitLabel.appendChild(limitInput);
    scoring.appendChild(limitLabel);

    const labelField = document.createElement('label');
    labelField.className = 'field';
    labelField.append('Run label');
    const labelWhy = ragWhy('run.label', labelField);
    if (labelWhy) labelField.appendChild(labelWhy);
    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.id = 'raglab-label';
    labelInput.placeholder = 'e.g. semantic + hybrid + gate';
    labelInput.value = cfg.label || '';
    labelInput.addEventListener('input', () => { cfg.label = labelInput.value; ragPersist(); });
    labelField.appendChild(labelInput);
    scoring.appendChild(labelField);
    grid.appendChild(ragModelPanel(cfg));
    sheet.appendChild(grid);

    const actions = document.createElement('div');
    actions.className = 'rag-actions';
    const buildBtn = document.createElement('button');
    buildBtn.type = 'button';
    buildBtn.className = 'btn ghost';
    buildBtn.id = 'raglab-build';
    buildBtn.textContent = 'Build index';
    buildBtn.disabled = ragState.busy;
    buildBtn.addEventListener('click', () => ragStart('index', {}));
    const runBtn = document.createElement('button');
    runBtn.type = 'button';
    runBtn.className = 'btn primary';
    runBtn.id = 'raglab-run';
    runBtn.textContent = 'Run evaluation';
    runBtn.disabled = ragState.busy;
    runBtn.addEventListener('click', () => ragStart('run', {
      ragas_mode: ragState.run.ragas_mode,
      limit: ragState.run.limit || null,
      types: ragState.run.types,
    }));
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn ghost';
    cancelBtn.id = 'raglab-cancel';
    cancelBtn.textContent = 'Stop experiment';
    cancelBtn.disabled = !ragState.jobId;
    cancelBtn.addEventListener('click', ragCancel);
    actions.append(buildBtn, runBtn, cancelBtn);
    sheet.appendChild(actions);

    if (ragState.indexInfo) {
      const info = ragState.indexInfo;
      const line = document.createElement('p');
      line.className = 'rag-meta';
      line.textContent = `${info.collection}: ${info.chunks} chunks · `
        + `avg ${info.avg_chars} chars · dim ${info.embed_dim} · ${info.build_seconds}s`
        + (info.reused ? ' · reused' : '');
      sheet.appendChild(line);
      for (const note of info.notes || []) {
        const warn = document.createElement('p');
        warn.className = 'rag-note';
        warn.textContent = note;
        sheet.appendChild(warn);
      }
    }

    if (ragState.job) {
      const progress = document.createElement('div');
      progress.className = 'rag-progress';
      const label = document.createElement('span');
      label.className = 'rag-meta';
      // The detail ("question 16/30 · hard", "judge call 137 of ~420") is what
      // makes a judged run readable: on a local model one stage is hours, so a
      // percentage that only moves at stage boundaries looks like a hang.
      label.textContent = `${ragState.job.kind}: ${ragState.job.stage} `
        + `${Math.round((ragState.job.progress || 0) * 100)}%`
        + (ragState.job.detail ? ` · ${ragState.job.detail}` : '');
      const track = document.createElement('div');
      track.className = 'rag-bar';
      const fill = document.createElement('i');
      fill.style.width = `${(ragState.job.progress || 0) * 100}%`;
      track.appendChild(fill);
      progress.append(label, track);
      sheet.appendChild(progress);
    }

    if (ragState.problem) {
      const problem = document.createElement('p');
      problem.className = 'rag-note';
      problem.setAttribute('role', 'alert');
      problem.textContent = ragState.problem;
      sheet.appendChild(problem);
    }

    if (ragState.result) sheet.appendChild(renderRagResult());

    sheet.appendChild(renderRagQuery());

    if (ragState.runs.length) {
      const boardTitle = document.createElement('h3');
      boardTitle.textContent = 'Leaderboard';
      sheet.appendChild(boardTitle);
      // Why the ranking column is the one it is, next to the ranking. The
      // deterministic scores are still on the row — they are what you debug
      // with — but they do not choose, because they grade retrieval almost
      // exclusively and would reward a config that finds the evidence and then
      // says nothing useful about it.
      const basis = document.createElement('p');
      basis.className = 'rag-note rag-basis';
      basis.textContent = 'Ranked by the RAGAS decision score — the unweighted '
        + 'mean of faithfulness, answer relevancy, context precision and context '
        + 'recall. Every other column is reported and none of them votes. Runs '
        + 'that could not measure all four are unranked and sort last. Where a '
        + 'row shows ±, that is the standard error on its own score: two rows '
        + 'whose intervals overlap have not been separated by this experiment.';
      sheet.appendChild(basis);
      // Unranked rows sort to the bottom rather than being dropped: a run that
      // could not be scored on all four is still a measurement.
      const ranked = ragState.runs.slice().sort((a, b) => {
        const x = a.ragas_decision, y = b.ragas_decision;
        const xs = x === null || x === undefined, ys = y === null || y === undefined;
        if (xs && ys) return 0;
        if (xs) return 1;
        if (ys) return -1;
        return y - x;
      });
      const rows = ranked.map((r) => {
        const overall = r.summary.overall || {};
        const judged = r.ragas || {};
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'btn ghost rag-open-run';
        open.textContent = r.label || r.run_id;
        open.addEventListener('click', async () => {
          try {
            ragState.result = await ragApi('/evaluations/' + r.run_id);
            ragState.problem = '';
          } catch (error) { ragState.problem = error.message; }
          render();
        });
        // The embedding model, not only the kind: two "fastembed" rows can be two
        // different representations, and the row has to say which one it was.
        const embedder = r.config.index.embedder
          + (r.config.index.embed_model
            ? '·' + r.config.index.embed_model.split('/').pop() : '');
        const decision = document.createElement('strong');
        decision.className = 'rag-decision';
        decision.textContent = ragNum(r.ragas_decision);
        // Absent on runs recorded before the spread was measured, and left
        // absent rather than shown as ± 0 — which would claim the oldest rows
        // were the most precisely measured ones.
        if (r.ragas_decision_stderr !== null
            && r.ragas_decision_stderr !== undefined) {
          const error = document.createElement('span');
          error.className = 'rag-stderr';
          error.textContent = `± ${ragNum(r.ragas_decision_stderr)}`;
          decision.appendChild(error);
        }
        // The deciding score, then its four constituents, so a row can be
        // checked rather than trusted.
        return [open, r.config.index.chunker, embedder,
          r.config.retrieval.retriever, r.config.retrieval.reranker,
          String(r.n_questions), decision,
          ragNum(judged.faithfulness), ragNum(judged.answer_relevancy),
          ragNum(judged.llm_context_precision_with_reference),
          ragNum(judged.context_recall),
          ragNum(overall.headline), ragNum(overall.recall),
          ragNum(overall.quote_recall), r.started_at];
      });
      sheet.appendChild(ragTable(['Run', 'Chunker', 'Embedder', 'Retriever',
        'Reranker', 'n', 'Decision ▼', 'Faith', 'Ans rel', 'Ctx prec',
        'Ctx recall', 'Composite', 'Recall', 'Quote', 'When'], rows, 'rag-board'));
    }

    return sheet;
  }

  function setView(next) {
    if (next === view || !VIEWS.includes(next)) return;
    view = next;
    try { localStorage.setItem(VIEW_KEY, view); } catch (_) { /* private mode */ }
    syncViewButtons();
    // Entering the Assistant: make sure the list is current, not stale.
    if (view === 'assistant') refreshProposals();
    // Entering the lab: probe again unless it already answered. The usual reason
    // for coming back is that the service was just started in a terminal, and
    // making the developer find a retry button for that is silly.
    if (view === 'raglab' && ragState.phase !== 'ready') ragLoad();
    dealCards = true; // re-deal for a gentle transition between views
    render();
    announce(`${VIEW_LABELS[view]} view`);
  }

  for (const btn of viewButtons) btn.addEventListener('click', () => setView(btn.dataset.view));
  syncViewButtons();

  // --------------------------------------------------------------------------
  // Go
  // --------------------------------------------------------------------------

  render();            // instant paint from localStorage
  initServerSync();    // then reconcile with the SQLite backend if one is running
  refreshProposals();  // and surface anything the Assistant left awaiting approval
})();
