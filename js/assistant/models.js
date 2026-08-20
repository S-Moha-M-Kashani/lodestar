import { KEY_PREFIX } from '../core/keys.js';
import { view } from '../core/state.js';
import { render } from '../ui/render.js';

// Model pickers — which brain answers, and with what. Every model choice is a
// labelled dropdown rather than a constant, and the panel folds away because a
// model is picked once and then left alone.

const MODELS_KEY = KEY_PREFIX + 'models';
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
const modelRoute = (slug) =>
  (slug.startsWith('4skl/') || !slug.includes('/')) ? 'local' : 'OpenRouter API';
export const assistantModels = { ...DEFAULT_MODELS };
assistantModels.provider = 'ollama';
const TEXT_MODELS_BY_PROVIDER = {
  ollama: MODEL_PICKERS[0].options,
  openrouter: ['openai/gpt-5-nano'],
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
export const brainModels = { provider: '', verified: false, models: [], default: '' };

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
  if (!savedTextProvider && (brainModels.provider === 'ollama'
      || brainModels.provider === 'openrouter')) {
    assistantModels.provider = brainModels.provider;
    if (!pickerOptions(MODEL_PICKERS[0]).includes(assistantModels.text)) {
      assistantModels.text = pickerOptions(MODEL_PICKERS[0])[0];
    }
    persistModels();
  }
  if (!answered || !brainModels.verified || !brainModels.models.length) return;
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
  if (changed && view === 'assistant') render();
}

// The options for one picker: the backend's own list when it verified one,
// otherwise the presets. Only the text pick is served by the chat model, so
// the omni picker keeps its curated list either way.
function pickerOptions(picker) {
  if (picker.key === 'text') {
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
  if (saved.provider === 'ollama' || saved.provider === 'openrouter') {
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
  name.textContent = `Models · ${assistantModels.text}`;
  panel.appendChild(name);
  const fields = document.createElement('div');
  fields.className = 'chat-settings-body';
  const providerLabel = document.createElement('label');
  providerLabel.className = 'field';
  providerLabel.append('Text provider');
  const provider = document.createElement('select');
  provider.id = 'model-provider';
  for (const [value, label] of [['ollama', 'Ollama — local, free & private'],
                                ['openrouter', 'OpenRouter — remote API']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
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
      opt.textContent = `${slug} (${modelRoute(slug)})`;
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
  // "stored"/"none yet" rather than anything containing "set": the word is the
  // save's confirmation, and a resting label that already matched it would let
  // a reader (or the e2e) mistake the old state for the new.
  const sayStatus = (configured) => {
    keyStatus.textContent = configured ? 'a key is stored' : 'none yet';
  };
  const refreshKeyStatus = () => {
    fetch('/api/agent/key').then((r) => r.json())
      .then((d) => sayStatus(d.configured))
      .catch(() => { keyStatus.textContent = 'brain unreachable'; });
  };
  if (settingsOpen) refreshKeyStatus();
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
      keyStatus.textContent = d.configured ? 'key set' : 'none yet';
    } catch {
      keyStatus.textContent = 'brain unreachable';
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
