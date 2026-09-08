import { catColor } from '../core/categories.js';
import { HABIT_EVERY, HABIT_NOW, cadenceText, habitCards, habitDoneIn, habitDoneNow, habitDue, habitExpectedBy, habitPeriod, habitPeriodsBack, habitReminding, habitRetired, habitTally, punchHabit, unpunchHabit } from '../core/habits.js';
import { commit, short } from '../core/history.js';
import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';
import { wireCardContext } from './card-menu.js';
import { $, announce, getCard } from './dom.js';
import { openDialog } from './edit-dialog.js';

// Habit UI — the punch strip, the history tape, the rail and the reminder.
//
// The strip is the signature object: one box per repetition the period asks
// for, stamped in the card's own category ink. It is the count, the progress
// and the control at once, so there is no second widget to keep in step.
//
// A habit is drawn here and nowhere else. It was also painted as a card in its
// board column until 2026-09-07, which meant two shapes for one fact and a
// second punch strip to keep in step; `habitCardParts` drew that and is gone.
// The strip is the rail's, and the history tape is the card dialog's.

// Past this the strip stops being readable at a glance, and — since 2026-09-07
// — stops leaving room for the name beside it. It was 8 while the strip had a
// whole line of a board card to itself; the rail's row is one line, 200px wide
// by default, and eight boxes plus a written count filled all of it, leaving a
// name of zero width that could not even be clicked. Five plus the count is
// what fits, and `punch-more` is what still says the real number.
const PUNCH_MAX_BOXES = 5;
const TAPE_PERIODS = { daily: 21, weekly: 12, monthly: 12, yearly: 6 };

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
 *  punched into it, dotted where the period was missed.
 *
 *  It lives in the card dialog's schedule block since 2026-09-07 — beside the
 *  cadence that produced it, and folded, which is why nothing here tracks
 *  whether it is open any more: a `<details>` element holds that. It was drawn
 *  on the board card until then, and a habit is not drawn as a board card. */
