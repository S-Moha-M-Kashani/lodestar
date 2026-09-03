import { widgetShowing } from './shell.js';
import { boardSuffix } from '../core/boards.js';
import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';
import { render } from '../ui/render.js';

// Model pickers — which brain answers, and with what. Every model choice is a
// labelled dropdown rather than a constant, and the panel folds away because a
// model is picked once and then left alone.

// Per board, because the backend is now one of the things a board can differ
// on: two people sharing one endpoint, each answering through their own CLI
// subscription, is the reason the CLI options below exist. The default board's
// key stays unsuffixed (`boardSuffix` is '' there), so nobody's existing pick
// moves — and a new board starts on the local default rather than inheriting a
// subscription that is not its owner's.
const MODELS_KEY = KEY_PREFIX + 'models' + boardSuffix;
// Every omni option must genuinely receive audio at a sane price. Free
// dictation is the local Parakeet backend's job (BRAIN_TRANSCRIBER
// defaults to it).
const DEFAULT_MODELS = {
  // Local-first is the normal Assistant experience. Nano stays one click
  // away under the explicit OpenRouter provider selector below. The omni
  // pick is the remote route by definition — local dictation is Parakeet's
  // job inside the brain, which ignores this pick entirely.
  text: '4skl/gemma4-e2b-mtp',
  omni: 'google/gemini-2.5-flash-lite',
};
// The one embedder the brain actually runs — shown in the panel, never picked.
const FIXED_EMBEDDER = 'heydariAI/persian-embeddings';
const MODEL_PICKERS = [
  { key: 'text', id: 'model-text', label: 'Text generation',
    options: [DEFAULT_MODELS.text, 'gemma4:e2b', 'deepseek-r1:8b'] },
  { key: 'omni', id: 'model-omni', label: 'Audio → text (route: OpenRouter API)',
    options: [DEFAULT_MODELS.omni, 'openai/gpt-audio-mini'] },
];
// The two backends that answer through a CLI this computer has already logged
// in to. No API key is involved and none is wanted: the subscription is the
// credential, and it lives in the binary rather than in this repo.
const CLI_PROVIDERS = ['claude-cli', 'codex-cli'];
const PROVIDER_LABELS = {
  ollama: 'Ollama — local, free & private',
  openrouter: 'OpenRouter — remote API',
  'claude-cli': 'Claude CLI — your own subscription',
  'codex-cli': 'Codex CLI — your own subscription',
};
// Where a slug runs, for the label. A CLI slug names neither a local daemon nor
// a billed API, so it cannot be read off the slug's shape the way the other two
// can — the provider has to say.
const modelRoute = (slug, provider) => {
  if (CLI_PROVIDERS.includes(provider)) return 'your subscription';
  return (slug.startsWith('4skl/') || !slug.includes('/')) ? 'local' : 'OpenRouter API';
};
// Codex is deliberately given no model to pick: the brain never passes it `-m`,
// because the choice made was "whatever codex defaults to", and a slug offered
// here would claim to have chosen something.
const CODEX_DEFAULT = '';
const codexLabel = (slug) => slug || "codex's own default";
export const assistantModels = { ...DEFAULT_MODELS };
assistantModels.provider = 'ollama';
const TEXT_MODELS_BY_PROVIDER = {
  ollama: MODEL_PICKERS[0].options,
  openrouter: ['openai/gpt-5-nano'],
  // Aliases rather than dated ids on purpose: `claude --model` takes them, and
  // they keep pointing at the current model instead of freezing on the one that
  // was current the day this line was written.
  'claude-cli': ['sonnet', 'opus', 'haiku'],
  'codex-cli': [CODEX_DEFAULT],
};
// What the brain says it can actually serve. Empty until asked, and only a
// local backend ever answers with a list (see served_models in the brain):
// OpenRouter is a paid API with hundreds of models, so nothing is probed there
// and the curated list above stands.
//
// This exists because the text pick rides on every chat request. With
// BRAIN_LLM=ollama the brain forwards that slug to a daemon that cannot load
// `openai/gpt-5-nano`, so every turn would fail with a picker offering no
// way out — an unservable pick has to be deselected, not merely delisted.
export const brainModels = { provider: '', verified: false, models: [], default: '',
                             // Which CLI subscriptions the brain's machine can
                             // actually run. Empty until asked, and a backend
                             // missing from it is never offered: handing the
                             // brain a CLI it has no binary for would fail every
                             // turn with no way out from here.
                             cli: {} };

