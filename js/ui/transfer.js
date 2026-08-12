import { assistantState, newSessionId, refreshChatSessions } from '../assistant/session.js';
import { ensureNums, parseState, uid } from '../core/cards.js';
import { CAT_LIMIT, catById, categories, setCategories } from '../core/categories.js';
import { commit } from '../core/history.js';
import { setCards, setDealCards, state } from '../core/state.js';
import { ask } from './dialogs.js';
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
      body: JSON.stringify({ sessionId: importedInto, messages: clean }),
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
  setDealCards(true);
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
  if (pendingImport.categories) setCategories(pendingImport.categories.map((c) => ({ ...c })));
  setCards(pendingImport.cards);
  pendingImport = null;
  importModeDialog.close();
  setDealCards(true); // deal the imported cards in like a fresh sheet
  commit(`Imported ${n} card(s), substituted the board`);
  announce('Board substituted with the imported cards');
});
