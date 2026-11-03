"""Optional local Whisper transcription. Prefers faster-whisper + GPU."""
from __future__ import annotations

import os
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg


def _pick_device() -> tuple[str, str]:
    """Return (device, compute_type). Degrade: cuda → cpu int8."""
    forced = (
        os.environ.get("NLE_WHISPER_DEVICE")
        or os.environ.get("VEGAS_WHISPER_DEVICE")
        or ""
    ).strip().lower()
    compute_env = os.environ.get("NLE_WHISPER_COMPUTE") or os.environ.get("VEGAS_WHISPER_COMPUTE")
    if forced in {"cpu", "cuda", "auto"}:
        if forced == "cpu":
            return "cpu", compute_env or "int8"
        if forced == "cuda":
            return "cuda", compute_env or "float16"
    try:
        import ctranslate2  # type: ignore

        if ctranslate2.get_cuda_device_count() > 0:  # type: ignore[attr-defined]
            return "cuda", compute_env or "float16"
    except Exception:
        pass
    return "cpu", compute_env or "int8"


def offset_transcript(result: dict[str, Any], offset_seconds: float) -> dict[str, Any]:
    """Add offset_seconds to every word and segment time (source-absolute)."""
    if not offset_seconds:
        return result
    shift = float(offset_seconds)

    def _shift_item(item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        for key in ("start_seconds", "end_seconds"):
            if key in out and out[key] is not None:
                out[key] = round(float(out[key]) + shift, 3)
        return out

    out = dict(result)
    out["segments"] = [_shift_item(s) for s in (result.get("segments") or [])]
    out["words"] = [_shift_item(w) for w in (result.get("words") or [])]
    if result.get("duration_seconds") is not None:
        out["duration_seconds"] = round(float(result["duration_seconds"]) + shift, 3)
    out["slice_offset_seconds"] = round(shift, 3)
    return out


def _extract_audio_slice(
    path: str,
    start_seconds: float | None,
    end_seconds: float | None,
) -> str:
    """Decode a time range to a 16 kHz mono wav for Whisper. Caller deletes it."""
    start = float(start_seconds or 0.0)
    if start < 0:
        raise FfmpegError("start_seconds must be >= 0")
    if end_seconds is not None and float(end_seconds) <= start:
        raise FfmpegError("end_seconds must be greater than start_seconds")
    tmp = tempfile.NamedTemporaryFile(prefix="rushcut-whisper-", suffix=".wav", delete=False)
    tmp.close()
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if end_seconds is not None:
        cmd += ["-t", f"{float(end_seconds) - start:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", tmp.name]
    try:
        run(cmd, timeout=180)
    except FfmpegError:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise
    return tmp.name


def transcribe_media(
    path: str,
    model_size: str = "base",
    language: str | None = "en",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    initial_prompt: str | None = None,
) -> dict[str, Any]:
    """Transcribe with faster-whisper if installed; otherwise soft-fail.

    GPU is used when ctranslate2 reports a CUDA device. Missing extra is a
    soft failure; other analysis tools still work.

    start_seconds / end_seconds transcribe a slice and return source-absolute
    word times (see #27). Word timestamps are always requested.
    ``initial_prompt`` is forwarded for vocabulary (names, jargon).
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "error": (
                "faster-whisper is not installed. "
                "pip install 'rushcut-mcp[whisper]' or pip install faster-whisper"
            ),
            "available": False,
        }

    work_path = path
    tmp_slice: str | None = None
    offset = float(start_seconds or 0.0) if start_seconds is not None or end_seconds is not None else 0.0
    if start_seconds is not None or end_seconds is not None:
        try:
            tmp_slice = _extract_audio_slice(path, start_seconds, end_seconds)
            work_path = tmp_slice
        except FfmpegError as exc:
            return {"ok": False, "error": f"slice extract failed: {exc}", "available": True}

    device, compute = _pick_device()
    extra: dict[str, Any] = {}
    if initial_prompt:
        extra["initial_prompt"] = initial_prompt
    try:
        try:
            model = WhisperModel(model_size, device=device, compute_type=compute)
            segments_iter, info = model.transcribe(
                work_path,
                language=language,
                vad_filter=True,
                word_timestamps=True,
                **extra,
            )
        except Exception as exc:
            # Typical: CUDA requested but no driver — retry CPU.
            if device != "cpu":
                try:
                    model = WhisperModel(model_size, device="cpu", compute_type="int8")
                    segments_iter, info = model.transcribe(
                        work_path,
                        language=language,
                        vad_filter=True,
                        word_timestamps=True,
                        **extra,
                    )
                    device, compute = "cpu", "int8"
                except Exception as exc2:
                    return {"ok": False, "error": f"whisper failed: {exc2}", "available": True}
            else:
                return {"ok": False, "error": f"whisper failed: {exc}", "available": True}

        segments: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []
        texts: list[str] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            texts.append(text)
            item = {
                "start_seconds": round(float(seg.start or 0), 3),
                "end_seconds": round(float(seg.end or 0), 3),
                "text": text,
            }
            segments.append(item)
            for w in getattr(seg, "words", None) or []:
                words.append(
                    {
                        "start_seconds": round(float(w.start or 0), 3),
                        "end_seconds": round(float(w.end or 0), 3),
                        "word": (w.word or "").strip(),
                    }
                )

        result: dict[str, Any] = {
            "ok": True,
            "available": True,
            "path": os.path.abspath(path),
            "device": device,
            "compute_type": compute,
            "model": model_size,
            "language": getattr(info, "language", language),
            "duration_seconds": getattr(info, "duration", None),
            "text": " ".join(t for t in texts if t).strip(),
            "segments": segments,
            "words": words,
        }
        if start_seconds is not None:
            result["start_seconds"] = float(start_seconds)
        if end_seconds is not None:
            result["end_seconds"] = float(end_seconds)
        if offset:
            result = offset_transcript(result, offset)
            result["path"] = os.path.abspath(path)
        return result
    finally:
        if tmp_slice:
            try:
                os.remove(tmp_slice)
            except OSError:
                pass