/** The provider options to offer: the two API routes always, and a CLI backend
 *  only when the brain says it can serve it. The current pick is kept even when
 *  unavailable, so a saved choice still shows as selected while the probe is in
 *  flight rather than silently reading as something else. */
function providerOptions() {
  const offered = ['ollama', 'openrouter',
                   ...CLI_PROVIDERS.filter((name) => brainModels.cli?.[name])];
  return offered.includes(assistantModels.provider)
    ? offered : [...offered, assistantModels.provider];
}

export async function probeBrainModels() {
  let answered = false;
  try {
    const res = await fetch('/api/agent/models');
    if (res.ok) {
      Object.assign(brainModels, await res.json());
      answered = true;
    }
  } catch { /* brain down — the presets stand, and chat will say so itself */ }
  // A configured OpenRouter brain is still an explicit remote choice, but it
  // is a useful initial value for a fresh browser profile. Once saved, the
  // person's picker choice wins over a later server configuration change.
  if (!savedTextProvider && PROVIDER_LABELS[brainModels.provider]) {
    assistantModels.provider = brainModels.provider;
    if (!pickerOptions(MODEL_PICKERS[0]).includes(assistantModels.text)) {
      assistantModels.text = pickerOptions(MODEL_PICKERS[0])[0];
    }
    persistModels();
  }
  // A saved CLI backend this brain can no longer serve is switched off rather
  // than left to fail on the next turn — the same rule the model list follows
  // below, one backend further out. Only when the brain actually answered: a
  // brain that is down reports nothing, and nothing is not "your subscription
  // is gone".
  if (answered && CLI_PROVIDERS.includes(assistantModels.provider)
      && !brainModels.cli?.[assistantModels.provider]) {
    assistantModels.provider = 'ollama';
    assistantModels.text = pickerOptions(MODEL_PICKERS[0])[0];
    persistModels();
    if (view === 'assistant' || widgetShowing()) render();
    return;
  }
  if (!answered || !brainModels.verified || !brainModels.models.length) return;
  // The daemon's tag list governs a pick that is going to the daemon, and only
  // that. Without this guard the list below rewrites the text pick whenever the
  // *brain* is Ollama, however the picker is set — so choosing Claude CLI and
  // reloading left `sonnet` replaced by an Ollama tag and sent to `claude`,
  // which cannot load it. Found by running it. The same was already true of an
  // OpenRouter pick on an Ollama brain; one guard covers both, because the
  // question is what the pick will be sent to, not what the brain booted as.
  if (assistantModels.provider !== 'ollama') return;
  // The backend named its models, so an unservable text pick is switched to
  // one that works rather than left to fail on the next turn.
  let changed = false;
  if (!brainModels.models.includes(assistantModels.text)) {
    assistantModels.text = brainModels.models.includes(brainModels.default)
      ? brainModels.default : brainModels.models[0];
    persistModels();
    changed = true;
  }
  // Only when the answer changed something. Re-rendering unconditionally would
  // loop: the render triggers the probe that triggers the render.
  if (changed && (view === 'assistant' || widgetShowing())) render();
}

// The options for one picker: the backend's own list when it verified one,
// otherwise the presets. Only the text pick is served by the chat model, so
// the omni picker keeps its curated list either way.
function pickerOptions(picker) {
  if (picker.key === 'text') {
    if (CLI_PROVIDERS.includes(assistantModels.provider)) {
      return TEXT_MODELS_BY_PROVIDER[assistantModels.provider];
    }
    if (assistantModels.provider === 'openrouter') return TEXT_MODELS_BY_PROVIDER.openrouter;
    if (brainModels.provider === 'ollama' && brainModels.verified && brainModels.models.length) {
      return brainModels.models;
    }
    return TEXT_MODELS_BY_PROVIDER.ollama;
  }
  return picker.options;
}
const persistModels = () => {
  try { localStorage.setItem(MODELS_KEY, JSON.stringify(assistantModels)); }
  catch { /* private mode — the pick still applies to this session */ }
};
let savedTextProvider = false;
try {
  const saved = JSON.parse(localStorage.getItem(MODELS_KEY) || '{}');
  for (const k of Object.keys(DEFAULT_MODELS)) {
    if (typeof saved[k] !== 'string' || !saved[k]) continue;
    assistantModels[k] = saved[k];
  }
  // A CLI backend is remembered like any other. Whether the brain can still
  // serve it is a separate question, answered by the probe — not here, where
  // nothing has been asked yet.
  if (PROVIDER_LABELS[saved.provider]) {
    assistantModels.provider = saved.provider;
    savedTextProvider = true;
  }
} catch { /* corrupted or private mode — keep defaults */ }

