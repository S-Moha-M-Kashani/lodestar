// mcp/tools.mjs — the eight tools, and what each one does to the board.
//
// **The board's own spelling, everywhere.** A tool's arguments are named
// exactly as the card's fields are — `columnId`, `habitFreq`, `parentId` — so
// there is no snake_case↔camelCase table in this surface and therefore nothing
// for a new field to fall off. The brain's Widget 2 keeps such a table because
// it is writing Python; here the two ends are the same language and the table
// would be pure risk. The three filters with no card field of their own
// (`tagsAll`, `tagsAny`, `childOf`, `card`) are the only names invented here.
//
// **What a write may set, and what it may never.** `WRITABLE` is every field
// the board itself lets a person change. Deliberately absent, each for its own
// reason:
//
//   - `id`, `num`, `createdAt` — the ledger's, issued once and for life.
//   - `doneAt` — it follows the column. `resolveDoneAt` (server.js) stamps the
//     moment a card enters Done, so a tool that could set it directly could
//     claim a day nobody finished anything on.
//   - `habitHistory` — a record of what actually happened. A history a tool
//     can write into is not a record, which is the same rule that gives the
//     brain's agent no habit-completion tool.
//   - `effortSrc` / `controlSrc` / `planSrc` — provenance is *who chose* a
//     value, not what it is, so it is derived below rather than accepted.
//
// **Provenance is derived, and it has to be.** `resolvePlan` reads a plan
// whose `planSrc` is `'auto'` as "follow the deadline", so a tool that set a
// plan without also saying who chose it would watch the board resolve that
// plan straight back onto the due date — a plan set, reported and silently
// undone. The provenance written is `'user'`, on the 2026-09-08 reasoning that
// the person instructing this tool is the person choosing; `'ai'` would leave
// the owner unable to tell their own plan from one nobody looked at.
import { randomUUID } from 'node:crypto';

import { COLUMNS, TYPES } from '../js/core/constants.js';
import { DEFAULT_LIMIT, MAX_LIMIT, SORT_KEYS, selectCards } from './filter.mjs';
import { getBoards, getState, getTrash, mutate } from './board.mjs';

const COLUMN_IDS = COLUMNS.map((c) => c.id);
const COLUMN_LABEL = Object.fromEntries(COLUMNS.map((c) => [c.id, c.title]));

/** Every field of a card a person can change, and so every field a tool may
 *  send. The three `*Src` companions are derived from these, never taken. */
const WRITABLE = ['title', 'notes', 'type', 'category', 'columnId', 'importance',
  'urgency', 'effort', 'control', 'deadline', 'plan', 'tags', 'habitFreq',
  'habitCount', 'habitTimes', 'parentId'];

const SRC_OF = { plan: 'planSrc', effort: 'effortSrc', control: 'controlSrc' };

// ── schema helpers ─────────────────────────────────────────────────────────
const str = (description, extra = {}) => ({ type: 'string', description, ...extra });
const enumOf = (values, description) => ({ type: 'string', enum: values, description });
const list = (description) => ({ type: 'array', items: { type: 'string' }, description });

const SPAN = (description) => ({
  type: 'object', description,
  properties: {
    preset: enumOf(['through-today', 'week', 'month', 'days30', 'year'],
      'a span relative to today; `through-today` is open at the start, so late work is in it'),
    start: str("the first day to include, 'YYYY-MM-DD' — ignored when a preset is given"),
    end: str("the last day to include, 'YYYY-MM-DD', the whole day"),
  },
});

const CARD_REF = str('which card: its id, or its ledger number like C-042');

/** The writable fields as a schema, shared by both write tools so the two
 *  doors cannot come to advertise different fields — the failure
 *  `brain/…/widget2/tools/fields.py` was written to end, one language over. */
