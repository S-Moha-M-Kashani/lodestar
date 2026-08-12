import { assistantModels } from './models.js';
import { CONTEXT_MESSAGES, assistantState, contextWindow, learnRecordedIds, newSessionId, persistChat, refreshChatSessions, replayable } from './session.js';
import { appendLinked, paintChatSoon, sseFrames } from './transcript.js';
import { adoptServerBoard } from '../core/sync.js';
import { announce } from '../ui/dom.js';
import { refreshEdits, refreshProposals } from '../ui/proposals.js';
import { render } from '../ui/render.js';

// Streaming reveal
//
// Two separate problems, and only one of them was on the wire. The brain has
// streamed `token` frames from the start and the Node proxy pipes them, but
// both of the things a user actually sees were wrong:
//
// 1. Every token went through render(), which does `board.innerHTML = ''` and
//    rebuilds the whole view. The recreated .chat-log starts at scrollTop 0
//    and was scrolled back to the bottom on the *next* frame, so sixty times a
//    second the log flashed to the top and snapped down again — the page
//    jumping up and down. Text now updates the existing node in place, and
//    only a tool call (rare) rebuilds anything.
//
// 2. A reasoning model thinks for seconds and then releases its whole answer
//    in under one, so a correctly streamed reply still landed in a single
//    frame. Text is now revealed at a readable rate rather than at whatever
//    rate the network delivered it.

// Drain whatever is buffered over roughly this long. A window rather than a
// fixed characters-per-second: a burst reveals faster than a trickle, which is
// what keeps `done` from arriving while there is still a paragraph to show and
// cutting the reveal off mid-sentence. MIN_CPS keeps a slow trickle moving.

const REVEAL_WINDOW_MS = 900;
const REVEAL_MIN_CPS = 45;
// How close to the bottom still counts as "following along". Bigger than a
// line so the last line landing does not un-pin the log by itself.
const PINNED_SLACK_PX = 28;

// The turn being streamed into: its message object, the DOM node showing it,
// and the text that has arrived but is not yet revealed. One at a time — the
// composer is disabled while a turn is running.
export let streaming = null;
let revealRaf = 0;
let revealAt = 0;
// Survives the log element it describes; see the scroll listener in
// renderAssistant. Starts pinned: a chat opens at its newest message.
export let chatScroll = { top: 0, pinned: true };

/** Opening a chat and scrolling one both replace the whole record, and both
 *  happen from other modules — see the note on the setters in core/state.js. */
export function setChatScroll(next) {
  chatScroll = next;
}

export const lastAssistantBubble = () => {
  const bubbles = document.querySelectorAll('.chat-log .chat-msg.assistant');
  return bubbles.length ? bubbles[bubbles.length - 1] : null;
};

export const isPinnedToBottom = (log) =>
  log.scrollHeight - log.scrollTop - log.clientHeight <= PINNED_SLACK_PX;

function beginStreaming(turn) {
  streaming = { turn, buffer: '' };
  render();                        // once, to create the bubble to write into
  streaming.node = lastAssistantBubble();
}

function endStreaming() {
  if (revealRaf) cancelAnimationFrame(revealRaf);
  revealRaf = 0;
  streaming = null;
}

/** Take arrived text. Revealed over the next frames, not on this one. */
function pushToken(text) {
  if (!streaming) { return; }
  streaming.buffer += text;
  if (revealRaf) return;
  revealAt = performance.now();
  revealRaf = requestAnimationFrame(revealStep);
}

function revealStep(now) {
  revealRaf = 0;
  if (!streaming) return;
  const elapsed = Math.max(0, now - revealAt);
  revealAt = now;
  const cps = Math.max(REVEAL_MIN_CPS,
    streaming.buffer.length / (REVEAL_WINDOW_MS / 1000));
  // At least one character a frame, so the reveal can never stall while text
  // is waiting.
  const take = Math.max(1, Math.round((cps * elapsed) / 1000));
  streaming.turn.content += streaming.buffer.slice(0, take);
  streaming.buffer = streaming.buffer.slice(take);
  paintStreamedText();
  if (streaming.buffer) revealRaf = requestAnimationFrame(revealStep);
}

/** Everything buffered is now shown. Resolves when the reveal has caught up,
 *  so a turn settles on the finished text without snapping past what the
 *  reader had not seen yet. */
function drainReveal() {
  return new Promise((resolve) => {
    const wait = () => {
      if (!streaming || !streaming.buffer) return resolve();
      requestAnimationFrame(wait);
    };
    wait();
  });
}

/** The revealed text, written into the bubble that is already on screen.
 *
 *  Never through render(): that is the whole point. The scroll position is
 *  read *before* the text changes and only restored if the reader was already
 *  at the bottom — an answer arriving must not yank the view away from
 *  someone who scrolled up to re-read the question. */
