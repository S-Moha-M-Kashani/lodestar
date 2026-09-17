// The subscriptions the brain answers through, and how to sign one in from
// here (2026-09-17).
//
// A CLI backend spends a subscription that lives *where the brain runs*. On
// this laptop that is the owner's own account and there has never been anything
// to do. In a container it is nobody's: no terminal, no browser, no login — so
// both backends were unreachable there whatever the picker said, which is what
// `docs/cli-backends.md` said outright for months.
//
// `codex login --device-auth` is what closes that, and it is the whole reason
// this file is a button rather than a page of instructions: the CLI prints a
// link and a one-time code and waits, so the container needs no browser of its
// own and no port published to it. The owner opens the link on a device they
// are already holding, and the container ends up signed in.
//
// Claude is the asymmetric half, and the asymmetry belongs to the platform
// rather than to this design: its login is a terminal program, and on a Mac
// what it writes is a keychain entry no container can read. So the one form of
// that credential a container can be *given* is a long-lived token, and this
// panel takes one the way the OpenRouter key row already does — write-only,
// emptied after the save, never read back.

// What each backend is called where a person is reading, and what it can be
// asked to do. `device` is the claim this whole file rests on: a login that can
// be driven from a web page. Claude has none, so it takes a token instead —
// stated here rather than inferred from the brain's answer, because an absent
// capability has to read as a different control and not as a missing one.
const SUBSCRIPTIONS = [
  { backend: 'codex-cli', name: 'Codex', device: true },
  { backend: 'claude-cli', name: 'Claude', device: false },
];
// How often the page asks again while a code is on screen. The answer changes
// exactly once — when the owner approves the code on another device — and it
// has to arrive without them coming back here to press anything, which is what
// makes this a poll rather than a refresh button. Nothing polls when no login
// is in flight: the status costs the brain a subprocess.
const POLL_MS = 1500;
// How long an answer is trusted without asking again. The drawer is rebuilt by
// every repaint of the transcript — several times a second while a turn streams
// — so asking on each one would spawn a subprocess in the brain at that rate;
// asking only once, which is what `known === null` alone would mean, leaves a
// panel that says "not signed in" over a subscription signed in a minute ago
// and never corrects itself, because the fold stays open across drawer opens
// and fires no toggle. Ten seconds is short enough that reopening the drawer is
// always a fresh answer and long enough that a streaming turn costs one.
const SUBS_MAX_AGE = 10_000;

// The brain's last answer, held here rather than in the DOM for the reason the
// OpenRouter key's status is: `renderAssistantTools()` rebuilds this whole
// drawer on every repaint of the transcript, so a value living only on screen
// is wiped under the hand that typed it. null = never asked.
let known = null;
let askedAt = 0;
// What has been typed into the token field and not yet saved — same rule, one
// control along.
let tokenDraft = '';
// Why the last press of Sign in did not start a login. Module state for the
// same reason everything else here is: the drawer is rebuilt under the hand
// that pressed the button.
let refused = '';
let polling = null;

const live = () => document.querySelector('.chat-subs');

/** Ask the brain what it can run and what is signed in.
 *
 *  Written into whichever `.chat-subs` is live rather than into a node this
 *  function closed over: a repaint between the request and the answer leaves
 *  the original detached, and an update painted into a detached node is an
 *  answer nobody sees. The same lesson as `sayStatus` in models.js. */
export async function refreshSubs() {
  // Stamped before the request and again after it: the window has to close the
  // moment the ask is made, or a repaint storm fires one request per repaint
  // for as long as the first is in flight — the bug `probeBrainModels` was
  // measured with on 2026-09-10, whose guard only closed once it resolved.
  askedAt = Date.now();
  try {
    const res = await fetch('/api/agent/cli-auth');
    known = res.ok ? await res.json() : { backends: {}, pending: null, down: true };
  } catch {
    known = { backends: {}, pending: null, down: true };
  }
  askedAt = Date.now();
  refused = '';
  paintSubs();
  // Only while a code is outstanding, and re-armed from here rather than on a
  // fixed interval, so a slow answer cannot stack requests on top of itself.
  clearTimeout(polling);
  polling = known?.pending ? setTimeout(refreshSubs, POLL_MS) : null;
}

function paintSubs() {
  const host = live();
  if (!host) return;
  host.replaceChildren(...subRows());
}

/** The block as it sits in the settings drawer. */
export function renderSubscriptions() {
  const box = document.createElement('div');
  box.className = 'field chat-subs';
  box.replaceChildren(...subRows());
  // Asked on the first paint and again whenever the drawer is reopened, never
  // per render: the status runs `codex login status` in the brain, which is a
  // subprocess, and a streaming transcript repaints several times a turn.
  if (known === null || Date.now() - askedAt > SUBS_MAX_AGE) refreshSubs();
  return box;
}

