import { activeBoard, activeBoardId, boards, createBoard, deleteBoard,
  loadBoards, openBoard, renameBoard, setBoards,
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
    message: `Its ${current.cardCount} card(s) and its chats are kept — the board is hidden and can be restored from Menu ▾ → History.`,
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

/** Re-read the list from the server, for a caller that changed it elsewhere —
 *  the History dialog restoring a deleted board is the one that matters: the
 *  board must land back in this dropdown the moment it is restored. */
export async function refreshBoards() {
  if (await loadBoards()) paintSelect();
}
