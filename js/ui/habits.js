import { catColor } from '../core/categories.js';
import { HABIT_EVERY, HABIT_NOW, cadenceText, habitCards, habitDoneIn, habitDoneNow, habitDue, habitPeriod, habitPeriodsBack, habitRetired, habitTally, pad2, punchHabit, unpunchHabit } from '../core/habits.js';
import { commit, short } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';
import { $, announce, getCard } from './dom.js';
import { openDialog } from './edit-dialog.js';
import { render } from './render.js';

// Habit UI — the punch strip, the history tape, the rail and the reminder.
//
// The strip is the signature object: one box per repetition the period asks
// for, stamped in the card's own category ink. It is the count, the progress
// and the control at once, so there is no second widget to keep in step.

const PUNCH_MAX_BOXES = 8;  // past this a strip stops being readable at a glance
const TAPE_PERIODS = { daily: 21, weekly: 12, monthly: 12, yearly: 6 };
const openTapes = new Set(); // card ids whose history is expanded

function punchStrip(card) {
  const strip = document.createElement('div');
  strip.className = 'habit-punch';
  const done = habitDoneNow(card);
  const shown = Math.min(card.habitCount, PUNCH_MAX_BOXES);

  for (let i = 0; i < shown; i++) {
    const stamped = i < done;
    const box = document.createElement('button');
    box.type = 'button';
    box.className = 'punch-box' + (stamped ? ' done' : '') + (i === done ? ' next' : '');
    box.textContent = stamped ? '✓' : '';
    // Only the newest stamp can be taken back — a stack, so the history keeps
    // matching the order things actually happened in.
    box.disabled = stamped && i < done - 1;
    box.title = stamped
      ? (box.disabled ? 'Recorded' : 'Take this one back')
      : 'Record one now';
    box.setAttribute('aria-label',
      `${card.title}: ${box.title.toLowerCase()} (${done} of ${card.habitCount} ${HABIT_NOW[card.habitFreq]})`);
    box.addEventListener('click', (e) => {
      e.stopPropagation(); // the card itself opens the edit dialog
      const target = getCard(card.id);
      if (!target) return;
      const undo = i < habitDoneNow(target);
      if (undo ? unpunchHabit(target) : punchHabit(target)) {
        commit(`${undo ? 'Took back' : 'Recorded'} “${short(target.title)}”`);
        announce(`${target.title}: ${habitTally(getCard(card.id))}`);
      }
    });
    strip.append(box);
  }

  if (card.habitCount > shown) {
    const more = document.createElement('span');
    more.className = 'punch-more';
    more.textContent = `${done}/${card.habitCount}`;
    strip.append(more);
  }
  return strip;
}

/** The history, run sideways: one cell per past period, carrying the number
 *  punched into it, dotted where the period was missed. */
function habitTape(card) {
  const n = TAPE_PERIODS[card.habitFreq] || 21;
  const periods = habitPeriodsBack(card.habitFreq, n);
  const current = habitPeriod(card.habitFreq);

  const wrap = document.createElement('div');
  wrap.className = 'habit-tape';

  const label = document.createElement('div');
  label.className = 'tape-label';
  label.textContent = `Last ${n} ${HABIT_EVERY[card.habitFreq]}s · oldest first`;

  const row = document.createElement('div');
  row.className = 'tape-row';
  let complete = 0, run = 0, best = 0;
  for (const period of periods) {
    const done = habitDoneIn(card, period);
    const full = done >= card.habitCount;
    if (full) { complete++; best = Math.max(best, ++run); }
    // An unfinished *current* period is not yet a broken run — the day isn't over.
    else if (period !== current) run = 0;

    const cell = document.createElement('span');
    cell.className = 'tape-cell ' + (full ? 'full' : done ? 'part' : 'miss');
    if (period === current) cell.classList.add('today');
    cell.textContent = done ? String(done) : '';
    cell.title = `${period} — ${done} of ${card.habitCount}`;
    row.append(cell);
  }

  const summary = document.createElement('div');
  summary.className = 'tape-summary';
  summary.textContent = `${complete} of ${n} complete · longest run ${best}`;

  wrap.append(label, row, summary);
  return wrap;
}

/** Everything a habit adds to its card: the cadence in words, the strip, and
 *  the history behind a button. */