function subRows() {
  const rows = [document.createElement('span')];
  rows[0].className = 'chat-subs-label';
  rows[0].textContent = 'Your subscriptions';
  if (known?.down) {
    const note = document.createElement('span');
    note.className = 'chat-sub-status';
    note.textContent = 'the brain is unreachable, so it cannot say';
    rows.push(note);
    return rows;
  }
  for (const sub of SUBSCRIPTIONS) {
    rows.push(subRow(sub, known?.backends?.[sub.backend]));
  }
  return rows;
}

function subRow(sub, state) {
  const row = document.createElement('div');
  row.className = 'chat-sub';
  row.dataset.backend = sub.backend;
  const name = document.createElement('span');
  name.className = 'chat-sub-name';
  name.textContent = sub.name;
  const status = document.createElement('span');
  status.className = 'chat-sub-status';
  // The brain's own sentence, verbatim. It knows three states where a tick and
  // a cross know two — signed in, signed out, and *cannot be known*, which is
  // the ordinary answer for a Claude login sitting in a keychain — and inventing
  // a wording here would be this page guessing at a fact it asked for.
  status.textContent = (sub.device && refused)
    || (state ? state.detail : 'asking the brain…');
  row.append(name, status);
  if (!state) return row;
  const pending = known?.pending?.backend === sub.backend ? known.pending : null;
  if (pending) row.append(deviceCode(pending));
  else if (sub.device && state.installed && state.signed_in === false) {
    row.append(signInButton(sub));
  } else if (!sub.device && state.installed) row.append(tokenRow(sub));
  return row;
}

function signInButton(sub) {
  const button = document.createElement('button');
  button.type = 'button';
  button.id = 'codex-signin';
  button.className = 'chat-sub-btn';
  button.textContent = 'Sign in';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const res = await fetch('/api/agent/cli-auth/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: sub.backend }),
      });
      const body = await res.json().catch(() => ({}));
      // A refusal is a sentence the brain wrote for this screen — "codex is not
      // installed where the brain runs" — so it is shown where the status was,
      // rather than becoming a silent button that does nothing when pressed.
      if (res.ok) known = { ...known, pending: body };
      else refused = body.detail || 'the login could not be started';
    } catch {
      refused = 'the brain is unreachable, so the login could not be started';
    }
    paintSubs();
    // Straight into the poll: from here the only thing that can change the
    // answer is the owner approving the code somewhere else entirely.
    clearTimeout(polling);
    polling = known?.pending ? setTimeout(refreshSubs, POLL_MS) : null;
  });
  return button;
}

/** The link to open and the code to type, which is the whole of the flow the
 *  owner sees. A link rather than a printed URL: the point is that they open it
 *  on a device that has a browser, and a URL you have to retype by hand is a
 *  login nobody finishes. */
function deviceCode(pending) {
  const box = document.createElement('span');
  box.className = 'chat-sub-device';
  const link = document.createElement('a');
  link.id = 'codex-link';
  link.href = pending.url;
  link.target = '_blank';
  link.rel = 'noreferrer noopener';
  link.textContent = 'Open the sign-in page';
  const code = document.createElement('code');
  code.id = 'codex-code';
  code.className = 'chat-sub-code';
  code.textContent = pending.code;
  const hint = document.createElement('span');
  hint.className = 'chat-sub-hint';
  hint.textContent = 'and enter this code';
  box.append(link, hint, code);
  return box;
}

/** Claude's half: a password field and a Save, under the OpenRouter key's own
 *  contract — nothing is read back, and the field empties itself once the brain
 *  has the token. */
function tokenRow(sub) {
  const row = document.createElement('span');
  row.className = 'chat-key-row chat-sub-token';
  const input = document.createElement('input');
  input.type = 'password';
  input.id = 'claude-token';
  input.autocomplete = 'off';
  input.placeholder = 'sk-ant-oat01-…';
  input.value = tokenDraft;
  input.addEventListener('input', () => { tokenDraft = input.value; });
  const save = document.createElement('button');
  save.type = 'button';
  save.id = 'claude-token-save';
  save.textContent = 'Save';
  save.addEventListener('click', async () => {
    try {
      await fetch('/api/agent/cli-auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: sub.backend, token: tokenDraft }),
      });
    } catch { /* the refresh below says so */ }
    tokenDraft = '';
    const typed = document.querySelector('#claude-token');
    if (typed) typed.value = '';
    refreshSubs();
  });
  row.append(input, save);
  return row;
}

// Nothing here is persisted, deliberately: a one-time code is spent the moment
// it is approved, and a subscription token belongs to the brain — localStorage
// is the one store on this page that any script can read, which is the argument
// that already keeps the OpenRouter key out of it.
