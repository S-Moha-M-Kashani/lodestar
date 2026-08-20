import { fetchDeletedBoards, purgeBoard, restoreBoard } from '../core/boards.js';
import { cardLabel } from '../core/cards.js';
import { restoreEntry, timeline } from '../core/history.js';
import { serverAvailable } from '../core/sync.js';
import { fetchTrash, purgeFromTrash, restoreFromTrash } from '../core/trash.js';
import { refreshBoards } from './boards-picker.js';
import { ask } from './dialogs.js';
import { $, announce } from './dom.js';

// Undo and the history dialog — the board's reflog — with the server-backed
// Trash beside it: everything that was removed, cards and whole boards alike,
// is brought back from here.

// Undo left the Menu (review, 2026-08-20): the timeline below restores any
// state, which is undo with a memory, so one control does the job of two.

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

  refreshBoardsTrash(); // populate the "Deleted boards" section from the server
  refreshTrash(); // and the "Deleted cards" one
}

// Deleted boards, above the deleted cards — a whole board is the biggest
// thing this app can delete, and its row says so in words. This used to be
// its own dialog behind the board menu's "Deleted boards…", where the person
// who deleted a board never found it; the history is where you look for what
// happened, so it is where a deletion has to be undone.
async function refreshBoardsTrash() {
  const section = $('#boards-trash-section');
  const list = $('#boards-trash-list');
  if (!section || !list) return;
  if (!serverAvailable) { section.hidden = true; return; }

  let deleted = [];
  try {
    deleted = await fetchDeletedBoards();
  } catch (_) { section.hidden = true; return; }
  if (!deleted.length) { section.hidden = true; list.innerHTML = ''; return; }
  section.hidden = false;
  list.replaceChildren(...deleted.map(boardRow));
}

function boardRow(board) {
  const row = document.createElement('div');
  row.className = 'history-row';

  const main = document.createElement('div');
  main.className = 'history-main';
  const label = document.createElement('p');
  label.className = 'history-action';
  label.textContent = `Board “${board.name}” is deleted`;
  const meta = document.createElement('span');
  meta.className = 'history-meta';
  meta.textContent = `${board.cardCount} card(s)`;
  main.append(label, meta);

  const actions = document.createElement('div');
  actions.className = 'trash-actions';

  const restore = document.createElement('button');
  restore.type = 'button';
  restore.className = 'btn ghost history-restore';
  restore.textContent = 'Restore';
  restore.addEventListener('click', async () => {
    try {
      const back = await restoreBoard(board.id);
      await refreshBoards(); // back in the picker's dropdown at once
      await refreshBoardsTrash();
      announce(`${back.name} is back`);
    } catch (err) { announce(`Could not restore that board — ${err.message}`); }
  });

  const purge = document.createElement('button');
  purge.type = 'button';
  purge.className = 'btn danger history-restore';
  purge.textContent = 'Delete permanently';
  purge.addEventListener('click', async () => {
    const ok = await ask({
      title: `Erase ${board.name}?`,
      message: `This deletes the board, its ${board.cardCount} card(s) and its chats for good. Nothing else can recover them.`,
      okLabel: 'Delete permanently',
      danger: true,
    });
    if (!ok) return;
    try {
      const gone = await purgeBoard(board.id);
      await refreshBoardsTrash();
      announce(`Erased ${board.name} — ${gone.cards} card(s), ${gone.sessions} chat(s)`);
    } catch (err) { announce(`Could not erase that board — ${err.message}`); }
  });

  actions.append(restore, purge);
  row.append(main, actions);
  return row;
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
