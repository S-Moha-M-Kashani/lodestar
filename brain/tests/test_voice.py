"""Voice-to-text: the Transcriber seam and its three implementations.

Fully offline. The OpenRouter implementation is pinned against respx so the
`input_audio` wire format is asserted without ever leaving the machine; the
local Parakeet implementation takes an injected loader so no mlx wheel, no
600 MB checkpoint and no Apple Silicon are needed to test it.
"""
import base64
import io
import json
import math
import os
import struct
import tempfile
import wave

import httpx
import pytest
import respx

from lodestar_brain.config import Settings
from lodestar_brain.voice import make_transcriber
from lodestar_brain.voice.base import SUPPORTED_FORMATS, TranscriptionError
from lodestar_brain.voice.fake import FAKE_TRANSCRIPT, FakeTranscriber
from lodestar_brain.voice.openrouter import OpenRouterTranscriber
from lodestar_brain.voice.parakeet import PARAKEET_MODEL, ParakeetTranscriber

AUDIO = b'RIFF....WAVEfake-pcm-bytes'
OMNI = 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free'


def openrouter_reply(text):
    return httpx.Response(200, json={'choices': [{'message': {'content': text}}]})


def transcriber():
    return OpenRouterTranscriber(api_key='sk-test',
                                 base_url='https://openrouter.ai/api/v1',
                                 default_model=OMNI)


# ---- Fake implementation --------------------------------------------------

def test_fake_transcriber_returns_deterministic_text():
    assert FakeTranscriber().transcribe(AUDIO, 'wav') == FAKE_TRANSCRIPT


def test_fake_transcriber_is_scriptable():
    fake = FakeTranscriber(script=['first thought', 'second thought'])
    assert fake.transcribe(AUDIO, 'wav') == 'first thought'
    assert fake.transcribe(AUDIO, 'wav') == 'second thought'


def test_fake_transcriber_still_validates_format():
    # The offline path must reject what the real one rejects, or e2e/CI would
    # happily pass a payload that fails against the live API.
    with pytest.raises(ValueError):
        FakeTranscriber().transcribe(AUDIO, 'webm')


def test_fake_transcriber_rejects_empty_audio():
    with pytest.raises(ValueError):
        FakeTranscriber().transcribe(b'', 'wav')


# ---- OpenRouter implementation -------------------------------------------

@respx.mock
def test_openrouter_builds_input_audio_payload():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply('what should I do about the visa paperwork'))
    text = transcriber().transcribe(AUDIO, 'wav')

    assert text == 'what should I do about the visa paperwork'
    sent = route.calls.last.request
    assert sent.headers['authorization'] == 'Bearer sk-test'
    payload = json.loads(sent.content)
    assert payload['model'] == OMNI
    parts = payload['messages'][-1]['content']
    audio_parts = [p for p in parts if p['type'] == 'input_audio']
    text_parts = [p for p in parts if p['type'] == 'text']
    assert len(audio_parts) == 1
    assert audio_parts[0]['input_audio'] == {
        'data': base64.b64encode(AUDIO).decode(), 'format': 'wav'}
    # A text part must tell the model to transcribe rather than answer.
    assert text_parts and 'transcribe' in text_parts[0]['text'].lower()


@respx.mock
def test_openrouter_honours_format_and_model_override():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply('hi'))
    transcriber().transcribe(AUDIO, 'mp3', model='google/gemini-2.5-flash')
    payload = json.loads(route.calls.last.request.content)
    assert payload['model'] == 'google/gemini-2.5-flash'
    assert payload['messages'][-1]['content'][-1]['input_audio']['format'] == 'mp3'


@respx.mock
def test_openrouter_strips_whitespace_and_tolerates_empty_reply():
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply('  padded transcript \n'))
    assert transcriber().transcribe(AUDIO, 'wav') == 'padded transcript'

    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply(None))
    assert transcriber().transcribe(AUDIO, 'wav') == ''


@respx.mock
def test_openrouter_rejects_unsupported_format_without_calling_out():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions')
    # MediaRecorder's native webm/opus is NOT accepted by OpenRouter; the
    # browser converts to wav first. Fail loudly instead of burning a request.
    with pytest.raises(ValueError):
        transcriber().transcribe(AUDIO, 'webm')
    assert not route.called


@respx.mock
def test_openrouter_rejects_empty_audio_without_calling_out():
    route = respx.post('https://openrouter.ai/api/v1/chat/completions')
    with pytest.raises(ValueError):
        transcriber().transcribe(b'', 'wav')
    assert not route.called


