import { assistantState, deleteChatMessage } from './session.js';
import { lastAssistantBubble, streaming } from './streaming.js';
import { openDialog } from '../ui/edit-dialog.js';
import { render } from '../ui/render.js';

// Drawing one turn: the bubble, the sources under it, the tool steps behind
// it, and what it cost. Read-only — nothing here talks to the brain.

export function renderChatMessage(msg) {
  const el = document.createElement('div');
  el.className = `chat-msg ${msg.role}${msg.error ? ' error' : ''}`;
  // The text is its own node now: the steps below are elements, and setting
  // textContent on the parent would wipe them.
  const body = document.createElement('div');
  body.className = 'chat-text';
  appendLinked(body, msg.content);
  el.appendChild(body);

  // One turn, deletable on its own. Only for a turn the record knows: without
  // a row id there is nothing to delete, and a control that quietly removed
  // the message from the screen alone would be the opposite of this feature.
  // It fades in on hover and on focus, so it is reachable by keyboard and is
  // not a delete button standing over every sentence of the conversation.
  if (Number.isInteger(msg.id)) {
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-msg-delete';
    remove.textContent = '×';
    remove.title = 'Delete this message';
    remove.setAttribute('aria-label', 'Delete this message');
    remove.addEventListener('click', () => deleteChatMessage(msg));
    el.appendChild(remove);
  }

  // Everything that is *about* the answer rather than part of it goes in one
  // ruled strip beneath it — what it cited, what it cost, which tools ran —
  // folded behind a line that says what is in there. Most turns are read for
  // the answer alone, and a source list, a row of tool chips and a token
  // receipt under every one of them is a wall of furniture.
  const done = msg.steps || [];
  const running = msg.running || [];
  const sources = sourcesOf(done);
  if (!done.length && !running.length && !sources.length && !msg.usage) return el;

  const meta = document.createElement('details');
  meta.className = 'chat-meta';
  // Open while the turn is still working: which tool is running right now is
  // progress, not an afterthought. It folds itself away once the turn lands.
  meta.open = running.length > 0;
  const summary = document.createElement('summary');
  summary.className = 'chat-meta-summary';
  summary.textContent = metaSummary(sources.length, done.length + running.length, msg.usage);
  meta.appendChild(summary);

  const evidence = document.createElement('div');
  evidence.className = 'chat-meta-body';
  if (sources.length) evidence.appendChild(renderChatSources(sources));
  if (msg.usage) evidence.appendChild(renderChatUsage(msg.usage, msg.cost));
  if (done.length || running.length) {
    const steps = document.createElement('div');
    steps.className = 'chat-steps';
    for (const step of done) steps.appendChild(renderChatStep(step, false));
    for (const call of running) steps.appendChild(renderChatStep(call, true));
    evidence.appendChild(steps);
  }
  meta.appendChild(evidence);
  el.appendChild(meta);
  return el;
}

// Three decimals, always — a tenth of a cent is the resolution a chat turn
// lives at, and a fixed width keeps the readout from twitching as the total
// grows. Rounded only here: the brain sends the figure unrounded so a session
// total is the sum of real numbers rather than a sum of rounded ones.
const money = (usd) => `${Number(usd).toFixed(3)}$`;

/** One turn's price, where three decimals is often not enough resolution.
 *
 *  A turn on a cheap model costs around $0.0002, which `money` renders as
 *  "0.000$" — indistinguishable from the local model that really was free. So a
 *  paid turn below the display resolution says so instead. A genuine zero still
 *  prints 0.000$, because that one is a fact rather than a rounding. */
const moneyTurn = (usd) => (usd > 0 && usd < 0.0005 ? '<0.001$' : money(usd));

/** What this conversation has cost so far, or nothing if nothing is known.
 *
 *  Summed from the turns rather than kept as a running counter, so it survives
 *  a reload with the transcript and can never drift from the turns it claims to
 *  add up. Turns the brain could not price are simply absent from the sum; if
 *  none of them carried a price there is no readout at all, because "0.000$"
 *  would be a claim about money nobody measured. */
