import { assistantState } from '../assistant/session.js';
import { boardUrl } from '../core/boards.js';
import { cardLabel, typeVal } from '../core/cards.js';
import { catLabel } from '../core/categories.js';
import { TYPE_META } from '../core/constants.js';
import { short } from '../core/history.js';
import { adoptServerBoard } from '../core/sync.js';
import { announce, columnTitle, getCard } from './dom.js';
import { openDialog } from './edit-dialog.js';
import { render, syncProposalBadge } from './render.js';

// Proposals — cards the Assistant suggested, waiting for the user's yes.
// They are stored server-side but stay off the board until confirmed, so they
// live outside `state` and are fetched on their own.

const PROPOSALS_API = '/api/proposals';

export async function refreshProposals() {
  try {
    const res = await fetch(boardUrl(PROPOSALS_API), { headers: { Accept: 'application/json' } });
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

export function renderProposals() {
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

// A suggested EDIT is not a proposed card: the card already exists and is the
// user's. So there is no "approve" here that writes anything. Reviewing opens
// the ordinary edit dialog with the suggestion filled in, and the user's own
// save is what applies it — the same path any hand edit takes. The suggestion
// is then discarded, because it has been answered either way.
const EDITS_API = '/api/edits';
export let reviewingEditId = null;

/** Cleared by the card dialog's own close handler, which lives in
 *  ui/edit-dialog.js — see the note on the setters in core/state.js. */
export function setReviewingEditId(id) {
  reviewingEditId = id;
}

export async function refreshEdits() {
  try {
    const res = await fetch(boardUrl(EDITS_API), { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    if (data && Array.isArray(data.edits)) {
      assistantState.edits = data.edits;
      syncProposalBadge();
      render();
    }
  } catch { /* offline — leave the list as it was */ }
}

export async function discardEdit(id, spoken) {
  try {
    const res = await fetch(`${EDITS_API}/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`edit ${res.status}`);
    await refreshEdits();
    if (spoken) announce(spoken);
  } catch {
    announce('Could not reach the server — the suggestion is unchanged');
  }
}

// What the suggestion would change, in the user's words rather than field
// names, so the row can be read without opening anything.
function describeEdit(fields, card) {
  const say = {
    title: (v) => `title → “${short(v)}”`,
    notes: () => 'notes rewritten',
    type: (v) => `stamp → ${TYPE_META[typeVal(v)].label}`,
    category: (v) => `area → ${v ? catLabel(v) : 'none'}`,
    columnId: (v) => `move to ${columnTitle(v)}`,
    importance: (v) => `importance → ${v || 'none'}`,
    urgency: (v) => `urgency → ${v || 'none'}`,
    tags: (v) => `tags → ${(v || []).join(', ') || 'none'}`,
  };
  return Object.entries(fields)
    .filter(([key]) => say[key])
    .map(([key, value]) => say[key](value))
    .join(' · ') || 'no change';
}

export function renderSuggestedEdits() {
  const wrap = document.createElement('section');
  wrap.className = 'suggestions';
  const heading = document.createElement('h3');
  heading.className = 'suggestions-heading';
  const n = assistantState.edits.length;
  heading.textContent = `Suggested — ${n} change${n === 1 ? '' : 's'} to review`;
  wrap.appendChild(heading);

  for (const edit of assistantState.edits) {
    const card = getCard(edit.cardId);
    const row = document.createElement('article');
    row.className = 'suggestion';
    row.dataset.id = edit.id;

    const title = document.createElement('p');
    title.className = 'suggestion-title';
    // The card it is about, named the way the board names it.
    title.textContent = card ? `${cardLabel(card)} ${card.title}` : 'a card that has since gone';
    row.appendChild(title);

    const what = document.createElement('p');
    what.className = 'suggestion-change';
    what.textContent = describeEdit(edit.fields, card);
    row.appendChild(what);

    const actions = document.createElement('div');
    actions.className = 'suggestion-actions';
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'btn ghost suggestion-dismiss';
    dismiss.textContent = 'Dismiss';
    dismiss.addEventListener('click', () => discardEdit(edit.id, 'Suggestion dismissed'));
    actions.appendChild(dismiss);
    if (card) {
      const review = document.createElement('button');
      review.type = 'button';
      review.className = 'btn primary suggestion-review';
      review.textContent = 'Review & save';
      review.title = 'Opens the card with this change filled in — nothing is saved until you say so';
      review.addEventListener('click', () => {
        reviewingEditId = edit.id;
        openDialog(edit.cardId, edit.fields);
      });
      actions.appendChild(review);
    }
    row.appendChild(actions);
    wrap.appendChild(row);
  }
  return wrap;
}
