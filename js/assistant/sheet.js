import { brainModels, probeBrainModels } from './models.js';
import { assistantState, contextWindow, replayable } from './session.js';
import { chatScroll, isPinnedToBottom, setChatScroll, submitChat } from './streaming.js';
import { mountAssistantTools, renderChatDock, renderChatDrift } from './tools.js';
import { busyLabel, renderChatMessage, renderSessionCost } from './transcript.js';
import { cancelRecording, formatElapsed, startRecording, stopRecording, voiceState, voiceSupported } from './voice.js';
import { renderProposals, renderSuggestedEdits } from '../ui/proposals.js';

// The Assistant sheet — the transcript, the composer, and the mic beside it.
// Everything here draws; what it draws lives in session.js.

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

export function renderAssistant() {
  // Asked on entering the view rather than at load, and retried on every entry
  // until it answers — a brain started after the page must be found without a
  // reload. Not awaited: the view renders from the presets and re-renders only
  // if the answer changes a pick.
  if (!brainModels.provider) probeBrainModels();
  const sheet = document.createElement('section');
  sheet.className = 'assistant-sheet';

  const head = document.createElement('div');
  head.className = 'assistant-head';
  const heading = document.createElement('h2');
  heading.textContent = 'Assistant';
  head.appendChild(heading);
  // The tools — search the record, History, New chat, the gear — sit right of
  // the heading, in the sheet they act on. render() rescues the node back to
  // the app header before every wipe, so this move is always of a live node.
  mountAssistantTools(head);
  sheet.appendChild(head);

  // First in the sheet because that is where it is on screen — the margin at
  // the top-left corner — and because tab order should reach the chat you are
  // in before the conversation itself. Absolutely positioned out of the sheet
  // on a wide window; with no margin to sit in it becomes an ordinary row here.
  sheet.appendChild(renderChatDock());

  // Nothing above the transcript but what this conversation cost. Searching
  // the record, the chats, New chat and the settings are all in the app header
  // now, in one row of controls — the sheet holds the conversation and the
  // things that belong to it, and a row of furniture above every reply was
  // exactly what made the assistant's own history hard to find.
  const cost = renderSessionCost();
  if (cost) {
    const toolbar = document.createElement('div');
    toolbar.className = 'assistant-toolbar';
    toolbar.appendChild(cost);
    sheet.appendChild(toolbar);
  }

  // Anything waiting on the user shares one pinned strip directly under the
  // row above. Pinned because it used to scroll away: it is above the
  // transcript, which is right until the transcript is long enough to push the
  // composer past the fold — the reader is then at the bottom typing while the
  // thing needing their decision sits off the top of the screen. Nothing
  // waiting, nothing shown: the strip must not sit there empty.
  if (assistantState.proposals.length || assistantState.edits.length) {
    const waiting = document.createElement('div');
    waiting.className = 'assistant-waiting';
    if (assistantState.proposals.length) waiting.appendChild(renderProposals());
    if (assistantState.edits.length) waiting.appendChild(renderSuggestedEdits());
    sheet.appendChild(waiting);
  }

  const log = document.createElement('div');
  log.className = 'chat-log';
  // Where the reader was, remembered across renders. render() destroys this
  // element, so without carrying the position every repaint would either lose
  // the place or yank the view down to the newest message mid-read.
  log.addEventListener('scroll', () => {
    setChatScroll({ top: log.scrollTop, pinned: isPinnedToBottom(log) });
  });
  if (!assistantState.messages.length) {
    const hint = document.createElement('p');
    hint.className = 'chat-status';
    hint.textContent = 'Ask about your board — research a question, triage the inbox, or find connections.';
    log.appendChild(hint);
  }
  // Where the sent window begins, marked in the transcript itself. Trimming
  // nobody can see is the quiet loss this project refuses; the marker is the
  // difference between "kept but not sent" and "gone". Computed with the same
  // contextWindow the request uses, over the same replayable filter, so what
  // it claims is what the next turn will actually carry.
  const sendable = assistantState.messages.filter(replayable);
  const boundary = contextWindow(sendable);
  const firstCarried = boundary.from > 0 ? sendable[boundary.from] : null;
  for (const msg of assistantState.messages) {
    if (msg === firstCarried) {
      const mark = document.createElement('div');
      mark.className = 'chat-trimmed';
      mark.textContent = 'Messages above stay here but no longer travel with '
        + 'each turn — the assistant can search them if asked.';
      log.appendChild(mark);
    }
    log.appendChild(renderChatMessage(msg));
  }
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

  // Directly above the composer, because that is where the message it is about
  // still sits — and because it is asking about what you are on the point of
  // sending, not about what you have read.
  if (assistantState.drift) sheet.appendChild(renderChatDrift());

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
    // The draft is NOT cleared here: submitChat may hold the turn back to ask
    // whether it belongs in this chat, and the words have to still be there.
    if (text && !assistantState.busy) submitChat(text);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  sheet.appendChild(form);

  // After layout, because scrollHeight is meaningless before it. Following the
  // newest message is the default and stays the default; a reader who scrolled
  // up keeps their place instead of being dragged along.
  requestAnimationFrame(() => {
    log.scrollTop = chatScroll.pinned ? log.scrollHeight : chatScroll.top;
  });
  return sheet;
}

/** The current chat's title, as the button that opens the history — so the
 *  control that moves you between chats is also the label saying where you
 *  are — plus New chat beside it. */
/** How long the panel waits after the pointer leaves it. Long enough to cross
 *  it with a wandering mouse, short enough that a panel you walked away from
 *  is gone when you look back. Cancelled by re-entering or by focus landing
 *  inside, so nobody reading the list with a keyboard is ever rushed. */