export function habitCardParts(card, el) {
  const cadence = document.createElement('p');
  cadence.className = 'habit-cadence';
  cadence.textContent = habitRetired(card) ? `${cadenceText(card)} · retired` : cadenceText(card);
  el.append(cadence);

  const line = document.createElement('div');
  line.className = 'habit-line';
  if (!habitRetired(card)) line.append(punchStrip(card));

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'habit-history-toggle';
  toggle.textContent = '↻ history';
  toggle.title = 'Show what you have done';
  toggle.setAttribute('aria-expanded', String(openTapes.has(card.id)));
  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    if (openTapes.has(card.id)) openTapes.delete(card.id);
    else openTapes.add(card.id);
    render();
  });
  line.append(toggle);
  el.append(line);

  if (openTapes.has(card.id)) el.append(habitTape(card));
}

/** The rail: today's habits beside the board. Absent until there is one — an
 *  empty panel would cost every non-habit user a column of space. */
export function renderHabitRail() {
  const habits = habitCards().filter((c) => !habitRetired(c));
  if (!habits.length) return null;

  const rail = document.createElement('aside');
  rail.className = 'habit-rail';
  rail.setAttribute('aria-label', 'Habits');

  const head = document.createElement('div');
  head.className = 'habit-rail-head';
  const title = document.createElement('h2');
  title.className = 'habit-rail-title';
  title.textContent = 'Habits';
  const sub = document.createElement('p');
  sub.className = 'habit-rail-sub';
  const due = habits.filter(habitDue).length;
  sub.textContent = due ? `${due} due` : 'All done';
  head.append(title, sub);
  rail.append(head);

  // Due first, finished after — a day's ledger, not a nag list.
  for (const card of [...habits].sort((a, b) => Number(habitDue(b)) - Number(habitDue(a)))) {
    const row = document.createElement('div');
    row.className = 'habit-rail-row' + (habitDue(card) ? '' : ' done');
    row.style.setProperty('--cat', catColor(card.category));
    if (card.category) row.classList.add('categorized');

    const top = document.createElement('div');
    top.className = 'habit-rail-name';
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'habit-rail-open';
    name.textContent = card.title;
    name.title = 'Open this card';
    name.addEventListener('click', () => openDialog(card.id));
    const tally = document.createElement('span');
    tally.className = 'habit-rail-tally';
    tally.textContent = habitTally(card);
    top.append(name, tally);

    row.append(top, punchStrip(card));
    rail.append(row);
  }
  return rail;
}

// --- The reminder ---------------------------------------------------------
// A banner that says what is due, and one short bip. The bip is a bonus:
// browsers refuse audio before the first gesture, so the banner is the
// channel that always works.

export const HABIT_MUTE_KEY = KEY_PREFIX + 'habit-mute';
export let habitMuted = localStorage.getItem(HABIT_MUTE_KEY) === '1';

/** Toggled from the actions menu, which main.js wires — see the note on the
 *  setters in core/state.js. Persisting is the caller's business, as it was. */
export function setHabitMuted(muted) {
  habitMuted = muted;
}
let habitBannerHidden = false; // dismissed for this session; a reload brings it back
let audioCtx = null;
// Keys of things already sounded, so the bip marks a change rather than
// repeating on every render.
const sounded = new Set();

/** How many repetitions the clock has asked for so far. With no slots set the
 *  whole period is fair game, so the full target is expected from its start. */
