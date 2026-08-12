// tests/context.test.js
//
// How much conversation one turn carries. Three numbers live in two languages
// and used to disagree:
//
//   CHAT_KEEP      (app.js)   200 messages restored into the transcript
//   CONTEXT_*      (app.js)   what is actually sent to the model
//   MAX_MESSAGES / MAX_CHARS  (brain/server.py) the server's refusal
//
// The bug this file exists to prevent: CHAT_KEEP was 200 and MAX_MESSAGES 80,
// and the browser sent *everything* it had restored. So a chat between 81 and
// 200 messages reloaded, rendered perfectly, and then 413'd on every send — a
// visibly alive conversation you could not talk into. Nothing was lost, and the
// refusal named the right fix, but the two numbers were each correct in
// isolation and wrong together, which is exactly what no runtime test catches.
//
// The reconciliation is that they now answer different questions:
//   - CHAT_KEEP is the READER's transcript. Bigger is better; it costs nothing.
//   - CONTEXT_* is the MODEL's window. Smaller is cheaper and faster.
//   - MAX_* is a backstop for callers that are not the browser, and stays a
//     refusal rather than a silent trim.
//
// So the invariant is an ordering, not an equality: window < server cap, and
// window <= what the reader keeps. Read out of the real source files, so an edit
// to any one of the three fails here instead of in the user's chat.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (...p) => readFileSync(join(ROOT, ...p), 'utf8');

/** A `const NAME = 12_345;` / `NAME = 12_345` numeric literal, whichever
 *  language it is written in. Underscores stripped: both JS and Python allow
 *  them as digit separators and both files use them for readability. */
function constant(source, name) {
  const m = source.match(new RegExp(`\\b${name}\\s*=\\s*([\\d_]+)`));
  assert.ok(m, `could not find ${name} in the source`);
  return Number(m[1].replaceAll('_', ''));
}

// The three browser-side numbers all live in the chat-session module; the
// frontend was one 6,400-line app.js when this file was written.
const app = read('js', 'assistant', 'session.js');
const brain = read('brain', 'src', 'lodestar_brain', 'server.py');

// This is a configuration invariant.
test('the window the browser sends fits inside what the brain accepts', () => {
  const messages = constant(app, 'CONTEXT_MESSAGES');
  const chars = constant(app, 'CONTEXT_CHARS');
  const maxMessages = constant(brain, 'MAX_MESSAGES');
  const maxChars = constant(brain, 'MAX_CHARS');

  // Strictly under, not equal. A budget sitting exactly on the limit refuses on
  // the turn that reaches it — the failure would arrive as one dead conversation
  // rather than as a test. (It used to also have to leave room for the framing
  // message the client prepended outside the budget; that message is gone —
  // see the pinning test below.)
  assert.ok(messages < maxMessages,
    `the browser sends up to ${messages} messages but the brain refuses past ${maxMessages}`);
  assert.ok(chars < maxChars,
    `the browser sends up to ${chars} chars but the brain refuses past ${maxChars}`);
});

// This is a configuration invariant.
test('the reader keeps more of the conversation than the model is sent', () => {
  // The point of the split. If these were equal, trimming the window would also
  // be throwing away transcript, and the whole design would collapse back into
  // "send everything you kept".
  assert.ok(constant(app, 'CHAT_KEEP') > constant(app, 'CONTEXT_MESSAGES'),
    'CHAT_KEEP is the transcript, CONTEXT_MESSAGES is the request — the '
    + 'transcript must be the larger of the two or there is nothing to recall');
});

// This is a configuration invariant.
test('no message is pinned outside the context budget', () => {
  // The bug this test exists to prevent, and it shipped: contextWindow ended
  // with `const framing = history.find((m) => m.role === 'user')` and prepended
  // that message to every request, deliberately outside the char budget, as
  // "framing". With one endless transcript the framing message is not the
  // subject of this conversation — it is the subject of the FIRST conversation
  // this board ever had, stapled to the top of every request for the life of
  // the install. Saying "hi" got an answer about last month.
  //
  // Sessions replace it: the window is one conversation, so the framing is the
  // conversation. Asserted on the source rather than by calling contextWindow,
  // because what must not come back is a *shape* of code, not a return value —
  // the same reason the brain's refusal is matched by regex above.
  // The mechanism, not the word for it: what must not come back is the lookup
  // that found the message to pin. Asserting on the prose would only forbid
  // *describing* the bug, which is the opposite of useful.
  assert.doesNotMatch(app, /history\.find\(/,
    'contextWindow must not reach into the history for a message to prepend — '
    + 'a pinned first message is the original bug');
  assert.match(app, /history\.slice\(from\)/,
    'the carried window must be a contiguous tail of this session, so what the '
    + 'transcript marks as "no longer travelling" is exactly what is left out');
});

// This is a configuration invariant.
test('the resume gap is a named constant with a plausible value', () => {
  // Opening the Assistant resumes the last chat unless the gap was long. The
  // number is a judgement call, not a measurement, so what is pinned here is
  // only that it exists and is in the range where it could be one: a gap of
  // seconds would start a new chat between two sentences, and a gap of days
  // would never fire and quietly make the feature a lie.
  const gap = constant(app, 'RESUME_WITHIN_MS');
  assert.ok(gap >= 60_000 && gap <= 24 * 60 * 60 * 1000,
    `RESUME_WITHIN_MS is ${gap}ms — expected between a minute and a day`);
});

// This is a configuration invariant.
test('the brain still refuses rather than trimming', () => {
  // The server-side cap is a backstop for non-browser callers, and it must stay
  // a refusal: a brain that silently dropped the oldest messages would make the
  // client's visible, marked trimming indistinguishable from invisible loss.
  assert.match(brain, /raise HTTPException\(413/,
    'MAX_MESSAGES/MAX_CHARS must refuse, never trim');
});
