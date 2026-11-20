"""Optional local Whisper transcription. Prefers faster-whisper + GPU."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from typing import Any

from ..output_policy import check_output_path
from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import attach_container_warnings, probe_media

DEFAULT_CHUNK_S = 600.0
DEFAULT_OVERLAP_S = 30.0
DEFAULT_DRIFT_S = 0.25
GPU_MODEL = "large-v3-turbo"
CPU_MODEL = "small"

# Tests replace this to avoid loading Whisper.
TranscribeRangeFn = Callable[..., dict[str, Any]]
_TRANSCRIBE_HOOK: TranscribeRangeFn | None = None


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


def default_model_for_device(device: str | None = None) -> tuple[str, str, str | None]:
    """GPU → large/turbo; CPU → small int8. Warning is set on CPU."""
    picked, compute = _pick_device() if device is None else (
        device,
        "int8" if device == "cpu" else "float16",
    )
    if picked == "cuda":
        return GPU_MODEL, compute, None
    return CPU_MODEL, "int8", "CPU transcription uses the small int8 model; timings drift more on long files"


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


def _whisper_file(
    work_path: str,
    *,
    model_size: str,
    language: str | None,
    initial_prompt: str | None,
) -> dict[str, Any]:
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
    extra: dict[str, Any] = {}
    if initial_prompt:
        extra["initial_prompt"] = initial_prompt
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
    return {
        "ok": True,
        "available": True,
        "device": device,
        "compute_type": compute,
        "model": model_size,
        "language": getattr(info, "language", language),
        "duration_seconds": getattr(info, "duration", None),
        "text": " ".join(t for t in texts if t).strip(),
        "segments": segments,
        "words": words,
    }


def transcribe_range(
    path: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    *,
    model_size: str = "base",
    language: str | None = "en",
    initial_prompt: str | None = None,
) -> dict[str, Any]:
    """Transcribe [start, end) and return source-absolute times."""
    if _TRANSCRIBE_HOOK is not None:
        return _TRANSCRIBE_HOOK(
            path,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            model_size=model_size,
            language=language,
            initial_prompt=initial_prompt,
        )
    work_path = path
    tmp_slice: str | None = None
    offset = float(start_seconds or 0.0) if start_seconds is not None or end_seconds is not None else 0.0
    if start_seconds is not None or end_seconds is not None:
        try:
            tmp_slice = _extract_audio_slice(path, start_seconds, end_seconds)
            work_path = tmp_slice
        except FfmpegError as exc:
            return {"ok": False, "error": f"slice extract failed: {exc}", "available": True}
    try:
        result = _whisper_file(
            work_path,
            model_size=model_size,
            language=language,
            initial_prompt=initial_prompt,
        )
        if not result.get("ok"):
            return result
        result["path"] = os.path.abspath(path)
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


def _file_fingerprint(path: str) -> str:
    digest = hashlib.sha256()
    size = os.path.getsize(path)
    digest.update(str(size).encode("utf-8"))
    try:
        digest.update(str(os.path.getmtime(path)).encode("utf-8"))
    except OSError:
        pass
    with open(path, "rb") as fh:
        if size <= 32 * 1024 * 1024:
            digest.update(fh.read())
        else:
            digest.update(fh.read(64 * 1024))
            if size > 64 * 1024:
                fh.seek(max(0, size - 64 * 1024))
                digest.update(fh.read())
    return digest.hexdigest()


def _cache_key(
    path: str,
    *,
    model_size: str,
    language: str | None,
    initial_prompt: str | None,
    start_seconds: float | None,
    end_seconds: float | None,
    chunk_seconds: float,
    overlap_seconds: float,
    drift_threshold_s: float,
) -> str:
    payload = {
        "fingerprint": _file_fingerprint(path),
        "model": model_size,
        "language": language,
        "initial_prompt": initial_prompt or "",
        "start": start_seconds,
        "end": end_seconds,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "drift_threshold_s": drift_threshold_s,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"{key}.json")


def _read_cache(cache_dir: str, key: str) -> dict[str, Any] | None:
    dest = _cache_path(cache_dir, key)
    if not os.path.isfile(dest):
        return None
    try:
        with open(dest, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("ok"):
        data["cache_hit"] = True
        return data
    return None


def _write_cache(cache_dir: str, key: str, result: dict[str, Any]) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    dest = _cache_path(cache_dir, key)
    payload = dict(result)
    payload.pop("cache_hit", None)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, dest)


def write_transcript_json(result: dict[str, Any], json_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(json_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    return os.path.abspath(json_path)


def _chunk_windows(
    duration: float,
    start: float,
    end: float,
    chunk_seconds: float,
    overlap_seconds: float,
) -> list[tuple[float, float]]:
    if duration <= 0:
        return [(start, end)]
    length = end - start
    if length <= chunk_seconds + 1e-6:
        return [(start, end)]
    step = max(1.0, chunk_seconds - overlap_seconds)
    windows: list[tuple[float, float]] = []
    t = start
    while t < end - 1e-6:
        stop = min(end, t + chunk_seconds)
        windows.append((round(t, 3), round(stop, 3)))
        if stop >= end:
            break
        t += step
    return windows


def _stitch_chunk_words(
    chunks: list[dict[str, Any]],
    overlap_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    texts: list[str] = []
    for index, chunk in enumerate(chunks):
        start = float(chunk.get("start_seconds") or 0)
        end = float(chunk.get("end_seconds") or start)
        next_start = (
            float(chunks[index + 1]["start_seconds"])
            if index + 1 < len(chunks)
            else None
        )
        cut = end
        if next_start is not None and overlap_seconds > 0:
            cut = min(end, (end + next_start) / 2.0)
        keep_from = start if index == 0 else (start + end) / 2.0 if overlap_seconds else start
        if index == 0:
            keep_from = start
        else:
            prev_end = float(chunks[index - 1].get("end_seconds") or start)
            keep_from = (prev_end + start) / 2.0 if overlap_seconds else start
        for word in chunk.get("words") or []:
            mid = (float(word.get("start_seconds") or 0) + float(word.get("end_seconds") or 0)) / 2.0
            if index + 1 < len(chunks) and mid >= cut:
                continue
            if index > 0 and mid < keep_from:
                continue
            words.append(dict(word))
        for seg in chunk.get("segments") or []:
            mid = (float(seg.get("start_seconds") or 0) + float(seg.get("end_seconds") or 0)) / 2.0
            if index + 1 < len(chunks) and mid >= cut:
                continue
            if index > 0 and mid < keep_from:
                continue
            segments.append(dict(seg))
        text = str(chunk.get("text") or "").strip()
        if text:
            texts.append(text)
    words.sort(key=lambda w: float(w.get("start_seconds") or 0))
    segments.sort(key=lambda s: float(s.get("start_seconds") or 0))
    return words, segments, " ".join(texts).strip()


def _word_key(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip().casefold()


def _drift_check(
    path: str,
    words: list[dict[str, Any]],
    chunk_start: float,
    chunk_end: float,
    *,
    model_size: str,
    language: str | None,
    initial_prompt: str | None,
    sample_count: int = 4,
) -> dict[str, Any]:
    timed = [w for w in words if _word_key(w)]
    if not timed:
        return {"ok": True, "max_drift_s": 0.0, "samples": 0, "rerun": False}
    mid = timed[len(timed) // 2 : len(timed) // 2 + sample_count] or timed[:sample_count]
    drifts: list[float] = []
    for word in mid[:sample_count]:
        claimed = float(word.get("start_seconds") or 0)
        start = max(chunk_start, claimed - 6.0)
        end = min(chunk_end, float(word.get("end_seconds") or claimed) + 6.0)
        if end - start < 0.4:
            end = min(chunk_end, start + 1.2)
        aligned = transcribe_range(
            path,
            start,
            end,
            model_size=model_size,
            language=language,
            initial_prompt=initial_prompt,
        )
        if not aligned.get("ok"):
            continue
        target = _word_key(word)
        match = None
        for other in aligned.get("words") or []:
            if _word_key(other) == target:
                match = other
                break
        if match is None and aligned.get("words"):
            match = aligned["words"][0]
        if match is None:
            continue
        drifts.append(abs(float(match.get("start_seconds") or 0) - float(word.get("start_seconds") or 0)))
    max_drift = max(drifts) if drifts else 0.0
    return {
        "ok": True,
        "max_drift_s": round(max_drift, 3),
        "samples": len(drifts),
        "rerun": False,
    }


def transcribe_media(
    path: str,
    model_size: str = "base",
    language: str | None = "en",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    initial_prompt: str | None = None,
    *,
    chunk_seconds: float = DEFAULT_CHUNK_S,
    overlap_seconds: float = DEFAULT_OVERLAP_S,
    drift_threshold_s: float = DEFAULT_DRIFT_S,
    cache_dir: str | None = None,
    no_cache: bool = False,
    json_path: str | None = None,
    run_dir: str | None = None,
    lexicon: list[str] | None = None,
) -> dict[str, Any]:
    """Transcribe with faster-whisper if installed; otherwise soft-fail.

    GPU is used when ctranslate2 reports a CUDA device. Missing extra is a
    soft failure; other analysis tools still work.

    start_seconds / end_seconds transcribe a slice and return source-absolute
    word times (see #27). Long files are chunked (default 10 min, 30 s
    overlap) and stitched on word boundaries. A drift check re-aligns a
    sample of words per chunk and re-runs any chunk over the threshold.
    Times stay absolute to the source. ``initial_prompt`` / lexicon words
    are forwarded for vocabulary.
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}

    # Everything this call can write, checked before any work: the transcript,
    # and the cache, which lives in cache_dir, else in run_dir.
    transcript_dest = json_path or (os.path.join(str(run_dir), "transcript.json") if run_dir else None)
    writes: list[tuple[str | None, str]] = [(transcript_dest, "output")]
    if not no_cache:
        writes.append((cache_dir, "work") if cache_dir else (run_dir, "output"))
    for target, kind in writes:
        refused = check_output_path(target, kind=kind)
        if refused:
            return refused

    prompt = initial_prompt
    if lexicon:
        extra = " ".join(str(w).strip() for w in lexicon if str(w).strip())
        if extra:
            prompt = f"{prompt} {extra}".strip() if prompt else extra

    resolved_model = model_size
    model_warning = None
    if not model_size or str(model_size).strip().lower() in {"auto", "default"}:
        resolved_model, _compute, model_warning = default_model_for_device()

    probe = probe_media(path, speech_ratio=False)
    duration = float(probe.get("duration_seconds") or 0)
    start = 0.0 if start_seconds is None else max(0.0, float(start_seconds))
    end = duration if end_seconds is None else float(end_seconds)
    if end_seconds is None and duration <= 0:
        end = start
    if end < start:
        return {"ok": False, "error": "end_seconds must be greater than start_seconds"}

    cache_root = cache_dir or run_dir
    cache_key = None
    if cache_root and not no_cache:
        cache_key = _cache_key(
            path,
            model_size=resolved_model,
            language=language,
            initial_prompt=prompt,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            chunk_seconds=float(chunk_seconds),
            overlap_seconds=float(overlap_seconds),
            drift_threshold_s=float(drift_threshold_s),
        )
        hit = _read_cache(cache_root, cache_key)
        if hit:
            hit["path"] = os.path.abspath(path)
            if transcript_dest:
                hit["transcript_path"] = write_transcript_json(hit, transcript_dest)
            return attach_container_warnings(hit, probe)

    windows = _chunk_windows(end - start, start, end, float(chunk_seconds), float(overlap_seconds))
    chunk_reports: list[dict[str, Any]] = []
    for win_start, win_end in windows:
        slice_start = win_start if (len(windows) > 1 or start_seconds is not None) else None
        slice_end = win_end if (len(windows) > 1 or end_seconds is not None) else None
        raw = transcribe_range(
            path,
            slice_start,
            slice_end,
            model_size=resolved_model,
            language=language,
            initial_prompt=prompt,
        )
        if not raw.get("ok"):
            return attach_container_warnings(raw, probe)
        drift = {"ok": True, "max_drift_s": 0.0, "samples": 0, "rerun": False}
        rerun = False
        # Drift-check every long window, including a single 10-minute slice.
        if len(windows) > 1 or (win_end - win_start) > 60:
            drift = _drift_check(
                path,
                list(raw.get("words") or []),
                win_start,
                win_end,
                model_size=resolved_model,
                language=language,
                initial_prompt=prompt,
            )
            caught = float(drift.get("max_drift_s") or 0)
            if caught > float(drift_threshold_s):
                rerun = True
                raw = transcribe_range(
                    path,
                    win_start,
                    win_end,
                    model_size=resolved_model,
                    language=language,
                    initial_prompt=prompt,
                )
                if not raw.get("ok"):
                    return attach_container_warnings(raw, probe)
                after = _drift_check(
                    path,
                    list(raw.get("words") or []),
                    win_start,
                    win_end,
                    model_size=resolved_model,
                    language=language,
                    initial_prompt=prompt,
                )
                drift = {
                    "ok": True,
                    "max_drift_s": caught,
                    "max_drift_after_s": after.get("max_drift_s"),
                    "samples": drift.get("samples"),
                    "rerun": True,
                }
        chunk_reports.append(
            {
                "start_seconds": win_start,
                "end_seconds": win_end,
                "word_count": len(raw.get("words") or []),
                "max_drift_s": drift.get("max_drift_s"),
                "drift_samples": drift.get("samples"),
                "rerun": rerun or bool(drift.get("rerun")),
                "words": list(raw.get("words") or []),
                "segments": list(raw.get("segments") or []),
                "text": raw.get("text") or "",
                "device": raw.get("device"),
                "compute_type": raw.get("compute_type"),
                "model": raw.get("model") or resolved_model,
                "language": raw.get("language"),
            }
        )

    words, segments, text = _stitch_chunk_words(chunk_reports, float(overlap_seconds))
    device = next((c.get("device") for c in chunk_reports if c.get("device")), None)
    compute = next((c.get("compute_type") for c in chunk_reports if c.get("compute_type")), None)
    language_out = next((c.get("language") for c in chunk_reports if c.get("language")), language)
    result: dict[str, Any] = {
        "ok": True,
        "available": True,
        "path": os.path.abspath(path),
        "device": device,
        "compute_type": compute,
        "model": resolved_model,
        "language": language_out,
        "duration_seconds": duration if end_seconds is None and start_seconds is None else round(end - start, 3),
        "text": text,
        "segments": segments,
        "words": words,
        "cache_hit": False,
        "chunking": {
            "chunk_seconds": float(chunk_seconds),
            "overlap_seconds": float(overlap_seconds),
            "chunk_count": len(chunk_reports),
            "drift_threshold_s": float(drift_threshold_s),
            "chunks": [
                {
                    "start_seconds": c["start_seconds"],
                    "end_seconds": c["end_seconds"],
                    "word_count": c["word_count"],
                    "max_drift_s": c["max_drift_s"],
                    "drift_samples": c["drift_samples"],
                    "rerun": c["rerun"],
                }
                for c in chunk_reports
            ],
        },
    }
    if model_warning:
        result["model_warning"] = model_warning
        result.setdefault("warnings", []).append(model_warning)
    if start_seconds is not None:
        result["start_seconds"] = float(start_seconds)
    if end_seconds is not None:
        result["end_seconds"] = float(end_seconds)
    attach_container_warnings(result, probe)

    if cache_root and cache_key and not no_cache:
        try:
            _write_cache(cache_root, cache_key, result)
        except OSError:
            pass
    if transcript_dest:
        result["transcript_path"] = write_transcript_json(result, transcript_dest)
    return result
