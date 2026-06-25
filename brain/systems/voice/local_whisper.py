"""Local CPU speech-to-text using faster-whisper (CTranslate2).

This is an optional provider. faster-whisper is not part of the slim production
image, so every entry point imports it lazily and degrades to a clear
``AudioTranscriptionError`` when it is missing. The existing OpenAI dictation
path never imports this module.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from brain.systems.voice.transcription import AudioTranscriptionError, AudioTranscriptionResult

LOCAL_WHISPER_MODEL_SIZES = ("tiny", "base", "small")
DEFAULT_LOCAL_WHISPER_MODEL_SIZE = "base"
LOCAL_WHISPER_TRANSPORT = "faster_whisper"
LOCAL_WHISPER_INSTALL_DETAIL = (
    "Local voice dictation needs faster-whisper. It ships with Illospace; "
    "rebuild or update the API image (or run `pip install faster-whisper`) if this persists."
)

# WhisperModel instances are expensive to build (first use downloads weights),
# so cache one per model size. faster-whisper transcription is CPU-bound and not
# safe to run concurrently on a shared model, so a single lock serialises calls.
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = asyncio.Lock()


def local_whisper_available() -> bool:
    """Return True when faster-whisper can be imported in this runtime."""

    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def _normalize_model_size(value: object) -> str:
    size = str(value or "").strip().lower()
    return size if size in LOCAL_WHISPER_MODEL_SIZES else DEFAULT_LOCAL_WHISPER_MODEL_SIZE


def _whisper_language(value: object) -> str | None:
    language = str(value or "auto").strip().lower()
    return language if language in {"en", "fr"} else None


def _model_download_root() -> str:
    override = os.getenv("ILLO_VOICE_MODEL_DIR")
    if override:
        root = Path(override)
    else:
        from brain.kernel.config import PRIVATE_HOME

        root = Path(PRIVATE_HOME) / "voice-models"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _load_model(model_size: str) -> object:
    cached = _MODEL_CACHE.get(model_size)
    if cached is not None:
        return cached
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # pragma: no cover - import guard
        raise AudioTranscriptionError(LOCAL_WHISPER_INSTALL_DETAIL) from exc
    try:
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            download_root=_model_download_root(),
        )
    except Exception as exc:
        raise AudioTranscriptionError(f"Could not load local Whisper model '{model_size}': {exc}") from exc
    _MODEL_CACHE[model_size] = model
    return model


def _transcribe_sync(model: object, path: str, language: str | None) -> tuple[str, str]:
    segments, info = model.transcribe(  # type: ignore[attr-defined]
        path,
        language=language,
        vad_filter=True,
    )
    transcript = "".join(segment.text for segment in segments)
    detected = getattr(info, "language", None) or language or "auto"
    return transcript.strip(), str(detected)


async def async_transcribe_local_audio_path(
    path: str | Path,
    *,
    language: str = "auto",
    model_size: str = DEFAULT_LOCAL_WHISPER_MODEL_SIZE,
) -> AudioTranscriptionResult:
    size = _normalize_model_size(model_size)
    whisper_language = _whisper_language(language)
    audio_path = str(path)

    async with _MODEL_LOCK:
        model = await asyncio.to_thread(_load_model, size)
        try:
            transcript, detected = await asyncio.to_thread(
                _transcribe_sync, model, audio_path, whisper_language
            )
        except AudioTranscriptionError:
            raise
        except Exception as exc:
            raise AudioTranscriptionError(f"Local Whisper transcription failed: {exc}") from exc

    if not transcript:
        raise AudioTranscriptionError("Local Whisper did not produce any transcript from the audio.")

    return AudioTranscriptionResult(
        transcript=transcript,
        language=detected,
        provider="local",
        model=f"faster-whisper-{size}",
        transport=LOCAL_WHISPER_TRANSPORT,
    )