@respx.mock
def test_openrouter_wraps_upstream_failure():
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=httpx.Response(500, json={'error': 'model exploded'}))
    with pytest.raises(TranscriptionError):
        transcriber().transcribe(AUDIO, 'wav')


@respx.mock
def test_openrouter_wraps_transport_failure():
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        side_effect=httpx.ConnectError('no route'))
    with pytest.raises(TranscriptionError):
        transcriber().transcribe(AUDIO, 'wav')


def test_supported_formats_match_the_openrouter_docs():
    assert SUPPORTED_FORMATS == frozenset(
        {'wav', 'mp3', 'aiff', 'aac', 'ogg', 'flac', 'm4a', 'pcm16', 'pcm24'})


# ---- The audio-dropped guard ---------------------------------------------
# nvidia/nemotron-3-nano-omni-...:free is advertised by OpenRouter as accepting
# audio, but the provider serving it silently discards the input_audio part: a
# 106 KB WAV arrived as 30 prompt tokens and the model's own reasoning said
# "there is no audio provided". It then invents an apology, which is non-empty
# text — so without a guard the frontend pastes "I'm sorry, but I need the audio
# file to transcribe it." into the composer as if the user had dictated it.
#
# The guard must be conservative: a false positive costs the user a retry (their
# typed draft is always preserved), but a false positive on a *real* transcript
# would silently swallow a genuine thought, which the durability pillar forbids.

UNHEARD_REPLIES = [
    '(No output)',
    '(No transcript available)',
    '(no speech detected)',
    "I'm sorry, but I need the audio file to transcribe it.",
    'I need an audio recording to transcribe.',
    'There is no audio provided.',
    'No audio file was provided.',
    'no audio was attached',
    "I cannot hear the audio you are referring to.",
    "I can't access the audio.",
    "I don't have any audio to transcribe.",
]


@pytest.mark.parametrize('reply', UNHEARD_REPLIES)
@respx.mock
def test_openrouter_rejects_a_reply_that_means_the_audio_never_arrived(reply):
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply(reply))
    with pytest.raises(TranscriptionError) as caught:
        transcriber().transcribe(AUDIO, 'wav')
    # The message has to name the real problem — the model, not the brain.
    assert 'audio' in str(caught.value).lower()
    assert OMNI in str(caught.value)


# Real dictations that merely *talk about* audio, hearing or absence must survive
# the guard untouched. These are the false positives that would lose a thought.
REAL_TRANSCRIPTS = [
    "I can't hear the music in my flat — should I complain to the landlord?",
    'Remind me to send the audio file to Sam before Friday.',
    'No audio in the video I recorded yesterday, ask the shop about it.',
    'Why do I need an audio interface for the synth setup?',
    'I need the audio cable from the drawer, and a new adapter.',
    'Should I transcribe my therapy notes or is that a bad idea?',
    'no',
    'Audio.',
]


@pytest.mark.parametrize('reply', REAL_TRANSCRIPTS)
@respx.mock
def test_openrouter_passes_real_transcripts_through_the_guard(reply):
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply(reply))
    assert transcriber().transcribe(AUDIO, 'wav') == reply


@respx.mock
def test_an_empty_reply_is_still_empty_not_an_error():
    # Silence is not a failure: the frontend says "Didn't catch that" and leaves
    # the composer alone. Only a reply that *claims there was no audio* raises.
    respx.post('https://openrouter.ai/api/v1/chat/completions').mock(
        return_value=openrouter_reply('   '))
    assert transcriber().transcribe(AUDIO, 'wav') == ''


# ---- Local Parakeet implementation ---------------------------------------
# nvidia/parakeet-tdt-0.6b-v3 via parakeet-mlx: free, offline, no API key.
#
# parakeet-mlx's own `model.transcribe(path)` shells out to **ffmpeg** to decode
# the file, which the voice design deliberately refused to depend on — and which
# is pure waste here, because the browser already sends 16 kHz mono PCM. So the
# audio is decoded with libsndfile (via librosa, already a parakeet-mlx
# dependency) and handed to the model's own mel/generate path instead.
#
# Both seams — loading the checkpoint and decoding the audio — are injectable, so
# these tests need neither mlx, nor librosa, nor a 2.5 GB download.

SAMPLES = [0.0, 0.25, -0.25, 0.5]      # stands in for decoded PCM


class FakeParakeetEngine:
    """Stands in for a loaded model: raw samples in, text out."""

    def __init__(self, text='hello from the microphone', sample_rate=16000):
        self.text = text
        self.sample_rate = sample_rate
        self.seen = []                  # samples handed over, per call

    def transcribe_samples(self, samples):
        self.seen.append(list(samples))
        return self.text


