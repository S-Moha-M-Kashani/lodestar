import { renderModelPicker } from './models.js';
import { assistantState, contextWindow, replayable } from './session.js';
import { chatScroll, isPinnedToBottom, sendChat, setChatScroll, submitChat } from './streaming.js';
import { renderChatSuggestions } from './suggestions.js';
import { renderChatDrift } from './tools.js';
import { busyLabel, renderChatMessage } from './transcript.js';
import { cancelRecording, formatElapsed, startRecording, stopRecording, voiceState, voiceSupported } from './voice.js';
import { activeBoard, activeBoardId } from '../core/boards.js';

// The furniture around a conversation, drawn once for both shells.
//
// The Assistant has two of them now — the full view (sheet.js) and the corner
// widget (widget.js) — and everything from the transcript down to the send
// button is identical in each. One builder is what makes that true by
// construction: two files kept in step would drift the first time one of them
// was the convenient place to fix something. The `shell` argument decides
// nothing behavioural; it is stamped on the nodes so a test, or a person with
// the inspector open, can say which shell they are looking at.
//
// Only one shell is ever in the document at a time, which is not a nicety:
// `#chat-input` is an id, and two of them is invalid HTML that would break
// every selector this project's e2e suite is written against. render() is what
// enforces it.

/** The message input, the footer, and what Send means. */
function renderComposer(shell) {
  const form = document.createElement('form');
  form.className = 'chat-composer';
  form.dataset.shell = shell;

  const input = document.createElement('textarea');
  input.id = 'chat-input';
  input.placeholder = 'Message the assistant…';
  input.disabled = assistantState.busy;
  input.value = assistantState.draft;
  // The draft lives in state, not in the DOM: render() rebuilds this textarea,
  // and both shells read the same value — which is what carries half-typed
  // words across an expand or a collapse.
  input.addEventListener('input', () => { assistantState.draft = input.value; });
  form.appendChild(input);

  const footer = document.createElement('div');
  footer.className = 'composer-footer';

  // A label, not a control. The board switch is in the app header and this
  // project's rule is that what sits in the margin as a label stays one — but
  // which board a question is answered against is worth saying where the
  // question is typed, because the answer depends on it entirely.
  const chip = document.createElement('span');
  chip.className = 'composer-context';
  const board = activeBoard();
  chip.textContent = board ? board.name : activeBoardId;
  chip.title = 'Answered against this board';
  footer.appendChild(chip);

  // The model pick, out of the ⚙ drawer and next to the send button. Same
  // `assistantModels` and the same save the drawer writes, so the two can
  // never show different answers to "which model is answering".
  footer.appendChild(renderModelPicker({ busy: assistantState.busy }));

  const actions = document.createElement('div');
  actions.className = 'composer-actions';
  if (voiceSupported()) actions.appendChild(renderMicButton());
  const send = document.createElement('button');
  send.type = 'submit';
  send.id = 'chat-send';
  send.className = 'btn primary chat-send';
  send.textContent = '➤';
  send.setAttribute('aria-label', 'Send message');
  send.title = 'Send (Enter)';
  send.disabled = assistantState.busy;
  actions.appendChild(send);
  footer.appendChild(actions);
  form.appendChild(footer);

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
  return form;
}

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

/** The transcript itself: the openers on an empty chat, the trim marker, and
 *  one node per turn. */
function renderChatLog(shell) {
  const log = document.createElement('div');
  log.className = 'chat-log';
  log.dataset.shell = shell;
  // Where the reader was, remembered across renders. render() destroys this
  // element, so without carrying the position every repaint would either lose
  // the place or yank the view down to the newest message mid-read.
  log.addEventListener('scroll', () => {
    setChatScroll({ top: log.scrollTop, pinned: isPinnedToBottom(log) });
  });
  // Nothing said yet: the hint, and one opener per thing the assistant can
  // actually do. Keyed on an empty transcript rather than on a new-chat event,
  // which is what makes New chat, a first load and reopening a chat nobody ever
  // spoke into all offer them, with nothing to raise or listen for.
  if (!assistantState.messages.length) {
    const hint = document.createElement('p');
    hint.className = 'chat-status';
    hint.textContent = 'Ask about your board — or start with one of these.';
    log.appendChild(hint);
    log.appendChild(renderChatSuggestions());
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
  return log;
}

/** Everything from the transcript down to the send button, appended to
 *  whichever shell is drawing. Returns the log, because both shells restore
 *  the reader's scroll position into it after layout. */
export function renderChatChrome(parent, shell) {
  const log = renderChatLog(shell);
  parent.appendChild(log);

  const status = document.createElement('div');
  status.className = 'chat-status';
  if (assistantState.busy) status.textContent = busyLabel();
  else if (voiceState.phase === 'transcribing') status.textContent = 'Transcribing…';
  else if (voiceState.phase === 'recording') status.textContent = 'Listening…';
  parent.appendChild(status);

  if (voiceState.error) {
    const problem = document.createElement('p');
    problem.className = 'chat-voice-error';
    problem.setAttribute('role', 'alert');
    problem.textContent = voiceState.error;
    parent.appendChild(problem);
  }

  if (voiceState.phase === 'recording') parent.appendChild(renderRecordingBar());

  // Directly above the composer, because that is where the message it is about
  // still sits — and because it is asking about what you are on the point of
  // sending, not about what you have read.
  if (assistantState.drift) parent.appendChild(renderChatDrift());

  parent.appendChild(renderComposer(shell));
  return log;
}

/** Put the reader back where they were, after layout — scrollHeight is
 *  meaningless before it. Following the newest message is the default and
 *  stays the default; a reader who scrolled up keeps their place instead of
 *  being dragged along. */
export function restoreChatScroll(log) {
  requestAnimationFrame(() => {
    log.scrollTop = chatScroll.pinned ? log.scrollHeight : chatScroll.top;
  });
}

/** Re-send the message a failed turn was carrying.
 *
 *  `sendChat`, deliberately, not `submitChat`: the drift nudge has already been
 *  answered for this message — either it was never raised, or the user chose
 *  "keep this one" — and asking again would be asking twice about one send.
 *
 *  The failed turn is taken out of the transcript first, and the user's message
 *  with it: `sendChat` appends that message itself, so leaving it would file a
 *  second copy of one question. What the reader ends up with is what they would
 *  have had if the turn had worked the first time — their message, then the
 *  answer — and a retry that fails again leaves one banner rather than a column
 *  of them. */
export function retryTurn(msg) {
  if (assistantState.busy) return;
  const text = msg.retry;
  if (!text) return;
  const at = assistantState.messages.indexOf(msg);
  if (at === -1) return;
  assistantState.messages.splice(at, 1);
  // Back past whatever the dead stream left behind — an abandoned partial is
  // kept on purpose, because text that arrived was paid for — to the message
  // that provoked the failure.
  let i = at - 1;
  while (i >= 0 && assistantState.messages[i].partial) i -= 1;
  const asked = assistantState.messages[i];
  if (asked && asked.role === 'user' && asked.content === text) {
    assistantState.messages.splice(i, 1);
  }
  sendChat(text);
}
