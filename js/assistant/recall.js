import { boardUrl } from '../core/boards.js';
import { cardLabel } from '../core/cards.js';
import { render } from '../ui/render.js';

// Searching the record — the leftmost of the Assistant's header tools. Asks
// the brain for past turns matching a query and lists them in place, so
// remembering something does not cost you the conversation you are in.

const recallState = { open: false, query: '', matches: null, memory: true,
                      busy: false, failed: false, focused: false };

export function renderRecallPanel() {
  const box = document.createElement('details');
  box.className = 'chat-recall';
  box.open = recallState.open;
  box.addEventListener('toggle', () => { recallState.open = box.open; });
  const name = document.createElement('summary');
  name.className = 'chat-recall-name';
  name.textContent = 'Search past conversations';
  box.appendChild(name);

  const form = document.createElement('form');
  form.className = 'chat-recall-form';
  const input = document.createElement('input');
  input.id = 'recall-input';
  input.type = 'search';
  input.placeholder = 'What did we say about…';
  input.value = recallState.query;
  input.addEventListener('input', () => { recallState.query = input.value; });
  input.addEventListener('focus', () => { recallState.focused = true; });
  input.addEventListener('blur', () => { recallState.focused = false; });
  const go = document.createElement('button');
  go.type = 'submit';
  go.id = 'recall-search';
  go.className = 'btn';
  go.textContent = 'Search';
  go.disabled = recallState.busy;
  form.append(input, go);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    recallChat(input.value.trim());
  });
  // The body rides in one box so the header can drop it as a panel from under
  // the summary. Two loose siblings could not be positioned as one thing.
  const drop = document.createElement('div');
  drop.className = 'chat-recall-drop';
  drop.append(form, renderRecallResults());
  box.appendChild(drop);

  // render() rebuilds the whole sheet, and a streaming reply repaints it many
  // times a second — so without this, typing here while a reply arrives loses
  // the caret on the next frame.
  if (recallState.focused) {
    requestAnimationFrame(() => {
      const live = document.getElementById('recall-input');
      if (!live || document.activeElement === live) return;
      live.focus();
      live.setSelectionRange(live.value.length, live.value.length);
    });
  }
  return box;
}

function renderRecallResults() {
  const out = document.createElement('div');
  out.className = 'chat-recall-results';
  if (recallState.busy) { out.textContent = 'Searching…'; return out; }
  if (recallState.failed) {
    out.textContent = 'Could not reach the assistant to search.';
    return out;
  }
  if (recallState.matches === null) {
    out.textContent = 'Search what you and the assistant have said before, and the cards on the board.';
    return out;
  }
  if (!recallState.matches.length) {
    // Deliberately not "no matches" when memory is off: that is the service
    // being switched off, not the history being empty, and the two send you
    // to different places.
    out.textContent = recallState.memory
      ? 'Nothing recorded about that yet.'
      : 'Chat memory is off, so nothing has been recorded. '
        + 'Start the Chroma container to keep conversations.';
    return out;
  }
  const list = document.createElement('ol');
  list.className = 'recall-hits';
  for (const hit of recallState.matches) {
    const item = document.createElement('li');
    item.className = 'recall-hit';
    const said = document.createElement('p');
    said.className = 'recall-hit-text';
    said.textContent = hit.text;
    const meta = document.createElement('p');
    meta.className = 'recall-hit-meta';
    // A brain too old to label sources only ever returned chat hits, so
    // the missing field reads as 'chat' rather than as a guess.
    const source = hit.source === 'card'
      ? `card${hit.metadata && hit.metadata.num ? ' ' + cardLabel({ num: hit.metadata.num }) : ''}`
      : `chat · ${(hit.metadata && hit.metadata.role) || 'unknown'}`;
    meta.textContent = `${source} · ${hit.score}`;
    item.append(said, meta);
    list.appendChild(item);
  }
  out.appendChild(list);
  if (!recallState.memory) {
    const note = document.createElement('p');
    note.className = 'chat-recall-note';
    note.textContent = 'Chat memory is off — these matches are from cards only. '
      + 'Start the Chroma container to keep conversations.';
    out.appendChild(note);
  }
  return out;
}

async function recallChat(text) {
  if (!text) return;
  recallState.busy = true;
  recallState.failed = false;
  render();
  try {
    const res = await fetch(boardUrl('/api/rag/recall'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, k: 5 }),
    });
    if (!res.ok) throw new Error(`recall ${res.status}`);
    const data = await res.json();
    recallState.matches = data.matches || [];
    // Only an explicit false means off. A brain too old to send the field
    // cannot be reported as having memory switched off — that would be a
    // claim about the service made from its silence.
    recallState.memory = data.memory !== false;
  } catch {
    recallState.failed = true;
    recallState.matches = null;
  }
  recallState.busy = false;
  render();
}