export function habitTape(card) {
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

/** The rail's habits section — the one place a habit appears.
 *
 *  Absent until there is a habit at all: an empty list would cost every
 *  non-habit user the top of the panel. Retired habits keep it alive on their
 *  own, though, behind the closed line at its foot — dropping a retired habit
 *  from the rail *and* from the columns would leave a card reachable only from
 *  the Backlog, which is how a habit gets lost the day after you finish with
 *  it.
 *
 *  One line per habit, and the punch boxes come first: punching is what the
 *  rail is *for*, and the name is what you read to know which habit you are
 *  punching. The rail is the board's narrow margin and its width is the user's
 *  to set (`--rail-w`), so a name too long for it is truncated on that one line
 *  rather than wrapped — a wrapped name would undo the width they chose. */
export function renderHabitRail() {
  const habits = habitCards();
  if (!habits.length) return null;
  const running = habits.filter((c) => !habitRetired(c));
  const retired = habits.filter(habitRetired);

  const rail = document.createElement('section');
  rail.className = 'habit-rail';
  rail.setAttribute('aria-label', 'Habits');

  // One line: the section's name and how many are due. It was two — a heading
  // over a subtitle — in the panel whose whole job is to be narrow.
  const head = document.createElement('div');
  head.className = 'habit-rail-head';
  const title = document.createElement('h2');
  title.className = 'habit-rail-title';
  title.textContent = '↻ Habits';
  const sub = document.createElement('p');
  sub.className = 'habit-rail-sub';
  const due = running.filter(habitDue).length;
  sub.textContent = running.length ? (due ? `${due} due` : 'All done') : '';
  head.append(title, sub);
  rail.append(head);

  // Four lists, shortest period first, each named by a spine down its left
  // edge. An absent cadence paints nothing at all.
  for (const freq of CADENCE_ORDER) {
    const group = running.filter((c) => c.habitFreq === freq);
    if (group.length) rail.append(cadenceGroup(freq, group));
  }

  if (retired.length) rail.append(retiredFold(retired));
  return rail;
}

/** The cadences, shortest period to longest, and the ink each is drawn in.
 *
 *  Fixed, and not derived from what the board holds: a shortest-period-first
 *  reading is what makes the top of the rail today's work, and an order that
 *  moved with the card count would put a yearly habit above a daily one on the
 *  day you happened to keep three of the first.
 *
 *  The inks are the four `--ink-*` hues styles.css already defines in every
 *  theme, and already uses for exactly this job — `--col-accent` on the three
 *  board columns. A cadence group is the same kind of thing as a column, a
 *  container the cards sit in, so the spine is that idiom applied to a
 *  narrower one and **no token is added**. It is emphatically not a category
 *  colour: colour on this board means category, and that is why the colour is
 *  on the group's edge and never on a row, where it would compete with `--cat`
 *  on the same line. */
const CADENCE_ORDER = ['daily', 'weekly', 'monthly', 'yearly'];
const CADENCE_LABEL = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', yearly: 'Yearly' };

/** One cadence's habits, behind one spine.
 *
 *  The spine is the group's **label**, not decoration — which is why the group
 *  carries `role="group"` and an accessible name saying the cadence. Rotated
 *  glyphs are not an accessible name, and a screen reader has to reach "Daily
 *  habits" rather than an unlabelled run of rows. It is also why there is no
 *  horizontal heading: four of those, in a 200px margin, to name lists that are
 *  often one row long, is the height the 2026-09-07 redraw removed. */
function cadenceGroup(freq, cards) {
  const group = document.createElement('div');
  group.className = 'habit-group';
  group.dataset.cadence = freq;
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', `${CADENCE_LABEL[freq]} habits`);

  const spine = document.createElement('span');
  spine.className = 'habit-spine';
  spine.textContent = CADENCE_LABEL[freq];
  // The spine costs the group width and never height, so on a one-row group
  // the word is clipped to that row — the same rule as a habit's own name,
  // which is clipped rather than wrapped because the rail's width is the
  // user's. The tooltip is what a sighted reader gets back, exactly as it is
  // for a long habit name.
  spine.title = `${CADENCE_LABEL[freq]} habits`;
  // Decoration as far as assistive technology is concerned: the group's
  // aria-label above is the name, and reading the cadence twice would be the
  // same fact said twice.
  spine.setAttribute('aria-hidden', 'true');
  group.append(spine);

  // Due first, finished after — a day's ledger, not a nag list. The rule is
  // unchanged; it applies inside a group now rather than across the whole rail.
  for (const card of [...cards].sort((a, b) => Number(habitDue(b)) - Number(habitDue(a)))) {
    group.append(habitRailRow(card));
  }
  return group;
}

/** One habit, on one line: its boxes, then its name.
 *
 *  No written tally beside them. Where every repetition has a box the boxes
 *  *are* the tally, and `2/3` printed next to three boxes is the same fact
 *  twice; where the strip is capped, `punchStrip` prints the count itself
 *  (`punch-more`), which is the one place it knows something the boxes do not. */
function habitRailRow(card) {
  const row = document.createElement('div');
  row.className = 'habit-rail-row' + (habitDue(card) ? '' : ' done');
  row.style.setProperty('--cat', catColor(card.category));
  if (card.category) row.classList.add('categorized');

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'habit-rail-open';
  name.textContent = card.title;
  // The row is one line and the name is clipped to fit it, so the full name has
  // to be reachable another way — by hover and by screen reader alike.
  name.title = `${card.title} — ${cadenceText(card)} · ${habitTally(card)}`;
  name.setAttribute('aria-label', `Open ${card.title} (${habitTally(card)})`);
  name.addEventListener('click', () => openDialog(card.id));

  if (!habitRetired(card)) row.append(punchStrip(card));
  row.append(name);
  // The card's actions, wherever the card is shown — the 2026-09-06 rule, and
  // now load-bearing rather than a convenience: a habit has no board card and
  // therefore no `+`, so without this its Duplicate, Category, Type, Move to
  // and Delete would be reachable only from the Backlog.
  wireCardContext(row, card.id);
  return row;
}

/** The retired habits, behind a closed line at the rail's foot. Not in the
 *  day's list above it — a habit you have finished with is not today's work —
 *  and not gone either, because it is the one most likely to be looked for
 *  later. Opening one from here and filing it back in the Inbox brings it
 *  round again, history intact. */
function retiredFold(retired) {
  const fold = document.createElement('details');
  fold.className = 'habit-retired';

  const summary = document.createElement('summary');
  summary.className = 'habit-retired-summary';
  summary.textContent = `${retired.length} retired`;
  fold.append(summary);

  for (const card of retired) fold.append(habitRailRow(card));
  return fold;
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