const cardFields = () => ({
  title: str("the card's own words, one line"),
  notes: str('the body text, free-form'),
  type: enumOf(TYPES, 'the kind of thought this is'),
  category: str("the life area's id, lower-case — `describe_board` lists them; '' is uncategorised"),
  columnId: enumOf(COLUMN_IDS,
    "where it lives; 'answered' IS Done — this is how a card is finished, and the board stamps the date"),
  importance: enumOf(['high', 'low', ''], "'' is a card nobody has judged"),
  urgency: enumOf(['high', 'low', ''], "'' is a card nobody has judged"),
  effort: enumOf(['low', 'medium', 'high'], 'how much work it is'),
  control: enumOf(['act', 'influence', 'none'], 'how much of it is yours to do'),
  deadline: str("when it is due: 'YYYY-MM-DD', or '' for none"),
  plan: str("when you mean to get to it, at the precision you mean it: '2027', "
    + "'2027-03' or '2027-03-04'. Not a deadline"),
  tags: list('replaces the whole tag list'),
  habitFreq: enumOf(['daily', 'weekly', 'monthly', 'yearly', ''], 'habits only: how often the period comes round'),
  habitCount: { type: 'integer', minimum: 1, maximum: 99,
    description: 'habits only: repetitions per period' },
  habitTimes: list("habits only: one time of day per repetition, like '08:00'"),
  parentId: str('the id of the card this one is part of, or \'\' to detach it'),
});

const FILTERS = {
  keyword: str("free text, matched as the board's own search box matches it: "
    + "anywhere in the card's title, notes or tags, ignoring case"),
  columnId: enumOf(COLUMN_IDS, "where the card lives; 'answered' IS Done"),
  type: enumOf(TYPES),
  category: str("a life area's id, or '' for the cards filed under none"),
  importance: enumOf(['high', 'low', ''], "'' asks for the cards nobody has judged"),
  urgency: enumOf(['high', 'low', ''], "'' asks for the cards nobody has judged"),
  effort: enumOf(['low', 'medium', 'high']),
  control: enumOf(['act', 'influence', 'none']),
  habitFreq: enumOf(['daily', 'weekly', 'monthly', 'yearly', ''],
    "a habit's cadence; '' is every card that is not a habit"),
  tagsAll: list('only cards carrying every one of these tags'),
  tagsAny: list('only cards carrying at least one of these tags'),
  captured: SPAN('when it was written down'),
  finished: SPAN('when it was finished — unfinished cards match no such span'),
  updated: SPAN('when it was last changed'),
  deadline: SPAN('when it is due'),
  plan: SPAN("when you mean to get to it. A plan is the period it names, so a "
    + "card planned for '2027' matches any week of 2027"),
  hasDeadline: { type: 'boolean', description: 'true for cards with a due date at all, false for cards with none' },
  scope: enumOf(['all', 'top', 'children'],
    "'top' is cards that are not part of another card, 'children' is cards that are"),
  childOf: str('only the cards that are part of this one, by its id'),
  sortBy: enumOf(SORT_KEYS, 'which field orders the answer; cards with no value for it come last'),
  order: enumOf(['asc', 'desc'], "'desc' is latest first; for `priority`, 'asc' puts urgent and important first"),
  limit: { type: 'integer', minimum: 1, maximum: MAX_LIMIT,
    description: `how many cards to list (default ${DEFAULT_LIMIT}). The answer always states the true total` },
  board: str("which board, by id — omit for the one the board API answers with by default"),
};

// ── rendering ──────────────────────────────────────────────────────────────
const label = (card) => 'C-' + String(card.num || 0).padStart(3, '0');

/** One card, one line: what it is, where it lives, when it is due, its words,
 *  and its id — which is there because every write tool needs one, and a line
 *  a caller cannot act on is a line they have to ask about twice. */
function renderLine(card, areas) {
  const bits = [label(card), card.type, COLUMN_LABEL[card.columnId] || card.columnId];
  if (card.category) bits.push(areas.get(card.category) || card.category);
  if (card.deadline) bits.push('due ' + card.deadline);
  if (card.plan) bits.push('plan ' + card.plan);
  if (card.tags?.length) bits.push('#' + card.tags.join(' #'));
  return `${bits.join(' · ')} — ${card.title}  [${card.id}]`;
}

/** One card, in full. Every field a write tool can set, so a caller can read a
 *  card and change it without a second round trip — and the read-only ones,
 *  named as such rather than silently absent. */
