import { setChatScroll } from './streaming.js';
import { uid } from '../core/cards.js';
import { short } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';
import { ask } from '../ui/dialogs.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';

// Assistant view — chat with the brain service via /api/agent/chat.
//
// The conversation itself: which chat is open, what it holds, and every call
// that reads or writes the record. The sheet that draws it is sheet.js, and
// the turn-by-turn rendering is transcript.js.
// `draft` holds the composer text across re-renders — render() rebuilds the
// textarea, so anything typed (or dictated) has to live in state, not the DOM.
//
// `sessionId` is the chat being read and written. Everything else about the
// Assistant is per-chat: the transcript, the window one turn carries, and what
// the drift nudge compares against. `drift` holds a verdict awaiting the user's
// answer, with the message it is about — the turn is NOT sent while it sits
// there. `driftDismissed` is per-chat and deliberately sticky: told once that
// this conversation is broad, the nudge must not ask again in it.

export const assistantState = {
  messages: [], busy: false, draft: '', proposals: [], edits: [],
  sessionId: '', sessions: [], drift: null, driftDismissed: false,
  historyOpen: false, trash: [],
};

// The chat list and every message live in assistant.db. localStorage keeps two
// things only: which chat was open, and a per-chat cache of the transcript.
//
// The cache is not the record — it is the crash net for what the record cannot
// hold. The brain writes a turn once the model has answered, so a turn that
// died before that (a 500, a dropped connection) exists nowhere else, and
// "never lose a thought" is the pillar this project is built on. On open the
// longer of the two wins, which is safe because the browser only ever appends:
// the cache can be ahead of the record by un-recorded turns, never different.
const CHAT_SESSION_KEY = KEY_PREFIX + 'chat-session';
export const chatCacheKey = (id) => KEY_PREFIX + 'chat:' + id;
const CHAT_KEEP = 200;

// Opening the Assistant resumes the chat you were in — unless you have been
// away long enough that yesterday's thread is plainly over, when a fresh one
// is the better guess. A judgement call, not a measurement: short enough that
// a new day starts a new chat, long enough to survive lunch. Written as one
// literal (four hours) rather than as arithmetic, because tests/context.test.js
// reads the number out of this source and can only read a literal.
const RESUME_WITHIN_MS = 14_400_000;

// What one request carries. CHAT_KEEP above is the READER's cache — bigger is
// better and costs nothing. This is the MODEL's window: the newest
// CONTEXT_MESSAGES of THIS chat, trimmed again if they overrun CONTEXT_CHARS.
// Everything older stays on screen and in assistant.db, reachable through
// recall_chat — it just stops riding along on every turn, so turn fifty costs
// what turn five does. tests/context.test.js pins these against the brain's
// caps.
export const CONTEXT_MESSAGES = 16;
const CONTEXT_CHARS = 24_000;

/** A turn the model may be shown again. Errors and abandoned partials are
 *  rendered but never replayed — the model must not continue from something
 *  it never finished saying. One definition, shared by the send path and the
 *  transcript's trim marker, so the two can never disagree about where the
 *  window starts. */
export const replayable = (m) => !m.error && !m.partial
  && (m.role === 'user' || m.role === 'assistant');

/** The slice of a replayable history one request carries — a contiguous tail
 *  of THIS chat, and nothing else. Returns the original message objects plus
 *  `from`, the index where the window begins (0 when nothing was left out), so
 *  the transcript can mark the boundary with the same arithmetic the request
 *  used.
 *
 *  Nothing is pinned outside the budget. It used to be: the transcript's first
 *  user message rode along on every turn to say what the conversation was
 *  about, which with one endless transcript meant the subject of the FIRST
 *  conversation this board ever had was stapled to the top of every request
 *  forever — so a new question got an answer about an old one. The session
 *  boundary does that job now, and does it correctly. */
