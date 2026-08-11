
// The board's fixed vocabulary: its three columns, the card types and their
// neutral ink stamps, the priority labels, and the seven views.

export const COLUMNS = [
  { id: 'inbox', title: 'Inbox' },
  { id: 'in-progress', title: 'In Progress' },
  // 'answered' is the id every stored card and saved board already carries;
  // only the label changed, because the column finishes tasks and habits too.
  { id: 'answered', title: 'Done' },
];

// What kind of thing a card is — stamped on the card like the old priority
// stamp, but neutral ink: colour on this board always means category.
export const TYPES = ['question', 'problem', 'task', 'idea', 'plan', 'habit'];
export const TYPE_META = {
  question: { glyph: '?', label: 'question' },
  problem:  { glyph: '!', label: 'problem' },
  task:     { glyph: '✓', label: 'task' },
  idea:     { glyph: '✦', label: 'idea' },
  plan:     { glyph: '→', label: 'plan' },
  habit:    { glyph: '↻', label: 'habit' },
};
export const TYPE_RANK = Object.fromEntries(TYPES.map((t, i) => [t, i]));

// Automatic priority, derived from the same two judgements the Matrix uses —
// never stored. 1 urgent+important · 2 urgent only · 3 important only ·
// 4 neither; 0 (no label) until both judgements are set.
export const priorityOf = (c) => {
  if (!c.importance || !c.urgency) return 0;
  if (c.urgency === 'high') return c.importance === 'high' ? 1 : 2;
  return c.importance === 'high' ? 3 : 4;
};
export const PRIO_TITLE = ['', 'Urgent & important — answer now', 'Urgent, not important',
  'Important, not urgent', 'Neither urgent nor important'];

export const CONTROL_LABEL = { act: 'I can act', influence: 'I can influence', none: 'Out of my hands' };

export const VIEWS = ['board', 'backlog', 'overview', 'matrix', 'areas', 'review', 'assistant'];
export const VIEW_LABELS = { board: 'Board', backlog: 'Backlog', overview: 'Overview', matrix: 'Matrix', areas: 'Areas', review: 'Review', assistant: 'Assistant' };
