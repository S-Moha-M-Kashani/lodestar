import { assistantModels } from './models.js';
import { assistantState } from './session.js';
import { announce } from '../ui/dom.js';
import { render } from '../ui/render.js';

// Voice input — speak into the composer instead of typing.
//
// MediaRecorder emits webm/opus, which the omni models don't accept, so the
// blob is decoded and re-encoded here as 16 kHz mono WAV (a third of the
// bytes of the 48 kHz source) and posted as base64 to the brain. The
// transcript lands in the composer as editable text and is never auto-sent:
// a misheard word must be fixable before it reaches the agent.

const VOICE_RATE = 16000;      // Hz, mono — plenty for speech
// 90s ≈ 3.8 MB of base64, comfortably inside the server's ~5 MB body cap.
const VOICE_MAX_MS = 90_000;

export const voiceState = {
  phase: 'idle',               // 'idle' | 'recording' | 'transcribing'
  error: '',
  startedAt: 0,
  recorder: null,
  stream: null,
  chunks: [],
  timer: null,
};

export function voiceSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && window.MediaRecorder && (window.AudioContext || window.webkitAudioContext));
}

export function formatElapsed(ms) {
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function releaseMic() {
  if (voiceState.stream) {
    for (const track of voiceState.stream.getTracks()) track.stop();
    voiceState.stream = null;
  }
}

function stopTimer() {
  if (voiceState.timer) clearInterval(voiceState.timer);
  voiceState.timer = null;
}

// Resolve once the recorder has flushed its final chunk.
function flushRecorder() {
  return new Promise((resolve) => {
    const rec = voiceState.recorder;
    if (!rec || rec.state === 'inactive') return resolve();
    rec.addEventListener('stop', () => resolve(), { once: true });
    rec.stop();
  });
}

export async function startRecording() {
  if (voiceState.phase !== 'idle' || assistantState.busy) return;
  voiceState.error = '';
  try {
    voiceState.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    voiceState.error = 'Microphone blocked — check your browser permissions.';
    render();
    announce('Microphone blocked');
    return;
  }
  voiceState.chunks = [];
  const rec = new MediaRecorder(voiceState.stream);
  rec.addEventListener('dataavailable', (event) => {
    if (event.data && event.data.size) voiceState.chunks.push(event.data);
  });
  voiceState.recorder = rec;
  rec.start();
  voiceState.phase = 'recording';
  voiceState.startedAt = Date.now();
  voiceState.timer = setInterval(tickRecording, 250);
  render();
  announce('Recording — press Stop when you are done');
}

function tickRecording() {
  const elapsed = Date.now() - voiceState.startedAt;
  const readout = document.querySelector('.chat-elapsed');
  if (readout) readout.textContent = formatElapsed(elapsed);
  // Stop ourselves rather than let the payload grow past what the server takes.
  if (elapsed >= VOICE_MAX_MS) stopRecording();
}

export async function cancelRecording() {
  if (voiceState.phase !== 'recording') return;
  stopTimer();
  await flushRecorder();
  releaseMic();
  voiceState.chunks = [];
  voiceState.recorder = null;
  voiceState.phase = 'idle';
  voiceState.error = '';
  render();
  announce('Recording discarded');
}

export async function stopRecording() {
  if (voiceState.phase !== 'recording') return;
  stopTimer();
  // mimeType is only meaningful once recording has started.
  const type = voiceState.recorder.mimeType || 'audio/webm';
  await flushRecorder();
  releaseMic();
  const blob = new Blob(voiceState.chunks, { type });
  voiceState.chunks = [];
  voiceState.recorder = null;
  voiceState.phase = 'transcribing';
  render();

  try {
    const audio = await encodeWav(blob);
    if (!audio) {
      throw explained('Nothing was recorded — check that your microphone is picking up sound.');
    }
    const res = await fetch('/api/agent/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio, format: 'wav', model: assistantModels.omni }),
    });
    if (res.status === 413) {
      throw explained('That recording was too long to transcribe — try a shorter one.');
    }
    if (!res.ok) {
      // The brain names the real cause — an omni model whose provider dropped
      // the audio, a payload it refused. Flattening every failure into "the
      // brain is down" sent the user debugging a service that was running fine.
      // A 503 comes from our own proxy and really does mean unreachable.
      const detail = res.status === 503 ? '' : await failureDetail(res);
      throw explained(detail
        ? `Couldn’t transcribe that — ${detail}`
        : 'Couldn’t transcribe that — check that the brain service is running.');
    }
    const data = await res.json();
    const text = (data.text || '').trim();
    if (!text) {
      voiceState.error = 'Didn’t catch that — nothing was transcribed.';
      announce('Nothing was transcribed');
    } else {
      appendToDraft(text);
      announce('Transcript added to the composer');
    }
  } catch (err) {
    voiceState.error = (err && err.userMessage)
      || 'Couldn’t transcribe that — check that the brain service is running.';
    announce('Transcription failed');
  }

  voiceState.phase = 'idle';
  render();
  const input = document.getElementById('chat-input');
  if (input) {
    input.focus();
    input.selectionStart = input.selectionEnd = input.value.length;
  }
}