def parakeet(engine=None, decode=None, **kwargs):
    """A ParakeetTranscriber on fake seams; returns (transcriber, load_calls)."""
    loaded = engine if engine is not None else FakeParakeetEngine()
    calls = []

    def load(name):
        calls.append(name)
        return loaded

    def fake_decode(audio, sample_rate):
        return SAMPLES

    return ParakeetTranscriber(load=load, decode=decode or fake_decode,
                               **kwargs), calls


def test_parakeet_transcribes_audio_bytes():
    t, _ = parakeet(FakeParakeetEngine('  what should I do about the visa  '))
    assert t.transcribe(AUDIO, 'wav') == 'what should I do about the visa'


def test_parakeet_decodes_at_the_rate_the_model_expects():
    # Feeding a 16 kHz model 48 kHz samples would transcribe gibberish, so the
    # decode step must be told the model's own rate, not a hardcoded one.
    engine = FakeParakeetEngine(sample_rate=22050)
    asked = []

    def decode(audio, sample_rate):
        asked.append((audio, sample_rate))
        return SAMPLES

    t, _ = parakeet(engine, decode=decode)
    t.transcribe(AUDIO, 'wav')
    assert asked == [(AUDIO, 22050)]


def test_parakeet_hands_the_model_the_decoded_samples():
    engine = FakeParakeetEngine()
    t, _ = parakeet(engine)
    t.transcribe(AUDIO, 'wav')
    assert engine.seen == [SAMPLES]


def test_parakeet_never_writes_the_recording_to_disk():
    # The user's voice is decoded in memory. Nothing to leave behind, nothing to
    # clean up, and no temp file for another process to read.
    before = set(os.listdir(tempfile.gettempdir()))
    t, _ = parakeet()
    t.transcribe(AUDIO, 'wav')
    assert not {f for f in set(os.listdir(tempfile.gettempdir())) - before
                if 'lodestar' in f or 'voice' in f}


def test_parakeet_reports_silence_rather_than_asking_the_model():
    # An empty decode means the recording held no samples at all.
    engine = FakeParakeetEngine()
    t, _ = parakeet(engine, decode=lambda audio, rate: [])
    assert t.transcribe(AUDIO, 'wav') == ''
    assert engine.seen == []


def test_parakeet_wraps_a_decode_failure():
    def decode(audio, sample_rate):
        raise RuntimeError('libsndfile cannot read this')

    t, _ = parakeet(decode=decode)
    with pytest.raises(TranscriptionError) as caught:
        t.transcribe(AUDIO, 'wav')
    assert 'decode' in str(caught.value).lower()


# The real decoder, exercised for real: a WAV built here must come back as the
# samples that went in. Skipped where librosa is absent (Linux, Docker, CI),
# which is exactly where the OpenRouter backend is used instead.
def test_the_real_decoder_reads_a_wav_without_ffmpeg():
    pytest.importorskip('librosa')
    from lodestar_brain.voice.parakeet import decode_audio

    rate, tone = 16000, []
    for i in range(rate):                      # 1 second of 440 Hz
        tone.append(math.sin(2 * math.pi * 440 * i / rate))
    raw = b''.join(struct.pack('<h', int(s * 32767)) for s in tone)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(raw)

    samples = decode_audio(buffer.getvalue(), rate)
    assert len(samples) == rate, 'one second at 16 kHz is 16000 samples'
    assert max(abs(float(s)) for s in samples) > 0.9, 'the tone was lost'


def test_the_real_decoder_resamples_to_the_models_rate():
    # A 48 kHz recording (what MediaRecorder captures natively) has to come out
    # at the model's rate or the transcript is nonsense.
    pytest.importorskip('librosa')
    from lodestar_brain.voice.parakeet import decode_audio

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b''.join(struct.pack('<h', 0) for _ in range(48000)))
    assert len(decode_audio(buffer.getvalue(), 16000)) == 16000


def test_the_real_decoder_downmixes_stereo():
    pytest.importorskip('librosa')
    from lodestar_brain.voice.parakeet import decode_audio

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b''.join(struct.pack('<hh', 100, -100)
                                 for _ in range(16000)))
    samples = decode_audio(buffer.getvalue(), 16000)
    assert len(samples) == 16000, 'stereo must collapse to one channel'


def test_parakeet_does_not_load_the_checkpoint_until_it_is_needed():
    _, calls = parakeet()
    assert calls == [], 'constructing the transcriber must not download 600 MB'


def test_parakeet_loads_the_checkpoint_once_across_calls():
    t, calls = parakeet()
    t.transcribe(AUDIO, 'wav')
    t.transcribe(AUDIO, 'wav')
    assert calls == [PARAKEET_MODEL], 'the model must be cached, not reloaded'