export function renderSessionCost() {
  const priced = assistantState.messages
    .map((m) => m.cost)
    .filter((c) => typeof c === 'number' && Number.isFinite(c));
  if (!priced.length) return null;
  const el = document.createElement('p');
  el.className = 'assistant-cost';
  el.textContent = `current session cost = ${money(priced.reduce((a, b) => a + b, 0))}`;
  el.title = `Summed over ${priced.length} priced turn${priced.length === 1 ? '' : 's'}`
    + ' at the current model’s published rates';
  return el;
}

// What the folded strip says about itself. Counts, plus what the turn cost —
// a total is worth a glance, the in/out split is not, so only the total is
// named out here and the breakdown waits inside.
function metaSummary(nSources, nTools, usage) {
  const parts = [];
  if (nSources) parts.push(`${nSources} source${nSources === 1 ? '' : 's'}`);
  if (nTools) parts.push(`${nTools} tool${nTools === 1 ? '' : 's'}`);
  if (usage) parts.push(`${Number(usage.total_tokens || 0).toLocaleString()} tokens`);
  return parts.join(' · ');
}

// How the turn's total split, and what it cost. This used to be tokens only,
// on the grounds that a per-model price is a number the app cannot verify and
// a stale one is worse than none — which was right about a hardcoded table and
// is why the figure now comes from the provider's own live catalogue instead
// (pricing.py). The price is still absent, never zero, whenever the brain could
// not look it up: unpriced and free are different facts. Absent likewise when
// the model reported no usage — see _usage_from. The token total is on the
// folded line above, so it is not printed twice.
function renderChatUsage(usage, cost) {
  const line = document.createElement('p');
  line.className = 'chat-usage';
  const n = (v) => Number(v || 0).toLocaleString();
  const parts = [`${n(usage.input_tokens)} in · ${n(usage.output_tokens)} out`];
  if (typeof cost === 'number') parts.push(moneyTurn(cost));
  line.textContent = parts.join(' · ');
  return line;
}

// Deliberately anchored on the scheme, so nothing but http(s) can become an
// href — a linkifier that accepted any "word:" would happily build a
// javascript: link out of a web-search snippet. The last character may not be
// punctuation, or a url ending a sentence swallows the full stop.
const URL_RE = /\bhttps?:\/\/[^\s<>()[\]{}"']*[^\s<>()[\]{}"'.,;:!?]/g;

export function appendLinked(parent, text) {
  let at = 0;
  for (const match of String(text || '').matchAll(URL_RE)) {
    if (match.index > at) {
      parent.appendChild(document.createTextNode(text.slice(at, match.index)));
    }
    const link = document.createElement('a');
    link.className = 'chat-link';
    link.href = match[0];
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = match[0];
    parent.appendChild(link);
    at = match.index + match[0].length;
  }
  parent.appendChild(document.createTextNode(String(text || '').slice(at)));
}

// Where an answer came from. Each tool answers in its own shape, so the
// reader lives next to the tool it understands rather than one function
// guessing from the payload — a misread shape would cite the wrong thing,
// which is worse than citing nothing.
const SOURCE_READERS = {
  web_search: (rows) => rows.map((row) => ({
    label: row.title || row.url, url: row.url, note: row.snippet })),
  find_related: (rows) => rows.map((row) => ({
    label: (row.card && row.card.title) || '', cardId: row.card && row.card.id,
    note: row.card && row.card.columnId })),
  // No link: a recalled snippet is the transcript itself, and there is
  // nowhere to send the user that shows more of it than this does.
  recall_chat: (rows) => rows.map((row) => ({ label: row.text, note: '' })),
};

function sourcesOf(steps) {
  const found = [];
  const seen = new Set();
  for (const step of steps) {
    const read = SOURCE_READERS[step.tool];
    if (!read || !Array.isArray(step.result)) continue;
    for (const source of read(step.result)) {
      // Two searches often surface the same page; listing it twice would
      // read as two independent sources agreeing.
      const key = source.url || source.cardId || source.label;
      if (!source.label || seen.has(key)) continue;
      seen.add(key);
      found.push(source);
    }
  }
  return found;
}

function renderChatSources(sources) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-sources';
  const heading = document.createElement('p');
  heading.className = 'chat-sources-label';
  heading.textContent = sources.length === 1 ? '1 source' : `${sources.length} sources`;
  wrap.appendChild(heading);
  const list = document.createElement('ol');
  list.className = 'chat-source-list';
  for (const source of sources) list.appendChild(renderChatSource(source));
  wrap.appendChild(list);
  return wrap;
}