export function contextWindow(history) {
  let from = Math.max(0, history.length - CONTEXT_MESSAGES);
  let size = 0;
  for (let i = from; i < history.length; i += 1) size += history[i].content.length;
  // The char budget trims the window further, never below the newest message:
  // one oversized turn should cost context, not the ability to ask at all.
  // (The brain's own 120k cap remains the backstop for that case.)
  while (from < history.length - 1 && size > CONTEXT_CHARS) {
    size -= history[from].content.length;
    from += 1;
  }
  return { messages: history.slice(from), from };
}

export const persistChat = () => {
  if (!assistantState.sessionId) return;
  try {
    localStorage.setItem(chatCacheKey(assistantState.sessionId),
      JSON.stringify(assistantState.messages.slice(-CHAT_KEEP)));
    localStorage.setItem(CHAT_SESSION_KEY, assistantState.sessionId);
  } catch { /* private mode or quota — the transcript still holds this session */ }
};

/** One stored turn, read back defensively. Anything whose role or content is
 *  not what it claims is dropped rather than rendered. */
function restoredMessage(msg) {
  if (!msg || typeof msg !== 'object') return null;
  if (msg.role !== 'user' && msg.role !== 'assistant') return null;
  if (typeof msg.content !== 'string') return null;
  const out = { role: msg.role, content: msg.content };
  // The record's row id, kept through the cache. It is what a per-message
  // delete acts on: without it the turn on screen and the row in assistant.db
  // cannot be told to be the same turn. A turn that has no id yet — an error,
  // an abandoned partial, or one the brain has not recorded — simply has no
  // delete control, which is honest: there is nothing in the record to delete.
  if (Number.isInteger(msg.id)) out.id = msg.id;
  // `error` and `partial` are load-bearing, not decoration: sendChat filters
  // both out of the history it replays to the model. Persisting the text and
  // dropping the flag would silently undo that filter, and the model would be
  // asked to continue from something it never finished saying.
  if (msg.error) out.error = true;
  if (msg.partial) out.partial = true;
  if (Array.isArray(msg.steps)) out.steps = msg.steps;
  if (Array.isArray(msg.sources)) out.sources = msg.sources;
  if (msg.usage && typeof msg.usage === 'object') out.usage = msg.usage;
  // The session total is the sum of what each turn cost, so the figures have to
  // survive a reload with the transcript they belong to. Only a real number is
  // taken: a restored null or a string would otherwise turn the whole total
  // into NaN, and one unreadable turn must not erase the bill for the rest.
  if (typeof msg.cost === 'number' && Number.isFinite(msg.cost)) out.cost = msg.cost;
  // `running` is never restored. It names tools awaiting an answer from a
  // request that died with the old page, so a restored one is a spinner that
  // can never stop.
  return out;
}

/** The cached transcript for one chat, or [] when there is none. */
function cachedChat(id) {
  try {
    const saved = JSON.parse(localStorage.getItem(chatCacheKey(id)) || '[]');
    return Array.isArray(saved) ? saved.map(restoredMessage).filter(Boolean) : [];
  } catch { return []; }   // unreadable cache — the record is the truth anyway
}

export const newSessionId = () => 'chat-' + uid();

/** Start a fresh chat. Nothing is written: an empty chat is not persisted
 *  until its first message, so pressing New chat twice — or glancing at the
 *  Board and coming back — cannot litter the history panel. */
export function startNewChat({ focus = true } = {}) {
  assistantState.sessionId = newSessionId();
  assistantState.messages = [];
  assistantState.drift = null;
  assistantState.driftDismissed = false;
  assistantState.historyOpen = false;
  setChatScroll({ top: 0, pinned: true });
  try { localStorage.setItem(CHAT_SESSION_KEY, assistantState.sessionId); }
  catch { /* private mode */ }
  if (view === 'assistant') {
    render();
    if (focus) document.getElementById('chat-input')?.focus();
  }
}

/** Open an existing chat: its transcript from the record, or the local cache
 *  if that is ahead (a turn that died before the brain could record it). */
