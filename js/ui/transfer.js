import { assistantState, newSessionId, refreshChatSessions } from '../assistant/session.js';
import { asanaToLodestar, isAsanaExport } from '../core/asana.js';
import { activeBoardId, boardUrl } from '../core/boards.js';
import { cardLabel, ensureNums, parseState, uid } from '../core/cards.js';
import { CAT_LIMIT, catById, categories, setCategories } from '../core/categories.js';
import { commit, short } from '../core/history.js';
import { applyStep, planImport } from '../core/importplan.js';
import { setCards, setDealCards, state } from '../core/state.js';
import { ask } from './dialogs.js';
import { closeReview, openReviewStep } from './edit-dialog.js';
import { $, announce } from './dom.js';

// Export and import — the board or one conversation, as JSON, Markdown or
// plain text, with the schema the importer accepts shown beside it.

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

export function openExportDialog(mode) {
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
export async function importChatFile(file) {
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
  // Its own chat, never the reserved 'adhoc' one an unnamed batch lands in: an
  // imported transcript should arrive as a conversation you can open, not as
  // loose rows at the bottom of somebody else's.
  const importedInto = newSessionId();
  try {
    const res = await fetch('/api/chat/messages', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sessionId: importedInto, boardId: activeBoardId, messages: clean }),
    });
    if (!res.ok) throw new Error(`the server refused the import (${res.status})`);
  } catch (err) {
    announce(`Import failed — ${err.message}`);
    return;
  }
  await refreshChatSessions();
  // The record is the truth; the recall index catches up here — or at the
  // brain's next start, which runs the same sync, if it is off or away.
  let indexed = ' (recall index catches up at the next brain start)';
  try {
    const rag = await (await fetch(boardUrl('/api/rag/chat/reindex'), { method: 'POST' })).json();
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
    "id": "any string, unique in the file  (optional — one is minted; needed only so another card can name it as its parent)",
    "title": "The card's text (required)",
    "columnId": "inbox | in-progress | answered",
    "type": "question | problem | task | idea | dream | habit",
    "category": "one of your category ids — work, love, family, health, mind, music, travel, home, money by default  (optional)",
    "importance": "high | low  (optional — for the Matrix)",
    "urgency": "high | low  (optional — for the Matrix)",
    "effort": "low | medium | high  (optional — defaults to medium)",
    "control": "act | influence | none  (optional — defaults to influence)",
    "deadline": "YYYY-MM-DD  (optional)",
    "plan": "when you mean to do it: 2027 | 2027-03 | 2027-03-04  (optional)",
    "planSrc": "auto | user  (optional — 'auto', the default, keeps the plan equal to the deadline)",
    "habitFreq": "daily | weekly | monthly | yearly  (habits only)",
    "habitCount": "how many times per period, 1-99  (habits only, defaults to 1)",
    "habitTimes": [{ "month": "01-12 (yearly only)", "weekday": "1-7, Monday first (weekly only)", "day": "01-31 (monthly and yearly)", "time": "HH:MM" }],
    "parentId": "the id of the card this one is part of  (optional — one level deep; a link this board cannot honour is dropped and the card kept)",
    "notes": "Optional free-form notes or the answer",
    "tags": ["optional", "lowercase", "tags"],
    "num": 12,
    "createdAt": 1721606400000,
    "updatedAt": 1721606400000,
    "doneAt": "when it was finished, ms since epoch  (optional — kept only on a card in Done, and one in Done that names none is given its updatedAt)"
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
    const text = await file.text();
    // An Asana export is translated first and then validated like any other
    // file — never trusted straight onto the board. A file that is not JSON at
    // all falls through to parseState, which is what reports it.
    let parsed = null;
    try { parsed = JSON.parse(text); } catch { /* parseState says so below */ }
    pendingImport = parseState(
      isAsanaExport(parsed) ? JSON.stringify(asanaToLodestar(parsed)) : text);
    const n = pendingImport.cards.length;
    // What adding it would actually do, worked out before the choice rather
    // than reported after it. Advisory only — the plan that is applied is built
    // again when Add is pressed, against the board as it stands then.
    const { steps, skipped } = planImport(pendingImport.cards, state.cards, uid);
    const replaced = steps.filter((st) => st.kind === 'substitute').length;
    const effect = [
      `${steps.length - replaced} new`,
      ...(replaced ? [`${replaced} replacing a card you have`] : []),
      ...(skipped ? [`${skipped} already on the board`] : []),
    ].join(', ');
    $('#import-mode-copy').textContent =
      `The file contains ${n} card${n === 1 ? '' : 's'}: ${effect}. Add `
      + `${n === 1 ? 'it' : 'them'} to the current board, or substitute the whole `
      + "board with the file's contents?";
    importModeDialog.showModal();
  } catch (err) {
    pendingImport = null;
    ask({
      title: 'Could not import this file',
      message: 'It is neither a Lodestar board nor an Asana export — open Import JSON to see (and copy) the expected schema.',
      cancelLabel: null,
    });
  }
});

$('#cancel-import-mode').addEventListener('click', () => {
  pendingImport = null;
  importModeDialog.close();
});

// The import being reviewed one card at a time, or null. `index` is the step on
// screen; the counts are what the closing announcement reports.
let review = null;

