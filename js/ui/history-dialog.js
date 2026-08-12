import { cardLabel } from '../core/cards.js';
import { restoreEntry, timeline } from '../core/history.js';
import { serverAvailable } from '../core/sync.js';
import { fetchTrash, purgeFromTrash, restoreFromTrash } from '../core/trash.js';
import { $ } from './dom.js';

// Undo and the history dialog — the board's reflog — with the server-backed
// Trash beside it.

$('#undo-btn').addEventListener('click', () => {
  if (timeline.index <= 0) return;
  const undone = timeline.entries[timeline.index].action;
  restoreEntry(timeline.index - 1, `Undid “${undone}”`);
});

const historyDialog = $('#history-dialog');

$('#history-btn').addEventListener('click', () => {
  renderHistory();
  historyDialog.showModal();
});

$('#close-history').addEventListener('click', () => historyDialog.close());

function renderHistory() {
  const list = $('#history-list');
  list.innerHTML = '';
  const fmt = (ts) =>
    new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  for (let i = timeline.entries.length - 1; i >= 0; i--) {
    const entry = timeline.entries[i];
    const row = document.createElement('div');
    row.className = 'history-row' + (i === timeline.index ? ' current' : '');

    const time = document.createElement('span');
    time.className = 'history-time';
    time.textContent = fmt(entry.ts);

    const main = document.createElement('div');
    main.className = 'history-main';

    const action = document.createElement('p');
    action.className = 'history-action';
    action.textContent = entry.action;

    const meta = document.createElement('span');
    meta.className = 'history-meta';
    meta.textContent = `${entry.cards.length} card${entry.cards.length === 1 ? '' : 's'}`;

    main.append(action, meta);
    row.append(time, main);

    if (i === timeline.index) {
      const mark = document.createElement('span');
      mark.className = 'history-current';
      mark.textContent = 'current';
      row.append(mark);
    } else {
      const btn = document.createElement('button');
      btn.className = 'btn ghost history-restore';
      btn.textContent = 'Restore';
      btn.addEventListener('click', () => {
        restoreEntry(i, `Restored board to “${entry.action}”`);
        renderHistory(); // keep the dialog open, move the “current” mark
      });
      row.append(btn);
    }

    list.append(row);
  }

  refreshTrash(); // populate the "Deleted cards" section from the server
}

// Fill the Trash section of the History dialog with the server's soft-deleted
// cards. Hidden entirely when there's no backend or nothing is trashed.
async function refreshTrash() {
  const section = $('#trash-section');
  const list = $('#trash-list');
  if (!section || !list) return;
  if (!serverAvailable) { section.hidden = true; return; }

  const trashed = await fetchTrash();
  if (!trashed.length) { section.hidden = true; list.innerHTML = ''; return; }
  section.hidden = false;
  list.innerHTML = '';

  for (const card of trashed) {
    const row = document.createElement('div');
    row.className = 'history-row';

    const label = document.createElement('span');
    label.className = 'history-time';
    label.textContent = cardLabel(card);

    const main = document.createElement('div');
    main.className = 'history-main';
    const title = document.createElement('p');
    title.className = 'history-action';
    title.textContent = card.title;
    const meta = document.createElement('span');
    meta.className = 'history-meta';
    meta.textContent = card.tags && card.tags.length ? card.tags.map((t) => '#' + t).join(' ') : 'no tags';
    main.append(title, meta);

    const actions = document.createElement('div');
    actions.className = 'trash-actions';

    const restore = document.createElement('button');
    restore.className = 'btn ghost history-restore';
    restore.textContent = 'Restore';
    restore.addEventListener('click', () => {
      restoreFromTrash(card);
      row.remove(); // optimistic — the server clears deleted_at on the next push
      if (!list.children.length) section.hidden = true;
    });

    const purge = document.createElement('button');
    purge.className = 'btn danger history-restore';
    purge.textContent = 'Delete permanently';
    purge.addEventListener('click', async () => {
      if (await purgeFromTrash(card)) {
        row.remove();
        if (!list.children.length) section.hidden = true;
      }
    });

    actions.append(restore, purge);
    row.append(label, main, actions);
    list.append(row);
  }
}