export async function openChatSession(id) {
  assistantState.sessionId = id;
  assistantState.drift = null;
  assistantState.driftDismissed = false;
  assistantState.historyOpen = false;
  const cached = cachedChat(id);
  assistantState.messages = cached;
  setChatScroll({ top: 0, pinned: true });
  if (view === 'assistant') render();
  try {
    const res = await fetch('/api/chat/sessions/' + encodeURIComponent(id),
      { headers: { Accept: 'application/json' } });
    if (res.ok) {
      const data = await res.json();
      const recorded = (data.messages || []).map(restoredMessage).filter(Boolean);
      // The record wins unless the cache is ahead of it. The browser only
      // appends, so a longer cache is the record plus turns it never saw.
      if (recorded.length >= cached.length) assistantState.messages = recorded;
      else adoptRecordedIds(assistantState.messages, recorded);
    }
  } catch { /* offline — the cache is what we have, and it is enough to read */ }
  try { localStorage.setItem(CHAT_SESSION_KEY, id); } catch { /* private mode */ }
  persistChat();
  if (view === 'assistant') render();
}

/** Give the transcript on screen the record's row ids, in place. Returns
 *  whether anything was learned.
 *
 *  The two lists are the same conversation seen from two sides: the browser
 *  appends a turn the moment it is spoken, the brain records it once the model
 *  has answered. So they are aligned by walking both in order and matching
 *  role and text — a positional pairing would slide by one at the first turn
 *  the record never took, and the delete button on an error bubble would then
 *  erase somebody else's message. Local turns that match nothing are exactly
 *  those: errors and abandoned partials, which stay id-less. */
function adoptRecordedIds(local, recorded) {
  let i = 0;
  let learned = false;
  for (const row of recorded) {
    while (i < local.length
           && !(local[i].role === row.role && local[i].content === row.content)) i += 1;
    if (i >= local.length) break;
    if (local[i].id !== row.id) { local[i].id = row.id; learned = true; }
    i += 1;
  }
  return learned;
}

/** Re-read the open chat from the record, keeping the panel as it is. Used
 *  after a restore: openChatSession would do it too, but it also closes the
 *  chats panel, and putting three turns back one at a time should not take
 *  three trips into the list. */
