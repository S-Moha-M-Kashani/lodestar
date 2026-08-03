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

const app = read('app.js');
const brain = read('brain', 'src', 'lodestar_brain', 'server.py');

// This is a configuration invariant.
test('the window the browser sends fits inside what the brain accepts', () => {
  const messages = constant(app, 'CONTEXT_MESSAGES');
  const chars = constant(app, 'CONTEXT_CHARS');
  const maxMessages = constant(brain, 'MAX_MESSAGES');
  const maxChars = constant(brain, 'MAX_CHARS');

  // Strictly under, not equal. The client sends the window plus the framing
  // message, and a budget sitting exactly on the limit refuses on the turn that
  // reaches it — the failure would arrive as one dead conversation rather than
  // as a test.
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
test('the brain still refuses rather than trimming', () => {
  // The server-side cap is a backstop for non-browser callers, and it must stay
  // a refusal: a brain that silently dropped the oldest messages would make the
  // client's visible, marked trimming indistinguishable from invisible loss.
  assert.match(brain, /raise HTTPException\(413/,
    'MAX_MESSAGES/MAX_CHARS must refuse, never trim');
});
