import { activeBoard, activeBoardId, boards, createBoard, deleteBoard, fetchDeletedBoards,
  loadBoards, openBoard, purgeBoard, renameBoard, restoreBoard, setBoards,
  startLeavingBoard } from '../core/boards.js';
import { cancelPendingPush } from '../core/sync.js';
import { ask, prompt } from './dialogs.js';
import { $, announce } from './dom.js';

// The board picker — where you are, and the only way to somewhere else.
//
// Static markup in the header, so it is wired once here rather than on every
// render, and a repaint of the board underneath cannot take the open menu with
// it. Everything it does that changes which board is on screen goes through
// openBoard, which reloads the page; see core/boards.js for why.

const switcher = $('.board-switch');
const select = $('#board-select');
const menuBtn = $('#board-menu-btn');
const menuPanel = $('#board-menu-panel');
const dialog = $('#boards-dialog');
const trashList = $('#boards-trash-list');

function paintSelect() {
  select.replaceChildren(...boards.map((b) => {
    const option = document.createElement('option');
    option.value = b.id;
    // The name alone. A card count here was painted once at boot and went
    // stale on the next card you added, and it bought nothing: the board you
    // are on is the one whose cards are on screen, and the names are what tell
    // the others apart. The trash dialog does show counts, freshly fetched,
    // where "how much is in here" is the question being asked.
    option.textContent = b.name;
    option.selected = b.id === activeBoardId;
    return option;
  }));
}

/** Show the picker and fill it. Called from main.js after the board list has
 *  been fetched; with no backend the picker stays hidden. */
export function initBoardPicker() {
  switcher.hidden = false;
  paintSelect();
}

/** Leave for another board. The pending save is dropped first: it names the
 *  board being left, and after a delete that board is gone. */
function leaveFor(id) {
  cancelPendingPush();
  openBoard(id);
}

function setMenuOpen(open) {
  menuPanel.hidden = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
}

select.addEventListener('change', () => leaveFor(select.value));

menuBtn.addEventListener('click', () => setMenuOpen(menuPanel.hidden));
document.addEventListener('click', (e) => {
  // Scoped to this menu's own container: two dropdowns share the
  // .toolbar-menu class, and a check that only asked "is the click inside a
  // toolbar menu" would leave this one open while you used the other.
  if (!menuPanel.hidden && !e.target.closest('.board-switch')) setMenuOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !menuPanel.hidden) {
    setMenuOpen(false);
    menuBtn.focus();
  }
});
menuPanel.addEventListener('click', (e) => {
  if (e.target.closest('button')) setMenuOpen(false);
});

const failed = (verb, err) => announce(`Could not ${verb} — ${err.message}`);

$('#board-new').addEventListener('click', async () => {
  const name = await prompt({ title: 'New board', label: 'Name', okLabel: 'Create' });
  if (!name) return;
  try {
    leaveFor((await createBoard(name)).id);
  } catch (err) { failed('create that board', err); }
});

$('#board-rename').addEventListener('click', async () => {
  const current = activeBoard();
  if (!current) return;
  const name = await prompt({ title: 'Rename board', label: 'Name', value: current.name });
  if (!name) return;
  try {
    const board = await renameBoard(current.id, name);
    setBoards(boards.map((b) => (b.id === board.id ? board : b)));
    paintSelect();
    announce(`Renamed to ${board.name}`);
  } catch (err) { failed('rename that board', err); }
});

$('#board-delete').addEventListener('click', async () => {
  const current = activeBoard();
  if (!current) return;
  const ok = await ask({
    title: `Delete ${current.name}?`,
    message: `Its ${current.cardCount} card(s) and its chats are kept — the board is hidden and can be restored from “Deleted boards”.`,
    okLabel: 'Delete board',
    danger: true,
  });
  if (!ok) return;
  // Before the request, not after it: the save that would land on this board
  // is already queued, and the delete is an await it can fire inside.
  startLeavingBoard();
  cancelPendingPush();
  try {
    await deleteBoard(current.id);
    // Whichever board is left. The list still holds the deleted one, so it is
    // filtered out here rather than trusted.
    const next = boards.find((b) => b.id !== current.id);
    leaveFor(next ? next.id : current.id);
  } catch (err) { failed('delete that board', err); }
});

async function paintTrash() {
  let deleted = [];
  try {
    deleted = await fetchDeletedBoards();
  } catch (err) {
    trashList.replaceChildren(row(`Could not load deleted boards — ${err.message}`));
    return;
  }
  if (!deleted.length) {
    trashList.replaceChildren(row('No deleted boards.'));
    return;
  }
  trashList.replaceChildren(...deleted.map(boardRow));
}

function row(text) {
  const empty = document.createElement('p');
  empty.className = 'import-copy';
  empty.textContent = text;
  return empty;
}

function boardRow(board) {
  const item = document.createElement('div');
  item.className = 'history-row';

  const label = document.createElement('span');
  label.className = 'history-action';
  label.textContent = `${board.name} · ${board.cardCount} card(s)`;
  item.append(label);

  const restore = document.createElement('button');
  restore.type = 'button';
  restore.className = 'btn ghost';
  restore.textContent = 'Restore';
  restore.addEventListener('click', async () => {
    try {
      const back = await restoreBoard(board.id);
      setBoards([...boards, back]);
      paintSelect();
      await paintTrash();
      announce(`${back.name} is back`);
    } catch (err) { failed('restore that board', err); }
  });

  const purge = document.createElement('button');
  purge.type = 'button';
  purge.className = 'btn danger';
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
      await paintTrash();
      announce(`Erased ${board.name} — ${gone.cards} card(s), ${gone.sessions} chat(s)`);
    } catch (err) { failed('erase that board', err); }
  });

  item.append(restore, purge);
  return item;
}

$('#board-trash-btn').addEventListener('click', async () => {
  dialog.showModal();
  await paintTrash();
});

$('#close-boards').addEventListener('click', () => dialog.close());

/** Re-read the list from the server, for a caller that changed it elsewhere. */
export async function refreshBoards() {
  if (await loadBoards()) paintSelect();
}
