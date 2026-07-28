"""The voice-to-text seam.

Like every other module in the brain, transcription is substitutable: pick an
implementation with BRAIN_TRANSCRIBER, don't edit call sites. A local
faster-whisper backend belongs here as a third implementation, not as a rewrite.
"""
from typing import Protocol

# Formats OpenRouter accepts for an `input_audio` content part. MediaRecorder's
# native webm/opus is deliberately absent from this list — the browser decodes
# and re-encodes to 16 kHz mono WAV before sending.
SUPPORTED_FORMATS = frozenset({
    'wav', 'mp3', 'aiff', 'aac', 'ogg', 'flac', 'm4a', 'pcm16', 'pcm24',
})


class TranscriptionError(RuntimeError):
    """The backend was reached and refused, or answered unintelligibly.

    Distinct from ValueError, which means the *caller* handed us something
    unusable and no request was ever spent.
    """


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, fmt: str = 'wav',
                   model: str | None = None) -> str:
        ...


def validate(audio: bytes, fmt: str) -> None:
    """Reject locally what the backend would reject remotely, so the offline
    path can never green-light a payload the live API refuses."""
    if not audio:
        raise ValueError('audio is empty')
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f'unsupported audio format: {fmt!r}; '
                         f'expected one of {sorted(SUPPORTED_FORMATS)}')
