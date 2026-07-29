"""Local speech-to-text with NVIDIA Parakeet TDT via MLX (Apple Silicon).

Free, offline and private: no API key leaves the machine and no audio does
either. This is the third implementation behind the `Transcriber` Protocol
(invariant #3) — nothing else in the brain changed to accommodate it.

Two deliberate departures from parakeet-mlx's own convenience API:

* **No ffmpeg.** `model.transcribe(path)` shells out to ffmpeg to decode the
  file, which the voice design explicitly refused to take on as a system
  dependency — and which is wasted work here, since the browser already sends
  16 kHz mono PCM. Decoding goes through libsndfile (via librosa, already a
  parakeet-mlx dependency) straight into the model's mel/generate path.
* **No temp file.** The bytes are decoded in memory, so a recording of the
  user's voice is never written to disk for another process to read.

Every mlx/librosa import is lazy, never at module scope: the brain has to boot
on Linux and in Docker where mlx cannot be installed at all. Constructing this
class is therefore safe anywhere; only `transcribe` needs the wheel, and it says
so plainly (`parakeet_available` is the probe, and the error names
`BRAIN_TRANSCRIBER=openrouter` as the fix). `make_transcriber` no longer switches
backends on its own — an environment without mlx must ask for OpenRouter, which
is why `docker-compose.yml` pins it.
"""
import importlib.util
import io
from collections.abc import Callable, Sequence

from .base import TranscriptionError, validate

# The MLX-converted checkpoint of nvidia/parakeet-tdt-0.6b-v3: a single 2.5 GB
# fp32 safetensors file (0.6B params at 4 bytes each), fetched from Hugging Face
# on first use and cached under ~/.cache/huggingface. Only the first dictation
# pays for it; _model_once then keeps the model in memory for the process.
PARAKEET_MODEL = 'mlx-community/parakeet-tdt-0.6b-v3'


def parakeet_available() -> bool:
    """True when the local backend can actually run (Apple Silicon + wheels)."""
    return all(importlib.util.find_spec(mod) is not None
               for mod in ('parakeet_mlx', 'mlx', 'librosa'))


def decode_audio(audio: bytes, sample_rate: int) -> Sequence[float]:
    """Container bytes → mono float samples at `sample_rate`, without ffmpeg.

    librosa reads wav/flac/ogg through libsndfile, which is bundled in the
    soundfile wheel, and resamples and downmixes on the way. Raw pcm16/pcm24
    payloads have no container to read and are rejected here — the browser only
    ever sends WAV, and the OpenRouter backend covers the rest.
    """
    import librosa  # noqa: PLC0415 — see module docstring
    samples, _ = librosa.load(io.BytesIO(audio), sr=sample_rate, mono=True)
    return samples


class _MlxEngine:
    """Samples in, text out. Keeps every mlx import behind one seam."""

    def __init__(self, model):
        self.model = model

    @property
    def sample_rate(self) -> int:
        return self.model.preprocessor_config.sample_rate

    def transcribe_samples(self, samples: Sequence[float]) -> str:
        import mlx.core as mx  # noqa: PLC0415 — see module docstring
        from parakeet_mlx.audio import get_logmel  # noqa: PLC0415
        mel = get_logmel(mx.array(samples), self.model.preprocessor_config)
        return self.model.generate(mel)[0].text


def _load_mlx_model(name: str) -> _MlxEngine:
    from parakeet_mlx import from_pretrained  # noqa: PLC0415
    return _MlxEngine(from_pretrained(name))


class ParakeetTranscriber:
    def __init__(self, model_name: str = PARAKEET_MODEL,
                 load: Callable[[str], object] | None = None,
                 decode: Callable[[bytes, int], Sequence[float]] | None = None):
        self.model_name = model_name
        self._load = load or _load_mlx_model
        self._decode = decode or decode_audio
        self._engine = None

    def _model_once(self):
        """Load on first use and keep it: the checkpoint costs seconds and RAM."""
        if self._engine is None:
            try:
                self._engine = self._load(self.model_name)
            except Exception as exc:
                raise TranscriptionError(
                    f'could not load the local Parakeet model {self.model_name!r}: '
                    f'{exc}. Install it with `uv sync --project brain --extra voice` '
                    '(Apple Silicon only), or set BRAIN_TRANSCRIBER=openrouter.'
                ) from exc
        return self._engine

    def transcribe(self, audio: bytes, fmt: str = 'wav',
                   model: str | None = None) -> str:
        # `model` is whatever the browser's omni picker holds — an OpenRouter
        # slug. A local checkpoint is chosen by configuration, so the remote slug
        # is ignored rather than handed to from_pretrained, which would fail
        # every dictation.
        validate(audio, fmt)
        engine = self._model_once()
        try:
            samples = self._decode(audio, engine.sample_rate)
        except Exception as exc:
            raise TranscriptionError(
                f'could not decode the {fmt} recording: {exc}') from exc
        if len(samples) == 0:
            return ''
        try:
            text = engine.transcribe_samples(samples)
        except Exception as exc:
            raise TranscriptionError(f'local transcription failed: {exc}') from exc
        return (text or '').strip()
