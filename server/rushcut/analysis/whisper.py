"""Optional local Whisper transcription. Prefers faster-whisper + GPU."""
from __future__ import annotations

import os
from typing import Any


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


def transcribe_media(
    path: str,
    model_size: str = "base",
    language: str | None = "en",
) -> dict[str, Any]:
    """Transcribe with faster-whisper if installed; otherwise soft-fail.

    GPU is used when ctranslate2 reports a CUDA device. Missing extra is a
    soft failure; other analysis tools still work.
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

    device, compute = _pick_device()
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute)
        segments_iter, info = model.transcribe(
            path,
            language=language,
            vad_filter=True,
            word_timestamps=True,
        )
    except Exception as exc:
        # Typical: CUDA requested but no driver — retry CPU.
        if device != "cpu":
            try:
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
                segments_iter, info = model.transcribe(
                    path,
                    language=language,
                    vad_filter=True,
                    word_timestamps=True,
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

    return {
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
