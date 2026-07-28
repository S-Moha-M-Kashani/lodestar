"""The voice-to-text seam.

Like every other module in the brain, transcription is substitutable: pick an
implementation with BRAIN_TRANSCRIBER, don't edit call sites. The local Parakeet
backend arrived exactly that way — a third implementation, not a rewrite.
"""
import re
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


# ---- "the audio never arrived" detection ----------------------------------
# Some models advertise audio input but the provider serving them silently drops
# the input_audio part. The model then answers about the *absence* of audio, and
# because that reply is non-empty text the frontend would paste it into the
# composer as if the user had dictated it.
#
# These patterns are deliberately tight and mostly anchored to the start of the
# reply. A false positive costs one retry with the user's draft preserved; a
# false positive on a real transcript would silently replace a genuine thought
# with an error, which the durability pillar forbids. So when in doubt, let it
# through: only shapes that an assistant (never a dictating human) would produce
# are matched.
_SORRY = r"(?:i'm sorry[,.!]?\s*)?(?:but\s+)?"

_NO_AUDIO_PATTERNS = (
    # A bracketed placeholder standing in for output: "(No output)".
    re.compile(r'^\((?:no|none)\b[^)]*\)$'),
    # "there is no audio provided", "no audio file was provided/attached".
    re.compile(r'\bno audio\b[^.]{0,30}\b(?:provided|attached|included|supplied'
               r'|given|detected|found|present|received)\b'),
    # "I cannot hear the audio", "I don't have any audio to transcribe".
    re.compile(rf"^{_SORRY}i (?:cannot|can't|am unable to|do not|don't) "
               r'(?:hear|access|listen to|process|receive|see|find|have)\b'
               r'[^.!?]*\baudio\b'),
    # "I need the audio file to transcribe it."
    re.compile(rf'^{_SORRY}i (?:need|require)\b[^.!?]*\baudio\b[^.!?]*'
               r'\bto transcribe\b'),
)


def signals_no_audio(text: str) -> bool:
    """True when a reply means "I was given no audio" rather than a transcript."""
    reply = ' '.join(text.replace('’', "'").lower().split())
    return any(p.search(reply) for p in _NO_AUDIO_PATTERNS)