function renderFull(card, areas, children = []) {
  const lines = [
    `${label(card)} — ${card.title}`,
    `id: ${card.id}`,
    `columnId: ${card.columnId} (${COLUMN_LABEL[card.columnId] || '?'})`,
    `type: ${card.type}`,
    `category: ${card.category || '—'}${card.category ? ` (${areas.get(card.category) || '?'})` : ''}`,
    `importance: ${card.importance || '—'}   urgency: ${card.urgency || '—'}`,
    `effort: ${card.effort}   control: ${card.control}`,
    `deadline: ${card.deadline || '—'}   plan: ${card.plan || '—'} (${card.planSrc})`,
    `tags: ${card.tags?.length ? card.tags.join(', ') : '—'}`,
    `parentId: ${card.parentId || '—'}`,
  ];
  if (card.type === 'habit') {
    lines.push(`habit: ${card.habitCount}× ${card.habitFreq || 'daily'}`
      + (card.habitTimes?.length ? `  times: ${JSON.stringify(card.habitTimes)}` : ''));
    lines.push(`habitHistory: ${Object.keys(card.habitHistory || {}).length} periods recorded (read-only)`);
  }
  lines.push(`createdAt: ${new Date(card.createdAt).toISOString()}`);
  lines.push(`updatedAt: ${new Date(card.updatedAt).toISOString()}`);
  if (card.doneAt) lines.push(`doneAt: ${new Date(card.doneAt).toISOString()}`);
  if (card.notes) lines.push('', 'notes:', card.notes);
  if (children.length) {
    lines.push('', `parts of this card (${children.length}):`);
    for (const kid of children) lines.push('  ' + renderLine(kid, areas));
  }
  return lines.join('\n');
}

const areaMap = (state) =>
  new Map((state.categories || []).map((c) => [c.id, c.label || c.name || c.id]));

// ── finding the card a caller means ────────────────────────────────────────
/** A card by id or by ledger number. Both, because the id is what a tool
 *  answer carries and `C-042` is what the owner reads off the board — being
 *  told "no such card" for the number printed on it would be the tool refusing
 *  its own vocabulary. */
function findCard(cards, ref) {
  const wanted = String(ref ?? '').trim();
  if (!wanted) throw new Error('Name a card: its id, or its ledger number like C-042.');
  const byId = cards.find((c) => c.id === wanted);
  if (byId) return byId;
  const digits = wanted.match(/^[Cc]-?0*(\d+)$/);
  if (digits) {
    const byNum = cards.find((c) => c.num === Number(digits[1]));
    if (byNum) return byNum;
  }
  throw new Error(`No card ${wanted} on this board. `
    + 'Use `list_cards` to find it — a deleted one is in `list_trash`.');
}

/** The fields this call actually named, plus the provenance they imply.
 *  A field the caller did not mention is absent and not `undefined`: on an
 *  edit, "leave this alone" and "set it to nothing" must not be the same
 *  instruction. */
function writable(args) {
  const patch = {};
  for (const field of WRITABLE) {
    if (!Object.hasOwn(args, field)) continue;
    patch[field] = args[field];
    if (SRC_OF[field]) patch[SRC_OF[field]] = 'user';
  }
  return patch;
}

const text = (body) => ({ content: [{ type: 'text', text: body }], isError: false });

function listing(cards, state, { total, heading }) {
  const areas = areaMap(state);
  if (!cards.length) return text(`${heading}\nNo cards match.`);
  const head = total > cards.length
    ? `${heading}\n${total} cards match; showing ${cards.length}. Raise \`limit\` for more.`
    : `${heading}\n${total} card${total === 1 ? '' : 's'}.`;
  return text([head, '', ...cards.map((c) => renderLine(c, areas))].join('\n'));
}

