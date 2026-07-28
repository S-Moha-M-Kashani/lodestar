"""Deterministic offline transcriber for unit tests, e2e, and CI.

Scripted mode pops pre-baked transcripts in order; otherwise every recording
comes back as FAKE_TRANSCRIPT. Validation matches the real implementation so an
offline run can't pass audio the live API would reject.
"""
from .base import validate

FAKE_TRANSCRIPT = 'FAKE TRANSCRIPT: hello from the microphone'


class FakeTranscriber:
    def __init__(self, script: list[str] | None = None):
        self.script = list(script) if script is not None else None

    def transcribe(self, audio: bytes, fmt: str = 'wav',
                   model: str | None = None) -> str:
        validate(audio, fmt)
        if self.script is not None:
            return self.script.pop(0)
        return FAKE_TRANSCRIPT
