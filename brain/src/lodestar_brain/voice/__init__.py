"""Voice-to-text: pick an implementation from Settings, never at a call site."""
from ..config import Settings
from .base import SUPPORTED_FORMATS, Transcriber, TranscriptionError
from .fake import FakeTranscriber
from .openrouter import OpenRouterTranscriber

__all__ = ['SUPPORTED_FORMATS', 'FakeTranscriber', 'OpenRouterTranscriber',
           'Transcriber', 'TranscriptionError', 'make_transcriber']


def make_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == 'fake':
        return FakeTranscriber()
    if settings.transcriber in ('auto', 'openrouter'):
        return OpenRouterTranscriber(api_key=settings.openrouter_api_key,
                                     base_url=settings.openrouter_base_url,
                                     default_model=settings.omni_model)
    raise ValueError(f'unknown transcriber: {settings.transcriber!r}; '
                     "expected 'auto', 'openrouter', or 'fake'")
