import { retryTurn } from './chrome.js';
import { assistantState, deleteChatMessage } from './session.js';
import { lastAssistantBubble, streaming } from './streaming.js';
import { announce } from '../ui/dom.js';
import { openDialog } from '../ui/edit-dialog.js';
import { render } from '../ui/render.js';

// Drawing one turn: the bubble, the sources under it, the tool steps behind
// it, and what it cost. Read-only — nothing here talks to the brain.

export function renderChatMessage(msg) {
  // A failed turn is not a reply the assistant gave, so it is not drawn as
  // one. It keeps the `.chat-msg.assistant.error` classes — those are
  // test-stable API and it is still the turn's place in the transcript — and
  // adds what a banner needs: the whole error, and a way to try again.
  if (msg.error) return renderErrorBanner(msg);
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
  const worked = workedRow(msg);
  const hasEvidence = done.length || running.length || sources.length || msg.usage;
  if (!hasEvidence && !worked) return el;
  // A turn with nothing behind it still says what it cost in time and tools —
  // but it says it as a line, not as a fold: a control that opens onto nothing
  // is a control that lies about having something.
  if (!hasEvidence) {
    const line = document.createElement('p');
    line.className = 'chat-meta-summary chat-worked-line';
    line.appendChild(worked);
    el.appendChild(line);
    if (assistantState.busy) tickWorked();
    return el;
  }

  const meta = document.createElement('details');
  meta.className = 'chat-meta';
  // Open while the turn is still working: which tool is running right now is
  // progress, not an afterthought. It folds itself away once the turn lands.
  meta.open = running.length > 0;
  const summary = document.createElement('summary');
  summary.className = 'chat-meta-summary';
  // What the turn took, first — it is the one thing every turn can say, and it
  // is what replaced the anonymous "Thinking…" that used to stand here. The
  // counts and the token total follow it; nothing that was shown is hidden.
  if (worked) {
    summary.appendChild(worked);
    if (assistantState.busy) tickWorked();
  }
  // The tool count is passed only when `worked` is absent. That row already
  // ends in it — "Worked for 17.2s · 2 tools" — and handing the same number to
  // metaSummary as well printed it twice in one strip: "Worked for 17.2s · 2
  // tools · 1 source · 2 tools · 14,090 tokens". It went unseen because the
  // e2e checks read `.chat-worked`, which is correct on its own; the duplicate
  // only exists in the two spans joined. An untimed turn — one restored from
  // before the row existed, or imported — has no `worked`, and there the count
  // is the only place the tools are named, so it still goes here.
  const rest = metaSummary(sources.length, worked ? 0 : done.length + running.length,
    msg.usage);
  if (rest) {
    const tail = document.createElement('span');
    tail.className = 'chat-meta-rest';
    tail.textContent = worked ? ` \u00b7 ${rest}` : rest;
    summary.appendChild(tail);
  }
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

// How long a turn took, and how many tools it ran. One row, and the same words
// in flight and settled — a turn that is still working counts up instead of
// claiming a total it does not have yet.
//
// This replaced an anonymous busy label. "Thinking…" says nothing a spinner
// does not; thirteen seconds and two tool calls is the difference between a
// slow model and a hung one, and it is the first thing anybody asks when a
// reply takes a while.
const duration = (ms) => (ms < 60_000
  ? `${(ms / 1000).toFixed(1)}s`
  : `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`);

/** The row's text for one turn, or '' when the turn was never timed — a
 *  transcript restored from before this existed, or imported from a file. */
function workedText(msg) {
  const tools = (msg.steps || []).length + (msg.running || []).length;
  if (Number.isFinite(msg.elapsedMs)) {
    return `Worked for ${duration(msg.elapsedMs)} \u00b7 ${tools} tool${tools === 1 ? '' : 's'}`;
  }
  if (Number.isFinite(msg.startedAt)) {
    return `Working for ${duration(Date.now() - msg.startedAt)}`;
  }
  return '';
}

function workedRow(msg) {
  const text = workedText(msg);
  if (!text) return null;
  const row = document.createElement('span');
  row.className = 'chat-worked';
  row.textContent = text;
  return row;
}

// The live half of the row, ticked once a second — by writing the text, never
// by rendering. render() destroys the transcript, the focus and the scroll
// position; this module already learned that lesson when tokens were painted
// one render at a time. The timer reads the state on every tick rather than
// closing over a message or an element, because both are replaced under it by
// the repaints a streaming turn does make.
let workedTimer = 0;
function tickWorked() {
  if (workedTimer) return;
  workedTimer = setInterval(() => {
    const last = assistantState.messages[assistantState.messages.length - 1];
    const row = document.querySelector('.chat-log .chat-msg.assistant:last-of-type .chat-worked');
    if (!assistantState.busy || !last || !row) {
      clearInterval(workedTimer);
      workedTimer = 0;
      return;
    }
    row.textContent = workedText(last);
  }, 1000);
}

/** A failed turn: one line of what went wrong, and the two things a person
 *  actually wants next — the whole error, and another go.
 *
 *  Retry is offered rather than automatic. A turn that failed may have failed
 *  because it was too long, or because the model is down, and re-sending it on
 *  the user's behalf spends their quota on a guess.
 *
 *  `.chat-msg.assistant.error` is kept: it is what the suite's existing checks
 *  look for, and this IS the failed turn's place in the transcript. The banner
 *  is what those classes now draw. */
function renderErrorBanner(msg) {
  const el = document.createElement('div');
  el.className = 'chat-msg assistant error chat-error';
  el.setAttribute('role', 'alert');

  const icon = document.createElement('span');
  icon.className = 'chat-error-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '\u26a0';

  // `.chat-text` so the friendly line is where every reader — a person, a
  // screen reader, a test — already looks for what a turn says.
  const line = document.createElement('p');
  line.className = 'chat-text chat-error-line';
  line.textContent = msg.content;

  const full = msg.detail || msg.content;
  const detail = document.createElement('pre');
  detail.className = 'chat-error-detail';
  detail.textContent = full;
  detail.hidden = true;
  detail.id = `chat-error-detail-${++errorSeq}`;

  const actions = document.createElement('div');
  actions.className = 'chat-error-actions';

  const reveal = document.createElement('button');
  reveal.type = 'button';
  reveal.className = 'btn ghost chat-error-reveal';
  reveal.textContent = 'Details';
  reveal.setAttribute('aria-expanded', 'false');
  reveal.setAttribute('aria-controls', detail.id);
  reveal.addEventListener('click', () => {
    detail.hidden = !detail.hidden;
    reveal.setAttribute('aria-expanded', String(!detail.hidden));
  });

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'btn ghost chat-error-copy';
  copy.textContent = 'Copy';
  copy.title = 'Copy the full error';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(full);
      announce('Error copied to clipboard');
    } catch {
      // No clipboard permission is not a dead end: unfolding the text leaves
      // it selectable, which is the manual version of the same thing.
      detail.hidden = false;
      reveal.setAttribute('aria-expanded', 'true');
      announce('Could not copy — the full error is shown instead');
    }
  });

  actions.append(reveal, copy);
  // Only where there is something to re-send. A turn restored from before this
  // existed carries no message, and a Retry that had nothing to send would be
  // a button that does nothing.
  if (msg.retry) {
    const again = document.createElement('button');
    again.type = 'button';
    again.className = 'btn chat-error-retry';
    again.textContent = 'Retry';
    again.disabled = assistantState.busy;
    again.addEventListener('click', () => retryTurn(msg));
    actions.appendChild(again);
  }

  el.append(icon, line, actions, detail);
  return el;
}

let errorSeq = 0;

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
