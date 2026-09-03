import { ensureChatSession } from './assistant/session.js';
import { cancelRecording, voiceState } from './assistant/voice.js';
import { loadBoards } from './core/boards.js';
import { initTimeline } from './core/history.js';
import { initServerSync } from './core/sync.js';
import { initBoardPicker } from './ui/boards-picker.js';
import { announce } from './ui/dom.js';
import { CHIME_NAMES, HABIT_MUTE_KEY, habitMuted, playChime, renderHabitBanner,
  setHabitChime, setHabitMuted, syncChimePicker, syncHabitMute } from './ui/habits.js';
import { setPlanLayout, syncPlanLayoutPicker } from './ui/plan.js';
import { refreshEdits, refreshProposals } from './ui/proposals.js';
import { render, syncViewButtons } from './ui/render.js';

// Lodestar — the browser half. This is the entry point index.html loads, and
// the only script tag in the page: everything else is reached through the
// import graph below.
//
// What used to be one 6,400-line IIFE is now ~40 modules under js/. The closure
// that kept the app's internals private is gone, and ES modules do that job
// instead — a name is private unless the module exports it, which is a stronger
// guarantee than "it is somewhere in the same function" and, unlike the
// closure, tells you where each thing lives from its path alone.
//
// Two properties this file exists to preserve:
//
//  1. A module runs when it is first imported, and only if something imports
//     it. Several modules wire their own controls at evaluation time (the card
//     dialog, the toolbar, the export sheet), so a module dropped out of the
//     graph does not fail loudly — its buttons simply stop working. The
//     side-effect imports below are named for exactly that reason, and
//     tests/frontend.test.js walks this graph to prove every module is reached.
//
//  2. <script type="module"> is deferred, so the DOM is parsed before any of
//     this runs. Modules may therefore look up their elements at the top level,
//     the same way the old script at the end of <body> could.

// Imported for the controls they wire as they evaluate — nothing here calls
// into them. Removing one of these lines silently disables that surface.
import './ui/cats-dialog.js';
import './ui/edit-dialog.js';
import './ui/history-dialog.js';
import './ui/keyboard.js';
import './ui/theme.js';
import './ui/toolbar.js';
import './ui/transfer.js';

// Wiring that belongs to no single module

// Escape abandons a recording from anywhere in the page, which is why it is
// here and not on the composer: the mic keeps running while you scroll away.

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && voiceState.phase === 'recording') {
    event.preventDefault();
    cancelRecording();
  }
});

// The habit sound lives in the actions menu but is owned by the habit rail, so
// the toggle is wired where the two meet rather than inside either. Switching
// it ON is greeted by the chosen chime — the one moment a demo is exactly the
// confirmation wanted; going silent is confirmed silently, on principle.
document.querySelector('#habit-mute').addEventListener('click', () => {
  setHabitMuted(!habitMuted);
  localStorage.setItem(HABIT_MUTE_KEY, habitMuted ? '1' : '0');
  syncHabitMute();
  if (!habitMuted) playChime();
  announce(habitMuted ? 'Habit reminders are silent' : 'Habit reminders will sound');
});
syncHabitMute();

// The Sound submenu: four chimes, radio semantics, and picking one previews
// it — choosing a sound you cannot hear is not choosing.
for (const name of CHIME_NAMES) {
  document.querySelector(`#sound-${name}`)?.addEventListener('click', () => {
    setHabitChime(name);
    syncChimePicker();
    playChime(name);
  });
}
syncChimePicker();

// The Plan submenu: whether the rail shows every horizon at once or one at a
// time. It lives in the menu but belongs to the rail, so it is wired here
// where the two meet — the same arrangement as the habit sound above.
for (const name of ['stacked', 'dropdown']) {
  document.querySelector(`#plan-${name}`)?.addEventListener('click', () => {
    setPlanLayout(name);
    syncPlanLayoutPicker();
    render();
    announce(`Plan section: ${name}`);
  });
}
syncPlanLayoutPicker();

// A slot time passing is the other moment a habit comes due, so the reminder
// is re-checked while the board is open, not only when it is opened.
setInterval(renderHabitBanner, 30_000);

// --------------------------------------------------------------------------
// Go
// --------------------------------------------------------------------------

initTimeline();      // the undo timeline, opened on the board as restored
syncViewButtons();   // mark the restored view before the first paint
render();            // instant paint from localStorage — the widget with it,
                     // since a board left with it open reopens with it open
// The picker only appears once the server has listed the boards, and that list
// is also what catches an active board this browser remembers but the database
// no longer has — loadBoards resets and reloads there, so the sync below must
// not run first and push a stale board's cards at whatever answers.
loadBoards().then((ok) => {
  if (!ok) return;
  initBoardPicker();
  initServerSync();  // then reconcile with the SQLite backend if one is running
});
refreshProposals();  // and surface anything the Assistant left awaiting approval
refreshEdits();
// Which chat is open. Unconditional rather than only when the Assistant is the
// view being restored: the transcript now comes from the record, so a reload
// straight into the Assistant must not paint an empty sheet while it waits.
ensureChatSession();