// Whether the Models panel inside the extras drawer is unfolded. Kept here
// rather than read off the DOM: choosing a provider re-renders the whole view,
// and a panel that refolded itself after every pick would be unusable.
// Deliberately not persisted — it is where you are in the page, not a
// preference. The drawer around it is `extrasOpen`, which belongs to the
// control that opens it (ui/tools.js).
let settingsOpen = false;

// What the drawer knows about the brain's OpenRouter key — the rendered status
// text, kept here so a repaint shows the same answer without asking again.
// '' = never asked.
let keyKnown = '';

/** The composer footer's model pick — the same choice the ⚙ drawer offers,
 *  where the question is actually asked.
 *
 *  Two controls, one source of truth: both read `assistantModels.text` and both
 *  write it through `persistModels`, so neither can show a model the other is
 *  not using. The re-render is what keeps the drawer's own summary line ("Models
 *  · gemma…") honest after a pick made down here.
 *
 *  Native `<select>`, and labelled by title rather than by a visible label: the
 *  footer is one line under a textarea, and a second word of chrome on it would
 *  cost the composer more than it explains. When the brain has not answered yet
 *  the curated presets stand, so the control always names a real model rather
 *  than sitting empty. */
export function renderModelPicker({ busy = false } = {}) {
  const sel = document.createElement('select');
  sel.id = 'chat-model';
  sel.className = 'composer-model';
  sel.setAttribute('aria-label', 'Model answering this chat');
  sel.title = 'Which model answers — the ⚙ settings hold the rest';
  const offered = pickerOptions(MODEL_PICKERS[0]);
  const opts = offered.includes(assistantModels.text)
    ? offered : [assistantModels.text, ...offered];
  for (const slug of opts) {
    const opt = document.createElement('option');
    opt.value = slug;
    opt.textContent = codexLabel(slug);
    sel.append(opt);
  }
  sel.value = assistantModels.text;
  sel.disabled = busy;
  sel.addEventListener('change', () => {
    assistantModels.text = sel.value;
    persistModels();
    render();
  });
  return sel;
}

