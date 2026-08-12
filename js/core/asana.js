// An Asana project export, translated into the JSON the board's own importer
// already accepts. Deliberately not a second import pipeline: the result goes
// through parseState, the same add-or-substitute choice and the same
// whole-board PUT as a hand-written file, so nothing here can invent a way to
// reach the board that the ordinary import does not have.
//
// It imports nothing, and must keep importing nothing — every other core
// module reaches ui/dom.js sooner or later, and this one is unit-tested under
// node, where there is no document.

/** Does this parsed file look like `GET /projects/:gid/tasks?opt_expand=this`? */
export function isAsanaExport(data) {
  const rows = data && typeof data === 'object' ? data.data : null;
  const first = Array.isArray(rows) ? rows[0] : null;
  return !!first && typeof first === 'object'
    && typeof first.gid === 'string' && first.resource_type === 'task';
}

const text = (v) => (typeof v === 'string' ? v.trim() : '');

/** Milliseconds, or undefined so sanitizeCard falls back to "now" rather than
 *  stamping the card with NaN. */
const millis = (iso) => {
  const t = Date.parse(typeof iso === 'string' ? iso : '');
  return Number.isNaN(t) ? undefined : t;
};

/** Where the card came from, then its project and section names, lowercased.
 *  Asana's own `tags` array is a different thing and is empty in every export
 *  seen so far; the grouping a reader actually recognises is which project and
 *  which section a task sits in, and tags are the only place a card can hold
 *  either today.
 *
 *  The IMPORTED stamp comes first and is on every card without exception —
 *  including the nested subtasks, which inherit no memberships and would
 *  otherwise arrive untagged. Once the importer has reminted ids and ledger
 *  numbers, nothing else on the board records that a card was imported at all,
 *  so this tag is the only way to review a batch, or to find it again to
 *  undo it, after the undo history has rolled past. */
const IMPORTED = 'asana';

const tagsOf = (task) => {
  const out = [IMPORTED];
  for (const m of task.memberships || []) {
    for (const name of [m.project?.name, m.section?.name]) {
      const t = text(name).toLowerCase();
      if (t && !out.includes(t)) out.push(t);
    }
  }
  return out;
};

/** Asana names 26 sections in one real project; only one of them maps onto a
 *  column, and guessing at the rest ("Highest Priority" → importance?) would
 *  be reading somebody's filing system as a schema. The rest survive as tags. */
const columnOf = (task) => {
  if (task.completed) return 'answered';
  const inProgress = (task.memberships || [])
    .some((m) => /in progress/i.test(text(m.section?.name)));
  return inProgress ? 'in-progress' : 'inbox';
};

/** The task's own notes, kept whole, with what the board cannot model yet
 *  written underneath it: which task this one hangs off, and the row it came
 *  from. A card that loses its permalink cannot be checked against Asana. */
const notesOf = (task) => {
  const foot = [];
  const parent = text(task.parent?.name);
  if (parent) foot.push(`Subtask of: ${parent}`);
  const url = text(task.permalink_url);
  if (url) foot.push(`Asana: ${url}`);
  const body = text(task.notes);
  return [body, foot.join('\n')].filter(Boolean).join('\n\n');
};

const cardOf = (task) => ({
  title: text(task.name),
  notes: notesOf(task),
  // Every Asana row is something to do. The board's other types are
  // distinctions Asana never made, and a converter that guessed at them would
  // be putting words in the user's mouth.
  type: 'task',
  columnId: columnOf(task),
  // Left to deadlineVal, which rejects the shape-valid impossibilities.
  deadline: text(task.due_on),
  tags: tagsOf(task),
  createdAt: millis(task.created_at),
  // When it was finished, not when Asana last touched the row — the Review
  // view reads updatedAt to decide what has been left alone.
  updatedAt: millis(task.completed_at) ?? millis(task.modified_at),
});

/** Translate a parsed Asana export into the board's import JSON.
 *
 *  Subtasks become cards of their own rather than a checklist inside the
 *  parent: they carry their own notes, dates and completion, and folding them
 *  into text would throw all of that away for a tidier board.
 *
 *  Neither ids nor ledger numbers are minted here. The importer assigns both,
 *  which is what lets the same file be imported twice without collision. */
export function asanaToLodestar(data) {
  const cards = [];
  const seen = new Set();

  const visit = (task) => {
    if (!task || typeof task !== 'object') return;
    const gid = typeof task.gid === 'string' ? task.gid : '';
    // Asana lists a subtask twice when it is also a project member: nested
    // under its parent, and again at the top level with `parent` set. Without
    // this, every such task arrives as two cards that then drift apart.
    // The subtasks are still walked — the repeat may carry ones the first
    // sighting did not.
    if (!gid || !seen.has(gid)) {
      if (gid) seen.add(gid);
      if (text(task.name)) cards.push(cardOf(task));
    }
    for (const sub of task.subtasks || []) visit(sub);
  };

  for (const task of data.data) visit(task);
  return { version: 1, cards };
}

// Alternatives considered
// ------------------------
// The obvious library here is Asana's own SDK, and the obvious architecture is
// to skip the file entirely: authenticate, call the API, pull the project
// live. That is a better product for somebody who lives in Asana, and it is
// the wrong one here. It puts an OAuth flow and a stored token into a board
// whose whole promise is that it is local-first and holds a private life, and
// it buys nothing this file does not already have — the export is complete,
// the API returns the same task objects, and a one-off migration does not need
// to stay connected to the thing it migrated away from. A file also fails
// visibly: it is either in the box or it is not.
//
// The second alternative is to hand the export to the Assistant and let the
// model write the cards. It would map sections onto importance and urgency far
// better than columnOf() does, because that genuinely is a judgement. It was
// rejected for the first version because a model paraphrases where a converter
// transcribes, and because 235 tasks would arrive as 235 proposals behind the
// confirmation gate. The measurement that would change this: run the export
// through the agent and count how many cards come back with the user's own
// wording intact. Below roughly 95%, a converter is the honest tool, and the
// model belongs afterwards — "look at these imported cards and suggest
// categories" is a good prompt, and it is one the board can already run.