function renderChatSource(source) {
  const item = document.createElement('li');
  item.className = 'chat-source';
  if (source.url) {
    const link = document.createElement('a');
    link.className = 'chat-source-link';
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = source.label;
    item.appendChild(link);
  } else if (source.cardId) {
    // A button, not a link: it opens the card's editor in place rather than
    // navigating, so the user does not lose the conversation to read it.
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'chat-source-link chat-source-card';
    open.textContent = source.label;
    open.addEventListener('click', () => openDialog(source.cardId));
    item.appendChild(open);
  } else {
    const said = document.createElement('span');
    said.className = 'chat-source-said';
    said.textContent = source.label;
    item.appendChild(said);
  }
  if (source.note) {
    const note = document.createElement('span');
    note.className = 'chat-source-note';
    note.textContent = source.note;
    item.appendChild(note);
  }
  return item;
}

// A tool call, collapsed to its name and openable for the evidence. Still
// `.chat-step` — the class is test-stable API, so this adds to it rather than
// renaming it.
function renderChatStep(step, running) {
  const box = document.createElement('details');
  box.className = `chat-step${running ? ' chat-step-running' : ''}`;
  const name = document.createElement('summary');
  name.className = 'chat-step-name';
  name.textContent = running ? `${step.tool}…` : step.tool;
  box.appendChild(name);
  box.appendChild(chatStepField('arguments', step.arguments));
  // A running call has no result yet, and an empty "result" row would read as
  // a tool that answered with nothing.
  if (!running) box.appendChild(chatStepField('result', step.result));
  return box;
}

function chatStepField(label, value) {
  const row = document.createElement('div');
  row.className = 'chat-step-field';
  const key = document.createElement('span');
  key.className = 'chat-step-label';
  key.textContent = label;
  const val = document.createElement('pre');
  val.className = 'chat-step-value';
  val.textContent = typeof value === 'string' ? value
    : value === undefined ? '—' : JSON.stringify(value, null, 2);
  row.appendChild(key);
  row.appendChild(val);
  return row;
}

// What the assistant is doing right now, from the last event that arrived.
// A label that names the running tool is the difference between waiting and
// wondering whether it has hung.
export function busyLabel() {
  const last = assistantState.messages[assistantState.messages.length - 1];
  if (!last || last.role !== 'assistant') return 'Thinking…';
  const running = last.running || [];
  if (running.length) return `Running ${running[running.length - 1].tool}…`;
  return last.content ? 'Writing…' : 'Thinking…';
}

// Server-sent events over fetch, because EventSource is GET-only and the chat
// turn is a POST. A frame can be split across reads, so nothing is parsed
// until its blank-line terminator is in the buffer.
export async function* sseFrames(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = 'message';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event: ')) name = line.slice(7);
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (data) yield { name, data: JSON.parse(data) };
    }
  }
}

// One repaint per frame at most. Used for the *structural* changes a turn
// makes — a tool starting, a tool answering — which happen a handful of times
// and are worth a full render. Arriving text is not one of them: see
// `paintStreamedText`.
let chatPaint = 0;
export function paintChatSoon() {
  if (chatPaint) return;
  chatPaint = requestAnimationFrame(() => {
    chatPaint = 0;
    render();
    // render() destroyed the bubble the stream was writing into, so the
    // reveal has to be told where its text lives now.
    if (streaming) streaming.node = lastAssistantBubble();
  });
}
