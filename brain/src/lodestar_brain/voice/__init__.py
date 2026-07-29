"""Voice-to-text: pick an implementation from Settings, never at a call site."""
from ..config import Settings
from .base import (SUPPORTED_FORMATS, Transcriber, TranscriptionError,
                   signals_no_audio)
from .fake import FakeTranscriber
from .openrouter import OpenRouterTranscriber
from .parakeet import PARAKEET_MODEL, ParakeetTranscriber, parakeet_available

__all__ = ['PARAKEET_MODEL', 'SUPPORTED_FORMATS', 'FakeTranscriber',
           'OpenRouterTranscriber', 'ParakeetTranscriber', 'Transcriber',
           'TranscriptionError', 'make_transcriber', 'parakeet_available',
           'signals_no_audio']


def make_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == 'fake':
        return FakeTranscriber()
    # No 'auto'. It preferred local Parakeet when mlx was importable and fell to
    # OpenRouter otherwise, so one config transcribed privately on Apple Silicon
    # and billed a paid API on Linux without ever saying which. Each environment
    # names its backend now: the default is 'parakeet' and compose pins
    # 'openrouter', since the brain image cannot install mlx.
    if settings.transcriber == 'parakeet':
        return ParakeetTranscriber(model_name=settings.parakeet_model)
    if settings.transcriber == 'openrouter':
        return OpenRouterTranscriber(api_key=settings.openrouter_api_key,
                                     base_url=settings.openrouter_base_url,
                                     default_model=settings.omni_model)
    raise ValueError(f'unknown transcriber: {settings.transcriber!r}; expected '
                     "'parakeet', 'openrouter', or 'fake'")
