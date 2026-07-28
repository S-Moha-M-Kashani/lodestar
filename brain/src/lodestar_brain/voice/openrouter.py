"""Transcription via any OpenAI-wire-format endpoint that accepts audio input.

Audio rides in as an `input_audio` content part on a normal chat completion, so
this reuses the same wire format (and the same future Ollama swap) as the text
provider — there is no separate transcription API to depend on.
"""
import base64

import httpx

from .base import TranscriptionError, signals_no_audio, validate

INSTRUCTION = (
    'Transcribe the audio verbatim. Reply with the transcript text only — '
    'no commentary, no preamble, no quotation marks. If the audio contains no '
    'speech, reply with nothing at all.'
)


class OpenRouterTranscriber:
    def __init__(self, api_key: str, base_url: str, default_model: str,
                 timeout: float = 90.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.default_model = default_model
        self.timeout = timeout

    def transcribe(self, audio: bytes, fmt: str = 'wav',
                   model: str | None = None) -> str:
        validate(audio, fmt)
        used = model or self.default_model
        payload = {
            'model': used,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': INSTRUCTION},
                {'type': 'input_audio', 'input_audio': {
                    'data': base64.b64encode(audio).decode(),
                    'format': fmt,
                }},
            ]}],
        }
        try:
            res = httpx.post(f'{self.base_url}/chat/completions',
                             headers={'Authorization': f'Bearer {self.api_key}'},
                             json=payload, timeout=self.timeout)
            res.raise_for_status()
            content = res.json()['choices'][0]['message'].get('content')
        except httpx.HTTPError as exc:
            raise TranscriptionError(f'transcription request failed: {exc}') from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise TranscriptionError(f'malformed transcription response: {exc}') from exc
        text = (content or '').strip()
        # A model that answers *about* missing audio was never given the audio:
        # its provider dropped the input_audio part. Returning that answer would
        # file the model's invented apology as the user's own words.
        if text and signals_no_audio(text):
            raise TranscriptionError(
                f'the model {used!r} did not receive the audio it was sent — it '
                f'replied {text!r} instead of a transcript. Some models list '
                'audio input but the provider serving them drops it; pick a '
                'different model for "Audio / photo / video → text".')
        return text
