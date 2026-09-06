import { KEY_PREFIX } from '../core/keys.js';
import { $ } from './dom.js';

// The divider between the three card columns and the rail, and the one number
// it owns.
//
// The rail was a fixed 200px track. That is right for one window and wrong for
// every other: a long habit name or a plan row clipped on a narrow screen,
// while a wide one gave the cards room the rail could have used. The width is
// now the user's, and it is a *single* number because the three columns share
// `1fr` each — whatever the rail gives up is handed back to Inbox, In Progress
// and Done in equal parts, which is the only division that keeps the board
// looking like one thing.
//
// role="separator" with a value and a tab stop is the window-splitter pattern:
// a divider that answers only the mouse is one a keyboard user cannot move at
// all, and this one carries a size rather than a state.

const KEY = KEY_PREFIX + 'railWidth';

// The default is the width the rail has always had, so a browser that has
// never touched the divider sees exactly the board it saw before.
const DEFAULT_W = 200;
// The bounds are a judgement, not a measurement, and they are bounded by
// something real at each end: below ~140px a habit row's punch strip and a
// plan row's date chip stop sharing a line, and above ~460px the three columns
// reach the 210px minimum the grid already refuses to go under — past that the
// grid simply stops obeying and the drag reads as broken.
const MIN_W = 140;
const MAX_W = 460;
// One arrow press. Small enough to aim with, large enough that the divider
// visibly moves rather than appearing not to answer the key.
const STEP = 16;

const clamp = (w) => Math.min(MAX_W, Math.max(MIN_W, Math.round(w)));

function stored() {
  try {
    const raw = Number(localStorage.getItem(KEY));
    return raw ? clamp(raw) : DEFAULT_W;
  } catch (_) {
    return DEFAULT_W; // private mode
  }
}

let width = stored();

/** Write the width onto #board, where the grid template reads it.
 *
 *  The property lives on the static host and not on anything render() builds:
 *  `render()` empties #board's children on every repaint and the element itself
 *  survives, so the size needs no restore step and cannot be lost mid-drag. */
function apply() {
  $('#board')?.style.setProperty('--rail-w', `${width}px`);
}

function setWidth(next, grip, persist) {
  width = clamp(next);
  apply();
  grip.setAttribute('aria-valuenow', String(width));
  // Persisted at the end of a gesture rather than on every pointermove: a drag
  // is one decision, and writing sixty of them is sixty writes for it.
  if (persist) {
    try { localStorage.setItem(KEY, String(width)); } catch (_) { /* private mode */ }
  }
}

/** The divider itself, rebuilt with the rail on every board render.
 *
 *  It is a grid item sharing the rail's track (styles.css) rather than a track
 *  of its own: a fifth column would take real width from the cards to paint a
 *  line, and the divider is the edge between two things rather than a thing
 *  between them. */
export function railGrip() {
  apply(); // whatever was stored, before the first paint of this view

  const grip = document.createElement('div');
  grip.className = 'rail-grip';
  grip.tabIndex = 0;
  grip.setAttribute('role', 'separator');
  grip.setAttribute('aria-orientation', 'vertical');
  grip.setAttribute('aria-label', 'Width of the habits and plan rail');
  grip.setAttribute('aria-valuemin', String(MIN_W));
  grip.setAttribute('aria-valuemax', String(MAX_W));
  grip.setAttribute('aria-valuenow', String(width));

  grip.addEventListener('pointerdown', (e) => {
    // Without this the browser starts a text selection and the whole board
    // highlights blue under the drag.
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    // Pointer capture, so the gesture survives the pointer leaving this 8px
    // strip — which it does immediately, that being the point of dragging it.
    grip.setPointerCapture(e.pointerId);
    grip.classList.add('dragging');

    // Leftwards is wider: the rail is the right-hand margin, so moving the
    // divider towards the cards is what gives the rail the room.
    const move = (ev) => setWidth(startW + (startX - ev.clientX), grip, false);
    const up = (ev) => {
      grip.releasePointerCapture(ev.pointerId);
      grip.classList.remove('dragging');
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      grip.removeEventListener('pointercancel', up);
      setWidth(width, grip, true); // the gesture is over: keep it
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
    grip.addEventListener('pointercancel', up);
  });

  grip.addEventListener('keydown', (e) => {
    const step = e.key === 'ArrowLeft' ? STEP : e.key === 'ArrowRight' ? -STEP : 0;
    if (!step) return;
    e.preventDefault();
    setWidth(width + step, grip, true);
  });

  return grip;
}
