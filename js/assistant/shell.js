import { boardSuffix } from '../core/boards.js';
import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';

// Which shell the conversation is in — and nothing else.
//
// This is deliberately a leaf, for the same reason `core/boards.js` is one.
// Half the Assistant needs to ask "is the chat on screen at all?" before it
// repaints: session.js, models.js, tools.js, render.js. Asking widget.js would
// mean each of them importing the whole renderer — which pulls chrome.js, the
// transcript and the composer in behind it, and reorders the module graph. It
// did exactly that once: `core/state.js` does its restore while it evaluates,
// so `core/cards.js` reaching it first left `uid` in its temporal dead zone and
// the whole page died with "Cannot access 'uid' before initialization".
//
// So the widget's *state* lives here, where the imports are three leaves, and
// widget.js — which draws it — imports this rather than the other way round.

const WIDGET_KEY = KEY_PREFIX + 'widget' + boardSuffix;

// Per board, composed here the way MODELS_KEY is composed in models.js:
// core/keys.js imports nothing and must stay that way.
export const widgetState = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(WIDGET_KEY) || '{}');
    return {
      // Closed unless this board was left with it open — so an existing user
      // meets no change until they ask for one.
      open: saved.open === true,
      w: Number.isFinite(saved.w) ? saved.w : 0,
      h: Number.isFinite(saved.h) ? saved.h : 0,
    };
  } catch {
    return { open: false, w: 0, h: 0 };   // private mode or a corrupted value
  }
})();

export function persistWidget() {
  try { localStorage.setItem(WIDGET_KEY, JSON.stringify(widgetState)); }
  catch { /* private mode — the size still applies to this session */ }
}

/** Whether the widget should be painting. Open is a per-board preference; the
 *  Assistant view overrules it, because that view IS the conversation and a
 *  second copy of it over itself would put two `#chat-input` ids in one
 *  document — invalid HTML, and every selector this project owns broken. */
export const widgetShowing = () => widgetState.open && view !== 'assistant';

/** Is the conversation on screen in either shell? Every repaint in session.js
 *  and models.js is conditional on this. Asking about the view alone was right
 *  until the widget existed; it would now leave a chat opened from the widget
 *  unpainted. */
export const chatOnScreen = () => view === 'assistant' || widgetShowing();
