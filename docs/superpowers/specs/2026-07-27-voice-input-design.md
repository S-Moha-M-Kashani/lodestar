# Voice input on the Assistant view

Date: 2026-07-27
Branch: `feature/voice-input`

## Goal

Speak to the assistant instead of typing. Dictation lands in the chat composer
as editable text; nothing is sent until the user presses Send.

Serves two product pillars directly: **reduce mental load** (a thought can be
spoken faster than typed) and **never lose a thought** (the transcript is always
recoverable text in the textarea, never a silent failure).

Scope is the Assistant view only. Voice capture straight into a new card is a
natural follow-on and is deliberately out of scope here.

## Decisions

| Question | Decision | Why |
| --- | --- | --- |
| Where does transcription run? | Browser records → brain → OpenRouter omni model | The `omni` picker ("Audio / photo / video → text") already exists and does nothing yet; the API key must stay in the brain (invariant #5). |
| Fully local Whisper? | Not now, but the seam allows it | `Transcriber` is a Protocol selected by env var, so faster-whisper becomes a third file, not a rewrite (invariant #3). |
| Browser `webkitSpeechRecognition`? | Rejected | Ships audio to Google/Apple, no model control, and cannot be driven offline in Playwright. |
| Mic interaction | Tap to start, tap to stop | Long thoughts are awkward to hold; keyboard-accessible by default. |
| After transcription | Append into `#chat-input`, never auto-send | A misheard word must be fixable before it reaches the agent. |
| Recording ceiling | 90 seconds, auto-stop | The Node body cap is ~5 MB; 16 kHz mono base64 runs ~43 KB/s, so 90 s ≈ 3.8 MB. |

## Data flow

```
mic → getUserMedia → MediaRecorder (webm/opus)
  → AudioContext decode → 16 kHz mono → WAV bytes → base64      [app.js, no deps]
  → POST /api/agent/transcribe { audio, format: 'wav', model }
  → Node /api/agent/* proxy (already generic; no new route)
  → brain POST /agent/transcribe → Transcriber seam
        · OpenRouterTranscriber → chat/completions with an `input_audio` part
        · FakeTranscriber (BRAIN_TRANSCRIBER=fake) → fixed string, offline
  → { text } → appended into #chat-input → user edits → Send
```

### Why the browser encodes WAV

`MediaRecorder` emits `audio/webm;codecs=opus` in Chrome. OpenRouter's audio
input accepts `wav, mp3, aiff, aac, ogg, flac, m4a, pcm16, pcm24` — webm is not
on the list. Decoding to 16 kHz mono WAV with `AudioContext` is ~40 lines of
vanilla JS, is identical across browsers, and shrinks the payload. The
alternative (ffmpeg in the brain) would add a system dependency.

## Components

**`brain/src/lodestar_brain/voice/`** (new)

- `base.py` — `Transcriber` Protocol, `SUPPORTED_FORMATS`, `TranscriptionError`.
- `openrouter.py` — `OpenRouterTranscriber`; builds the `input_audio` payload,
  validates format and non-empty audio before spending a request.
- `fake.py` — `FakeTranscriber`; deterministic string, optional script. Applies
  the same validation as the real one so offline runs can't pass a payload the
  live API would reject.
- `__init__.py` — `make_transcriber(settings)`.

**`config.py`** — two new settings: `transcriber` (`auto|openrouter|fake`, via
`BRAIN_TRANSCRIBER`) and `omni_model` (via `BRAIN_OMNI_MODEL`, defaulting to the
same slug the frontend picker defaults to).

**`server.py`** — `POST /agent/transcribe`, body `{audio, format='wav', model?}`,
returning `{text}`. 400 on malformed base64 / unsupported format / empty audio;
502 on upstream failure. Stateless: it never reads or writes the board, so the
durability promise is untouched.

**`server.js`** — one change: the `/api/agent/*` proxy must distinguish an
oversized body (413 `Payload too large`) from an unreachable brain (503
`assistant unavailable`). Today both surface as 503, which would send the user
hunting a service that is running fine.

**`app.js`** — one self-contained section beside the assistant code (~120 lines):
`voiceState`, `startRecording`, `stopRecording`, `cancelRecording`, `encodeWav`.
Stays in `app.js` to preserve the single-IIFE, no-build-step convention.

## UI

`.chat-mic` sits left of Send in `.chat-composer`. States:

- **idle** — mic glyph, `aria-pressed="false"`.
- **recording** — `.chat-recording` bar with a `--rule-red` pulse, elapsed time,
  `.chat-stop` and `.chat-cancel`. Escape cancels.
- **transcribing** — `Transcribing…` in the existing `.chat-status`.
- **error** — `.chat-voice-error` inline message.

Existing tokens only (`--rule-red`, `--ink`, `--paper`). The mic is hidden
entirely when `MediaRecorder`/`getUserMedia` is absent, and disabled while
`assistantState.busy`. State changes go through the existing `announce()` helper.
The `omni` picker's hint text is corrected — it now drives something real.

## Error handling

| Failure | Behaviour |
| --- | --- |
| Permission denied / no device | "Microphone blocked — check browser permissions." |
| `MediaRecorder` unsupported | Mic button not rendered at all. |
| Brain down (503) | "Couldn't transcribe — the brain service may be off." |
| Oversized recording (413) | "That recording was too long." |
| Empty transcript | "Didn't catch that." Input untouched. |
| Any failure | Whatever was already typed is preserved. |

## Testing

Written before the implementation, per the repo's test-first policy.

- `brain/tests/test_voice.py` — fake transcriber (deterministic, scriptable,
  validating); OpenRouter `input_audio` payload asserted via respx; format and
  empty-audio rejection without spending a request; upstream and transport
  failures wrapped as `TranscriptionError`; `make_transcriber` selection.
- `brain/tests/test_server.py` — `/agent/transcribe` happy path, wav default,
  malformed base64, unsupported format, empty audio, missing field (422), omni
  model passthrough, 502 mapping, and a no-board-traffic assertion.
- `brain/tests/test_config.py` — the two new env vars and their defaults.
- `tests/server.test.js` — proxy forwards the body byte-for-byte to
  `/agent/transcribe` against a stub brain; 503 when the brain is down; 413 for
  an oversized body, never forwarded upstream.
- `tests/e2e_test.py` — Chromium `--use-fake-device-for-media-stream` plus a
  granted mic permission and `BRAIN_TRANSCRIBER=fake` make a real dictation run
  headless and offline: mic present → record → stop → transcript in the
  composer, unsent; Escape cancels; a second take appends; a 503 reports and
  preserves typed text; mic disabled while the assistant is thinking.
