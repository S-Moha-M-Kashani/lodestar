"""Voice-to-text: the Transcriber seam and its two implementations.

Fully offline. The OpenRouter implementation is pinned against respx so the
`input_audio` wire format is asserted without ever leaving the machine.
"""
import base64
import json

import httpx
import pytest
import respx

from lodestar_brain.config import Settings
from lodestar_brain.voice import make_transcriber
from lodestar_brain.voice.base import SUPPORTED_FORMATS, TranscriptionError
from lodestar_brain.voice.fake import FAKE_TRANSCRIPT, FakeTranscriber
from lodestar_brain.voice.openrouter import OpenRouterTranscriber

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


# ---- The seam: selection by settings (invariant #3) ----------------------

def test_make_transcriber_selects_fake():
    assert isinstance(make_transcriber(Settings(transcriber='fake')), FakeTranscriber)


def test_make_transcriber_defaults_to_openrouter():
    made = make_transcriber(Settings(openrouter_api_key='sk-test', omni_model=OMNI))
    assert isinstance(made, OpenRouterTranscriber)
    assert made.default_model == OMNI


def test_make_transcriber_rejects_unknown_choice():
    with pytest.raises(ValueError):
        make_transcriber(Settings(transcriber='whisper-on-a-toaster'))