function paintStreamedText() {
  const node = streaming && streaming.node;
  if (!node || !node.isConnected) return;   // view switched away mid-turn
  const body = node.querySelector('.chat-text');
  if (!body) return;
  const log = node.closest('.chat-log');
  const follow = log ? isPinnedToBottom(log) : false;
  body.textContent = '';
  appendLinked(body, streaming.turn.content);
  if (follow && log) log.scrollTop = log.scrollHeight;
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

/** Ask the brain whether this message belongs to this chat. Returns the
 *  verdict when it does not, or null — which is also what every failure
 *  returns. Fail open, deliberately: a detector that can block a turn is
 *  worse than no detector, and this one runs in front of every message.
 *
 *  Skipped on the first message of a chat, where there is nothing to drift
 *  from — which is also the one case a request could only answer "no". */
async function checkTopicDrift(text) {
  if (assistantState.driftDismissed) return null;
  const recent = assistantState.messages
    .filter((m) => m.role === 'user' && replayable(m))
    .slice(-CONTEXT_MESSAGES).map((m) => m.content);
  if (!recent.length) return null;
  try {
    const res = await fetch('/api/agent/topic-check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recent, text }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data && data.changed ? data : null;
  } catch { return null; }
}

/** What Send does. Either the turn goes, or it is held back and the reader is
 *  offered a new chat — holding it back is what makes the offer free: nothing
 *  has been spent, and there is no answer sitting in the wrong chat. */
export async function submitChat(text) {
  if (assistantState.busy) return;
  const verdict = await checkTopicDrift(text);
  if (verdict) {
    assistantState.drift = { text, reason: verdict.reason };
    render();
    return;
  }
  sendChat(text);
}

export async function sendChat(text) {
  // Cleared here rather than by the submit handler: while a drift nudge is on
  // screen the message has not been sent, and a composer emptied of a question
  // nobody answered is the loss this project does not do.
  assistantState.draft = '';
  assistantState.drift = null;
  // A chat exists the moment it is talked into. Nothing was written while it
  // was empty, so this is where a brand-new chat gets its id.
  if (!assistantState.sessionId) assistantState.sessionId = newSessionId();
  assistantState.messages.push({ role: 'user', content: text });
  // Sending is joining the bottom of the conversation: whatever the reader was
  // scrolled to, they want to see what they just said and what answers it.
  setChatScroll({ top: 0, pinned: true });
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
    const carried = contextWindow(assistantState.messages.filter(replayable))
      .messages.map(({ role, content }) => ({ role, content }));
    const res = await fetch('/api/agent/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: carried,
        // Which chat this turn belongs to. The brain records the turn under it
        // and scopes recall_chat away from it — the only reason the brain needs
        // to know, since the browser does its own windowing.
        session_id: assistantState.sessionId,
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
    // One render to put the bubble on screen; from here text is written into
    // it in place. Rebuilding the view per token is what made the page jump.
    beginStreaming(turn);
    let data = null;
    let failed = '';
    for await (const { name, data: payload } of sseFrames(res)) {
      // A tool starting or answering changes the shape of the turn, not just
      // its text, so these repaint — a few times a turn, not hundreds.
      if (name === 'calling') { turn.running.push(payload); paintChatSoon(); }
      // Steps answer in request order, so the oldest running call is this
      // one. See _steps_from in the brain for why that holds.
      else if (name === 'step') {
        turn.steps.push(payload); turn.running.shift(); paintChatSoon();
      } else if (name === 'token') pushToken(payload.text);
      else if (name === 'error') failed = payload.message;
      else if (name === 'done') data = payload;
    }
    // `done` normally arrives while text is still being revealed — the model
    // finishes writing long before a reader finishes reading. Let the reveal
    // catch up first, so the turn does not settle by jumping to the end.
    await drainReveal();
    endStreaming();
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
    // Absent unless the brain actually knew the price — see pricing.py. Left
    // undefined rather than set to 0, so an unpriced turn is missing from the
    // session total instead of quietly reported as free.
    if (typeof data.cost === 'number' && Number.isFinite(data.cost)) {
      turn.cost = data.cost;
    }
    // Two distinct outcomes: an edit changed the board, a proposal did not.
    if (data.mutated) await adoptServerBoard();
    // One flag for both kinds of waiting suggestion: a card to accept, or a
    // change to review. Neither has altered the board, so no adoption here.
    if (data.proposed) { await refreshProposals(); await refreshEdits(); }
    announce(data.proposed ? 'Assistant has something waiting for your approval'
      : 'Assistant replied');
  } catch {
    // Before anything else: a dead stream must not leave a reveal loop running
    // against a turn that will never finish.
    endStreaming();
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
  // The brain has just recorded the turn, so the chat's title, its position in
  // the list, and its message count have all changed. A first turn is also
  // what created the chat row at all.
  // And the turn just spoken now has a row in the record — which is what a
  // per-message delete acts on, so it is asked for here rather than at the
  // next reload. After the list, not beside it: the list is what says whether
  // this chat reached the record at all.
  refreshChatSessions().then(learnRecordedIds);
}