// A failure we can already put in words, as opposed to an unexpected JS or
// network error, which falls back to the generic message.
function explained(message) {
  const err = new Error(message);
  err.userMessage = message;
  return err;
}

// FastAPI reports `detail`, our own Node proxy reports `error`.
async function failureDetail(res) {
  try {
    const body = await res.json();
    const detail = body && (body.detail || body.error);
    return typeof detail === 'string' ? detail : '';
  } catch {
    return '';
  }
}

// Dictation adds to the draft rather than replacing it — never lose a thought.
function appendToDraft(text) {
  const current = assistantState.draft;
  assistantState.draft = current && !/\s$/.test(current)
    ? `${current} ${text}`
    : `${current}${text}`;
}

// ---- WAV encoding (no dependencies) --------------------------------------

async function encodeWav(blob) {
  if (!blob.size) return '';
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  let buffer;
  try {
    buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
  } finally {
    ctx.close();
  }
  const samples = resample(downmix(buffer), buffer.sampleRate, VOICE_RATE);
  if (!samples.length) return '';
  return base64FromBytes(wavBytes(samples, VOICE_RATE));
}

function downmix(buffer) {
  const channels = buffer.numberOfChannels;
  const mono = new Float32Array(buffer.length);
  for (let c = 0; c < channels; c += 1) {
    const data = buffer.getChannelData(c);
    for (let i = 0; i < mono.length; i += 1) mono[i] += data[i];
  }
  if (channels > 1) for (let i = 0; i < mono.length; i += 1) mono[i] /= channels;
  return mono;
}

function resample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const pos = i * ratio;
    const low = Math.floor(pos);
    const high = Math.min(low + 1, input.length - 1);
    out[i] = input[low] + (input[high] - input[low]) * (pos - low);
  }
  return out;
}

function wavBytes(samples, rate) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const dv = new DataView(bytes);
  const ascii = (at, text) => {
    for (let i = 0; i < text.length; i += 1) dv.setUint8(at + i, text.charCodeAt(i));
  };
  ascii(0, 'RIFF');
  dv.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  dv.setUint32(16, 16, true);        // fmt chunk size
  dv.setUint16(20, 1, true);         // PCM
  dv.setUint16(22, 1, true);         // mono
  dv.setUint32(24, rate, true);
  dv.setUint32(28, rate * 2, true);  // byte rate
  dv.setUint16(32, 2, true);         // block align
  dv.setUint16(34, 16, true);        // bits per sample
  ascii(36, 'data');
  dv.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    // Clamp: browsers can hand back samples slightly outside [-1, 1].
    const s = Math.max(-1, Math.min(1, samples[i]));
    dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(bytes);
}

function base64FromBytes(bytes) {
  // Chunked so a long recording can't blow the argument limit.
  const CHUNK = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

// Escape discards the take in progress. Registered once; harmless otherwise.