function habitExpectedBy(card, at = new Date()) {
  if (!card.habitTimes.length) return card.habitCount;
  const now = `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
  return card.habitTimes.filter((t) => t <= now).length;
}
const habitReminding = (card, at = new Date()) =>
  habitDue(card) && habitDoneNow(card) < habitExpectedBy(card, at);

// --- The chime -------------------------------------------------------------
// Four synthesized voices, no audio files. The chosen one is what the
// reminder plays and what greets the sound being switched on; the Sound
// submenu previews each on click. `lodestar:chime` fires on window whenever a
// chime is *asked* to play — audio itself is at the browser's mercy (no
// sound before the first gesture on the page), so the event is the honest,
// testable record of intent and the banner stays the channel that always
// works.

export const CHIME_KEY = KEY_PREFIX + 'habitChime';
export const CHIME_NAMES = ['marimba', 'bell', 'droplet', 'kalimba'];
export let habitChime = (() => {
  const stored = localStorage.getItem(CHIME_KEY);
  return CHIME_NAMES.includes(stored) ? stored : 'marimba';
})();

export function setHabitChime(name) {
  if (!CHIME_NAMES.includes(name)) return;
  habitChime = name;
  try { localStorage.setItem(CHIME_KEY, name); } catch (_) { /* private mode */ }
}

// One tone: frequency, start offset, length, peak gain, wave, optional glide.
function tone(a, { f, at = 0, len = 0.3, peak = 0.09, type = 'sine', glideTo = null }) {
  const t = a.currentTime + at;
  const osc = a.createOscillator();
  const gain = a.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(f, t);
  if (glideTo) osc.frequency.exponentialRampToValueAtTime(glideTo, t + len * 0.7);
  gain.gain.setValueAtTime(0.0001, t);
  gain.gain.exponentialRampToValueAtTime(peak, t + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, t + len);
  osc.connect(gain).connect(a.destination);
  osc.start(t);
  osc.stop(t + len + 0.05);
}

const CHIMES = {
  marimba(a) { // two warm wooden notes, E6 then A6
    tone(a, { f: 1318.5, type: 'triangle', len: 0.16, peak: 0.10 });
    tone(a, { f: 1760.0, type: 'triangle', at: 0.11, len: 0.22, peak: 0.09 });
  },
  bell(a) { // A5 with a gentle octave-and-a-third shimmer
    tone(a, { f: 880, len: 0.55, peak: 0.08 });
    tone(a, { f: 1760, len: 0.4, peak: 0.03 });
    tone(a, { f: 2637, len: 0.25, peak: 0.015 });
  },
  droplet(a) { // a watery upward blip, C6 gliding to G6
    tone(a, { f: 1046.5, glideTo: 1568, len: 0.18, peak: 0.09 });
    tone(a, { f: 2093, at: 0.14, len: 0.12, peak: 0.03 });
  },
  kalimba(a) { // a muted thumb-piano pluck on A4
    tone(a, { f: 440, type: 'triangle', len: 0.3, peak: 0.11 });
    tone(a, { f: 880, type: 'sine', len: 0.18, peak: 0.03 });
  },
};

/** Play a chime by name (the chosen one by default) and say so on window. */
export function playChime(name = habitChime) {
  if (!CHIME_NAMES.includes(name)) name = 'marimba';
  window.dispatchEvent(new CustomEvent('lodestar:chime', { detail: { name } }));
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    CHIMES[name](audioCtx);
  } catch {
    // No audio device, or no user gesture yet. The banner still shows.
  }
}

function bip() {
  if (habitMuted) return;
  playChime();
}

export function renderHabitBanner() {
  const slot = $('#habit-banner-slot');
  if (!slot) return;
  slot.innerHTML = '';
  if (view !== 'board') return;

  const at = new Date();
  const due = habitCards().filter((c) => habitReminding(c, at));
  if (!due.length) return;

  // One key per habit per period per slot reached, so a passing slot time
  // sounds again but a re-render never does.
  for (const c of due) {
    const key = `${c.id}@${habitPeriod(c.habitFreq)}@${habitExpectedBy(c, at)}`;
    if (sounded.has(key)) continue;
    sounded.add(key);
    habitBannerHidden = false; // something new came due — show it again
    bip();
  }
  if (habitBannerHidden) return;

  const banner = document.createElement('div');
  banner.className = 'habit-banner';

  const bell = document.createElement('span');
  bell.className = 'habit-banner-bell';
  bell.textContent = '🔔';

  const text = document.createElement('p');
  text.className = 'habit-banner-text';
  const list = due.map((c) => `${c.title} (${habitTally(c)})`).join(' · ');
  text.textContent = `${due.length} habit${due.length > 1 ? 's' : ''} due — ${list}`;

  const hide = document.createElement('button');
  hide.type = 'button';
  hide.className = 'habit-banner-hide';
  hide.textContent = 'Hide';
  hide.title = 'Hide until the next one comes due';
  hide.addEventListener('click', () => { habitBannerHidden = true; renderHabitBanner(); });

  banner.append(bell, text, hide);
  slot.append(banner);
}

export function syncHabitMute() {
  const btn = $('#habit-mute');
  if (!btn) return;
  // The state in a word, right of the sign: "on" in green, "off" in red —
  // an icon that only swaps 🔊/🔇 made people look twice.
  const state = document.createElement('span');
  state.className = `habit-sound-state ${habitMuted ? 'off' : 'on'}`;
  state.textContent = habitMuted ? 'off' : 'on';
  btn.replaceChildren(
    document.createTextNode(habitMuted ? '🔇 ' : '🔊 '),
    state,
    document.createTextNode(' Habit sound'),
  );
  btn.setAttribute('aria-pressed', String(!habitMuted));
  btn.title = habitMuted ? 'Habit reminders are silent' : 'Sound the reminder when a habit is due';
}

/** Mark the chosen chime in the Sound submenu. */
export function syncChimePicker() {
  for (const name of CHIME_NAMES) {
    $(`#sound-${name}`)?.setAttribute('aria-checked', String(name === habitChime));
  }
}