def test_parakeet_uses_the_mlx_community_checkpoint_by_default():
    assert PARAKEET_MODEL == 'mlx-community/parakeet-tdt-0.6b-v3'


def test_parakeet_ignores_a_remote_model_slug():
    # The browser always sends its omni picker value (an OpenRouter slug). A
    # local backend must ignore it — trying to load 'google/gemini-2.5-flash-lite'
    # as an MLX checkpoint would fail every single dictation.
    t, calls = parakeet()
    t.transcribe(AUDIO, 'wav', model='google/gemini-2.5-flash-lite')
    assert calls == [PARAKEET_MODEL]


def test_parakeet_honours_an_explicitly_configured_checkpoint():
    t, calls = parakeet(model_name='mlx-community/parakeet-tdt-1.1b')
    t.transcribe(AUDIO, 'wav')
    assert calls == ['mlx-community/parakeet-tdt-1.1b']


def test_parakeet_validates_format_before_loading_anything():
    t, calls = parakeet()
    with pytest.raises(ValueError):
        t.transcribe(AUDIO, 'webm')
    assert calls == []


def test_parakeet_rejects_empty_audio_before_loading_anything():
    t, calls = parakeet()
    with pytest.raises(ValueError):
        t.transcribe(b'', 'wav')
    assert calls == []


def test_parakeet_wraps_a_missing_mlx_install_as_transcription_error():
    def load(name):
        raise ImportError("No module named 'mlx'")

    with pytest.raises(TranscriptionError) as caught:
        ParakeetTranscriber(load=load).transcribe(AUDIO, 'wav')
    assert 'parakeet' in str(caught.value).lower()


def test_parakeet_wraps_an_inference_failure():
    class Exploding:
        sample_rate = 16000

        def transcribe_samples(self, samples):
            raise RuntimeError('mlx kernel died')

    t, _ = parakeet(Exploding())
    with pytest.raises(TranscriptionError):
        t.transcribe(AUDIO, 'wav')


def test_parakeet_returns_empty_string_when_nothing_was_said():
    t, _ = parakeet(FakeParakeetEngine(''))
    assert t.transcribe(AUDIO, 'wav') == ''


def test_parakeet_module_imports_without_mlx_present():
    # Importing the module must never pull mlx in — the brain has to boot on
    # Linux and in Docker, where mlx cannot be installed at all.
    from lodestar_brain.voice import parakeet as mod
    assert isinstance(mod.parakeet_available(), bool)


# ---- The seam: selection by settings (invariant #3) ----------------------

def test_make_transcriber_selects_fake():
    assert isinstance(make_transcriber(Settings(transcriber='fake')), FakeTranscriber)


def test_make_transcriber_selects_parakeet():
    made = make_transcriber(Settings(transcriber='parakeet'))
    assert isinstance(made, ParakeetTranscriber)


def test_make_transcriber_passes_the_configured_checkpoint_to_parakeet():
    made = make_transcriber(Settings(transcriber='parakeet',
                                     parakeet_model='mlx-community/parakeet-tdt-1.1b'))
    assert made.model_name == 'mlx-community/parakeet-tdt-1.1b'


def test_make_transcriber_rejects_auto(monkeypatch):
    # 'auto' picked Parakeet when mlx was importable and OpenRouter otherwise, so
    # the same config transcribed locally on a Mac and billed an API on Linux —
    # invisibly. The backend is now named outright; each environment pins it.
    monkeypatch.setattr('lodestar_brain.voice.parakeet_available', lambda: True)
    with pytest.raises(ValueError):
        make_transcriber(Settings(transcriber='auto', openrouter_api_key='sk-test'))


def test_make_transcriber_parakeet_ignores_availability(monkeypatch):
    # An explicit pick is honoured even where mlx is missing: the failure then
    # surfaces at transcribe time with a real reason, instead of being papered
    # over by a silent switch to a paid API.
    monkeypatch.setattr('lodestar_brain.voice.parakeet_available', lambda: False)
    assert isinstance(make_transcriber(Settings(transcriber='parakeet')),
                      ParakeetTranscriber)


def test_make_transcriber_openrouter_is_explicit_and_ignores_local_availability(monkeypatch):
    monkeypatch.setattr('lodestar_brain.voice.parakeet_available', lambda: True)
    made = make_transcriber(Settings(transcriber='openrouter', openrouter_api_key='sk-test'))
    assert isinstance(made, OpenRouterTranscriber)


def test_make_transcriber_rejects_unknown_choice():
    with pytest.raises(ValueError):
        make_transcriber(Settings(transcriber='whisper-on-a-toaster'))