/** Accept one step of a review: apply it, record it, and count it.
 *
 *  One History entry per accepted step, naming the card — which is what makes a
 *  reviewed import traceable afterwards and every step of it separately
 *  undoable. A substitution is ONE entry naming both cards, not an "added" and
 *  a "deleted": accepting it was one decision, and two entries would mean two
 *  presses of undo to put back what one press of Accept did.
 *
 *  The ledger number is read after `ensureNums`, since that is where a card
 *  earns one — the same order the card dialog's own create path uses. Reached
 *  only from a running review, which is why it can count into `review` without
 *  asking whether there is one. */
function acceptStep(step) {
  const now = Date.now();
  const card = { ...step.card, createdAt: step.card.createdAt || now, updatedAt: now };
  state.cards = ensureNums(applyStep(state.cards, { ...step, card }));
  commit(step.kind === 'substitute'
    ? `Replaced ${cardLabel(step.replaces)} “${short(step.replaces.title)}” with ${cardLabel(card)}`
    : `Imported ${cardLabel(card)} “${short(card.title)}”`);
  review[step.kind === 'substitute' ? 'replaced' : 'added'] += 1;
}

/** What an import did, in one sentence, whether or not it was reviewed. */
function importReport({ added, replaced, skipped }) {
  const bits = [];
  if (added) bits.push(`Added ${added} card${added === 1 ? '' : 's'}`);
  if (replaced) bits.push(`replaced ${replaced}`);
  if (skipped) bits.push(`skipped ${skipped} already on the board`);
  return bits.length ? bits.join(', ') : 'Nothing to import';
}

/** Apply a whole plan as one act.
 *
 *  One History entry for the lot, deliberately: an import nobody reviewed is one
 *  decision, and the timeline keeps only HISTORY_LIMIT snapshots — a 200-card
 *  file writing 200 of them would push every earlier state off the end of the
 *  log to record something the user never looked at card by card. */
function takeAll(steps, skipped) {
  const replaced = steps.filter((s) => s.kind === 'substitute').length;
  const now = Date.now();
  let cards = state.cards;
  for (const step of steps) {
    cards = applyStep(cards, { ...step, card: { ...step.card, updatedAt: now } });
  }
  state.cards = ensureNums(cards);
  setDealCards(true);
  const report = importReport({ added: steps.length - replaced, replaced, skipped });
  commit(`${report} — imported`);
  announce(report);
}

function nextReviewStep() {
  if (!review) return;
  if (review.index >= review.steps.length) { endReview(); return; }
  openReviewStep(review.steps[review.index],
    { index: review.index + 1, total: review.steps.length }, onReviewDecision);
}

function endReview() {
  if (!review) return;
  const report = importReport(review);
  review = null;
  closeReview();
  setDealCards(true);
  announce(report);
}

/** One answer from the review dialog: 'accept', 'all', 'skip' or 'stop'. */
function onReviewDecision(kind, card) {
  if (!review) return;
  if (kind === 'stop') { review = null; return; }   // the dialog is already closed
  const step = review.steps[review.index];
  // The card the form says, not the card the file sent — the edits made in
  // review are the whole point of it.
  if (kind !== 'skip') acceptStep(card ? { ...step, card } : step);
  review.index += 1;
  if (kind === 'all') {
    // Everything left, as the file has it. The current card was taken above
    // with its edits, so "all the rest" is exactly what it says.
    for (; review.index < review.steps.length; review.index += 1) {
      acceptStep(review.steps[review.index]);
    }
  }
  nextReviewStep();
}

$('#import-add').addEventListener('click', () => {
  if (!pendingImport) return;
  // Categories the file defines but this board doesn't yet: adopt them, so
  // the imported cards keep their labels and colours.
  for (const cat of pendingImport.categories || []) {
    if (!catById(cat.id) && categories.length < CAT_LIMIT) categories.push({ ...cat });
  }
  // What the file means for this board, decided in full before anything is
  // written: which cards it adds, which of the board's it replaces, and which it
  // says nothing new about. Ids are re-issued here (that is what lets the same
  // file be imported twice safely) and every parent link is re-pointed with
  // them — a child left naming the id its *file* gave its parent would be
  // detached by the next save with nothing on screen saying so. Substituting the
  // whole board keeps the file's own ids and needs none of this.
  const { steps, skipped } = planImport(pendingImport.cards, state.cards, uid);
  const reviewing = $('#import-review').checked;
  pendingImport = null;
  importModeDialog.close();
  if (!steps.length) {
    announce(`Nothing to import — all ${skipped} card${skipped === 1 ? '' : 's'} in that file `
      + 'are already on the board');
    return;
  }
  if (!reviewing) { takeAll(steps, skipped); return; }
  review = { steps, skipped, index: 0, added: 0, replaced: 0 };
  nextReviewStep();
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
  if (pendingImport.categories) setCategories(pendingImport.categories.map((c) => ({ ...c })));
  setCards(pendingImport.cards);
  pendingImport = null;
  importModeDialog.close();
  setDealCards(true); // deal the imported cards in like a fresh sheet
  commit(`Imported ${n} card(s), substituted the board`);
  announce('Board substituted with the imported cards');
});