// ── the tools ──────────────────────────────────────────────────────────────
export const TOOLS = [
  {
    name: 'describe_board',
    description:
      'What this board is made of: the boards that exist, the life areas and '
      + 'their ids, every tag in use, and how many cards sit in each column. '
      + 'Read this before filtering by category or tag — the ids are the '
      + "board's own and are not guessable.",
    inputSchema: { type: 'object', properties: { board: FILTERS.board } },
    async run(args) {
      const [state, boards] = await Promise.all([getState(args.board), getBoards()]);
      const counts = COLUMNS.map((col) =>
        `${col.title}: ${state.cards.filter((c) => c.columnId === col.id).length}`);
      const tags = [...new Set(state.cards.flatMap((c) => c.tags || []))].sort();
      const areas = (state.categories || [])
        .map((c) => `  ${c.id} — ${c.label || c.name || c.id}`);
      const trash = await getTrash(args.board);
      return text([
        `Boards (default answers an unnamed request first): ${
          boards.boards.map((b) => `${b.name} [${b.id}]`).join(', ')}`,
        '',
        `${state.cards.length} live cards — ${counts.join(' · ')}`,
        `${trash.cards.length} in the Trash.`,
        '',
        'Life areas (the `category` filter takes the id):',
        ...(areas.length ? areas : ['  none']),
        '',
        `Tags in use: ${tags.length ? tags.join(', ') : 'none'}`,
      ].join('\n'));
    },
  },
  {
    name: 'list_cards',
    description:
      'Find cards. Every filter is optional and they combine with AND, so an '
      + 'unfiltered call lists the whole board newest first. `keyword` is the '
      + "board's own search. The answer always states how many matched before "
      + 'the cap, and carries each card id so it can be edited.',
    inputSchema: { type: 'object', properties: FILTERS },
    async run(args) {
      const state = await getState(args.board);
      const { total, cards } = selectCards(state.cards, args);
      return listing(cards, state, { total, heading: 'Cards matching this question:' });
    },
  },
  {
    name: 'get_card',
    description:
      'One card in full — every field, its provenance, and the cards that are '
      + 'part of it. This is what to read before changing a card.',
    inputSchema: {
      type: 'object', required: ['card'],
      properties: { card: CARD_REF, board: FILTERS.board },
    },
    async run(args) {
      const state = await getState(args.board);
      const card = findCard(state.cards, args.card);
      const children = state.cards.filter((c) => c.parentId === card.id);
      return text(renderFull(card, areaMap(state), children));
    },
  },
  {
    name: 'create_card',
    description:
      'Write a new card onto the board. Only the title is required — every '
      + 'other field has a board default, and a capture tool that demanded '
      + 'nine fields would be a worse one. The card is live immediately and '
      + 'earns its ledger number now.',
    inputSchema: {
      type: 'object', required: ['title'],
      properties: { ...cardFields(), board: FILTERS.board },
    },
    async run(args) {
      if (!String(args.title || '').trim()) throw new Error('A card needs a title.');
      // The Trash counts towards the next ledger number, and reading it is
      // what that costs. `ensureNums` (js/core/cards.js) takes its maximum over
      // the live cards alone, because the browser only ever holds those — so a
      // board whose highest card has been deleted mints that number a second
      // time, and restoring the first leaves two cards both reading C-395.
      // A ledger number belongs to a card for life (the rule a substitution in
      // js/core/importplan.js already obeys), and a card in the Trash is still
      // a card.
      const trash = await getTrash(args.board);
      // The id and the moment are minted once, outside the save, because
      // `mutate` may run this change twice — see its note. With the id fixed,
      // "the board with this card in it" is the same board however often it is
      // computed, where "the board plus this card" would be two cards.
      const id = randomUUID();
      const now = Date.now();
      let made;
      const state = await mutate(args.board, (board) => {
        const others = board.cards.filter((c) => c.id !== id);
        // The maximum is taken over the *other* cards and the Trash, so a
        // retry cannot read this card's own number and hand out the next one.
        const highest = [...others, ...trash.cards]
          .reduce((m, c) => Math.max(m, c.num || 0), 0);
        made = { id, columnId: 'inbox', ...writable(args), num: highest + 1,
                 createdAt: now, updatedAt: now };
        return [...others, made];
      });
      return text('Written down:\n'
        + renderLine(state.cards.find((c) => c.id === id) || made, areaMap(state)));
    },
  },
  {
    name: 'update_card',
    description:
      'Change a card. Only the fields named are touched; everything else is '
      + "left exactly as it was. Setting `columnId` to 'answered' is how a "
      + 'card is finished, and the board stamps the date itself.',
    inputSchema: {
      type: 'object', required: ['card'],
      properties: { card: CARD_REF, ...cardFields(), board: FILTERS.board },
    },
    async run(args) {
      let id;
      const state = await mutate(args.board, (board) => {
        const card = findCard(board.cards, args.card);
        id = card.id;
        const patch = writable(args);
        if (!Object.keys(patch).length) {
          throw new Error(`Nothing to change on ${label(card)} — name at least one field.`);
        }
        return board.cards.map((c) =>
          (c.id === id ? { ...c, ...patch, updatedAt: Date.now() } : c));
      });
      const after = state.cards.find((c) => c.id === id);
      return text('Changed:\n' + renderFull(after, areaMap(state),
        state.cards.filter((c) => c.parentId === id)));
    },
  },
  {
    name: 'delete_card',
    description:
      'Move a card to the Trash. It leaves the board and stays recoverable — '
      + '`list_trash` shows it and `restore_card` brings it back with the same '
      + 'ledger number. Cards that are part of this one are not deleted with '
      + 'it; they stay on the board, unparented.',
    inputSchema: {
      type: 'object', required: ['card'],
      properties: { card: CARD_REF, board: FILTERS.board },
    },
    async run(args) {
      let gone, orphans = 0;
      await mutate(args.board, (board) => {
        gone = findCard(board.cards, args.card);
        // Counted here and not after the save: `resolveLineage` (server.js)
        // blanks a link whose parent is not in the incoming list, so by the
        // time the board answers there is nothing left pointing at this card
        // and the count would always be 0 — a sentence that could never be
        // said, which is worse than no sentence.
        orphans = board.cards.filter((c) => c.parentId === gone.id).length;
        // The card is omitted from the whole-board save, and `writeBoard`'s
        // sweep archives it. That is the board's own deletion — one route, one
        // Trash, one undo — rather than a second path this tool would own.
        return board.cards.filter((c) => c.id !== gone.id);
      });
      return text(`${label(gone)} “${gone.title}” is in the Trash.`
        + (orphans ? ` Its ${orphans} part${orphans === 1 ? '' : 's'} stayed on the board.` : '')
        + '\nRestore it with `restore_card`, or empty the Trash on the board itself.');
    },
  },
  {
    name: 'list_trash',
    description:
      'The cards in the Trash, newest deletion first. Anything here can be '
      + 'brought back with `restore_card`.',
    inputSchema: {
      type: 'object',
      properties: { board: FILTERS.board, limit: FILTERS.limit },
    },
    async run(args) {
      const [trash, state] = await Promise.all([getTrash(args.board), getState(args.board)]);
      const cards = trash.cards.slice(0, Math.min(args.limit || DEFAULT_LIMIT, MAX_LIMIT));
      return listing(cards, state, { total: trash.cards.length, heading: 'In the Trash:' });
    },
  },
  {
    name: 'restore_card',
    description:
      'Bring a card back from the Trash onto the board, with the same id and '
      + 'the same ledger number it always had.',
    inputSchema: {
      type: 'object', required: ['card'],
      properties: { card: CARD_REF, board: FILTERS.board },
    },
    async run(args) {
      const trash = await getTrash(args.board);
      const card = findCard(trash.cards, args.card);
      // Adding it back to the whole-board save is the restore: the upsert
      // clears `deleted_at`, which is how the board's own Trash works. Filtered
      // first so a retry after a stale save — which has already put the card
      // back, additively — writes the same board rather than the card twice.
      const state = await mutate(args.board, (board) =>
        [...board.cards.filter((c) => c.id !== card.id), card]);
      const back = state.cards.find((c) => c.id === card.id);
      return text('Back on the board:\n' + renderLine(back || card, areaMap(state)));
    },
  },
];

export const TOOL_BY_NAME = new Map(TOOLS.map((tool) => [tool.name, tool]));