async function reloadOpenChat() {
  const id = assistantState.sessionId;
  if (!id) return;
  try {
    const res = await fetch('/api/chat/sessions/' + encodeURIComponent(id),
      { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    assistantState.messages = (data.messages || []).map(restoredMessage).filter(Boolean);
    persistChat();
    if (view === 'assistant') render();
  } catch { /* offline — what is on screen is still the conversation */ }
}

/** Ask the record which rows the turns just taken became. One GET per settled
 *  turn, and what it buys is the delete control appearing on the message you
 *  just sent rather than only after a reload.
 *
 *  Asked only of a chat the record actually holds. A turn that died before the
 *  brain could record it leaves no session row, and requesting it would be a
 *  404 per failed turn in the console — an error log for the ordinary case of
 *  "there is nothing to learn yet". */
export async function learnRecordedIds() {
  const id = assistantState.sessionId;
  if (!id) return;
  if (!assistantState.sessions.some((s) => s.id === id)) return;
  try {
    const res = await fetch('/api/chat/sessions/' + encodeURIComponent(id),
      { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    const recorded = (data.messages || []).map(restoredMessage).filter(Boolean);
    if (!adoptRecordedIds(assistantState.messages, recorded)) return;
    persistChat();
    if (view === 'assistant') render();
  } catch { /* offline — the ids arrive with the next reload */ }
}

/** Refresh the history list. */
export async function refreshChatSessions() {
  try {
    const res = await fetch('/api/chat/sessions', { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.sessions)) {
      assistantState.sessions = data.sessions;
      if (view === 'assistant') render();
    }
  } catch { /* offline — leave the list as it was */ }
}

/** Refresh the deleted-messages list. */
export async function refreshChatTrash() {
  try {
    const res = await fetch('/api/chat/trash', { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.messages)) {
      assistantState.trash = data.messages;
      if (view === 'assistant') render();
    }
  } catch { /* offline — leave the list as it was */ }
}

/** Tell the index the live record changed. Chroma holds chunks derived from
 *  these rows, and `sync` only ever adds, so a hidden turn would go on
 *  answering recall until the brain next booted. The same call covers a
 *  restore, where it is `sync` rather than `prune` that has work to do. */
const reindexChat = () => { fetch('/api/rag/chat/reindex', { method: 'POST' }).catch(() => {}); };

/** Delete one turn. No confirmation, deliberately: nothing is destroyed here,
 *  and the message says where it went. The board's To Trash reads the same. */
export async function deleteChatMessage(msg) {
  if (!Number.isInteger(msg.id)) return;
  try {
    const res = await fetch('/api/chat/messages/' + msg.id, { method: 'DELETE' });
    if (!res.ok) throw new Error(`the server refused (${res.status})`);
  } catch (err) {
    announce(`Delete failed — ${err.message}`);
    return;
  }
  const at = assistantState.messages.indexOf(msg);
  if (at !== -1) assistantState.messages.splice(at, 1);
  persistChat();
  reindexChat();
  render();
  refreshChatTrash();
  refreshChatSessions();
  announce('Message deleted — recoverable under Deleted messages');
}

export async function restoreChatMessage(row) {
  try {
    const res = await fetch('/api/chat/trash/' + row.id + '/restore', { method: 'POST' });
    if (!res.ok) throw new Error(`the server refused (${res.status})`);
  } catch (err) {
    announce(`Restore failed — ${err.message}`);
    return;
  }
  reindexChat();
  await refreshChatTrash();
  refreshChatSessions();
  if (row.sessionId === assistantState.sessionId) await reloadOpenChat();
  announce('Message restored');
}

export async function purgeChatMessage(row) {
  const sure = await ask({
    title: 'Delete permanently?',
    message: `“${short(row.content)}” will be erased from assistant.db and from the `
      + 'assistant’s memory for good. This is the only action that truly deletes it, '
      + 'and it cannot be undone.',
    okLabel: 'Delete permanently',
    danger: true,
  });
  if (!sure) return;
  try {
    const res = await fetch('/api/chat/trash/' + row.id, { method: 'DELETE' });
    if (!res.ok) throw new Error(`the server refused (${res.status})`);
  } catch (err) {
    announce(`Delete failed — ${err.message}`);
    return;
  }
  // The row is gone from the record, so the chunks derived from it have to go
  // too — the prune already ran when it was hidden, but a restore in between
  // would have put them back, and this is the call that settles it either way.
  reindexChat();
  await refreshChatTrash();
  announce('Message deleted permanently');
}

/** Decide which chat the Assistant opens with. Resume the most recent one if
 *  it was live recently; otherwise start a new one, because a thread you left
 *  yesterday is over and continuing it is how a fresh question gets an answer
 *  about an old subject. Runs once. */
let chatOpened = false;
export async function ensureChatSession() {
  if (chatOpened) return;
  chatOpened = true;
  // The pre-sessions transcript key. Dropped rather than migrated: assistant.db
  // already holds every turn it held, and the boot migration has filed them
  // under "Earlier conversations". All this key has that the record does not is
  // error bubbles and abandoned partials — decoration, not the user's thoughts.
  try { localStorage.removeItem(KEY_PREFIX + 'chat'); } catch { /* private mode */ }
  await refreshChatSessions();
  const remembered = (() => {
    try { return localStorage.getItem(CHAT_SESSION_KEY) || ''; } catch { return ''; }
  })();
  // The remembered chat first — it is where the reader was — and only then the
  // newest, for a browser that has never had one.
  const resumable = assistantState.sessions.find((s) => s.id === remembered)
    || assistantState.sessions[0];
  if (resumable && Date.now() - resumable.updatedAt <= RESUME_WITHIN_MS) {
    await openChatSession(resumable.id);
    return;
  }
  startNewChat({ focus: false });
}

// Model choices for the brain, one per capability. Only the text pick has
// an effect today — it rides along on every /api/agent/chat request (the
// brain forwards it to OpenRouter). The omni pick is a stored preference
// for the media-ingestion feature to come. Embedding is deliberately NOT a
// pick: the brain runs exactly one embedder (heydariAI/persian-embeddings),
// so the panel states it instead of offering a dead dropdown. A stale saved
// `embed` key is ignored by the load sweep and dropped on the next persist.