// Folded away by default, like the evidence strip under a reply: a model is
// chosen once and then left alone, and four pickers open in the rail is a wall
// of controls nobody is using. The summary names the model that is answering,
// which is the question you would open the panel to ask.
export function renderChatSettings() {
  const panel = document.createElement('details');
  panel.className = 'chat-settings';
  panel.open = settingsOpen;
  panel.addEventListener('toggle', () => { settingsOpen = panel.open; });
  const name = document.createElement('summary');
  name.className = 'chat-settings-name';
  name.textContent = `Models · ${codexLabel(assistantModels.text)}`;
  panel.appendChild(name);
  const fields = document.createElement('div');
  fields.className = 'chat-settings-body';
  const providerLabel = document.createElement('label');
  providerLabel.className = 'field';
  providerLabel.append('Text provider');
  const provider = document.createElement('select');
  provider.id = 'model-provider';
  for (const value of providerOptions()) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = PROVIDER_LABELS[value] || value;
    provider.append(opt);
  }
  provider.value = assistantModels.provider;
  provider.addEventListener('change', () => {
    assistantModels.provider = provider.value;
    const options = pickerOptions(MODEL_PICKERS[0]);
    if (!options.includes(assistantModels.text)) assistantModels.text = options[0];
    savedTextProvider = true;
    persistModels();
    render();
  });
  providerLabel.append(provider);
  fields.appendChild(providerLabel);
  for (const picker of MODEL_PICKERS) {
    const label = document.createElement('label');
    label.className = 'field';
    label.append(picker.label);
    const sel = document.createElement('select');
    sel.id = picker.id;
    // A previously saved slug that left the preset list still deserves to
    // show as selected, so it becomes an extra option instead of vanishing.
    const offered = pickerOptions(picker);
    const opts = offered.includes(assistantModels[picker.key])
      ? offered : [assistantModels[picker.key], ...offered];
    for (const slug of opts) {
      const opt = document.createElement('option');
      opt.value = slug;
      opt.textContent =
        `${codexLabel(slug)} (${modelRoute(slug, assistantModels.provider)})`;
      sel.append(opt);
    }
    sel.value = assistantModels[picker.key];
    sel.addEventListener('change', () => {
      assistantModels[picker.key] = sel.value;
      persistModels();
    });
    label.appendChild(sel);
    fields.appendChild(label);
  }
  // The embedder is a fact, not a pick: the brain runs exactly one model,
  // locally. A filled-in ledger cell where the dropdown used to stand —
  // same footprint as the selects, no affordance — so nobody hunts for a
  // control that shouldn't exist.
  const embedField = document.createElement('div');
  embedField.className = 'field model-fixed';
  embedField.id = 'model-embed-fixed';
  embedField.append('Text → embedding (route: local, fixed)');
  const embedValue = document.createElement('span');
  embedValue.className = 'model-fixed-value';
  const embedName = document.createElement('span');
  embedName.textContent = FIXED_EMBEDDER;
  const embedStamp = document.createElement('span');
  embedStamp.className = 'model-fixed-stamp';
  embedStamp.textContent = 'built-in';
  embedValue.append(embedName, embedStamp);
  const embedNote = document.createElement('span');
  embedNote.className = 'model-fixed-note';
  embedNote.textContent = 'Multilingual — English and Farsi · embeds your cards inside the brain, never remote';
  embedField.append(embedValue, embedNote);
  fields.appendChild(embedField);
  const hint = document.createElement('p');
  hint.className = 'field-hint';
  hint.textContent = 'Text generation applies to the chat. Ollama uses models pulled on this machine; OpenRouter currently offers GPT-5 Nano and requires an API key. The omni model transcribes your voice, unless the brain is dictating locally with Parakeet — that ignores this pick.';
  fields.appendChild(hint);
  // The OpenRouter key, typed here instead of edited into the brain's env.
  // Write-only end to end: a password field that empties itself once the brain
  // has the key, a status that only ever says yes or no, and nothing kept in
  // localStorage — the one store this page has is one any script could read.
  const keyLabel = document.createElement('label');
  keyLabel.className = 'field';
  keyLabel.append('OpenRouter API key');
  const keyRow = document.createElement('span');
  keyRow.className = 'chat-key-row';
  const keyInput = document.createElement('input');
  keyInput.type = 'password';
  keyInput.id = 'openrouter-key';
  keyInput.autocomplete = 'off';
  keyInput.placeholder = 'sk-or-…';
  const keySave = document.createElement('button');
  keySave.type = 'button';
  keySave.id = 'openrouter-key-save';
  keySave.textContent = 'Save';
  const keyStatus = document.createElement('span');
  keyStatus.className = 'chat-key-status';
  // The status is rendered from module state (`keyConfigured`) and written to
  // whichever status span is live — anything can repaint the drawer between
  // the click and the brain's answer (the transcript's render does), and a
  // handler that writes only its closed-over span updates a detached node.
  // Fetched at deliberate moments only — first need, reopening the panel, and
  // the save itself — never per render: a fetch on every repaint of a
  // streaming transcript is a request storm that spends the assistant
  // surface's own rate budget.
  const sayStatus = () => {
    const el = document.querySelector('.chat-key-status') || keyStatus;
    el.textContent = keyKnown;
  };
  const learnKey = (text) => { keyKnown = text; sayStatus(); };
  const refreshKeyStatus = () => {
    fetch('/api/agent/key').then((r) => r.json())
      .then((d) => learnKey(d.configured ? 'a key is set' : 'none yet'))
      .catch(() => learnKey('brain unreachable'));
  };
  sayStatus();
  if (settingsOpen && keyKnown === '') refreshKeyStatus();
  panel.addEventListener('toggle', () => { if (panel.open) refreshKeyStatus(); });
  keySave.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/agent/key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: keyInput.value }),
      });
      const d = await res.json();
      keyInput.value = '';
      learnKey(res.ok ? (d.configured ? 'a key is set' : 'none yet')
                      : 'brain unreachable');
    } catch {
      learnKey('brain unreachable');
    }
  });
  keyRow.append(keyInput, keySave, keyStatus);
  keyLabel.appendChild(keyRow);
  fields.appendChild(keyLabel);
  // Where the chat model runs, when the brain told us. Worth saying out loud:
  // a local backend is free and private but answers in tens of seconds, and
  // the list above is then the daemon's, not ours.
  if (brainModels.verified && brainModels.models.length) {
    const where = document.createElement('p');
    where.className = 'field-hint';
    where.textContent = `The configured backend serves local models through ${brainModels.provider} — free and private, and the list is whatever is pulled on this machine.`;
    fields.appendChild(where);
  }
  panel.appendChild(fields);
  return panel;
}
