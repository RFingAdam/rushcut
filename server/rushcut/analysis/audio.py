"""Windowed loudness / peak detection. No NLE required."""
from __future__ import annotations

import audioop
import os
import struct
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media

_DEFAULT_WINDOW = 0.08


def _decode_mono_pcm(path: str, sample_rate: int = 8000) -> tuple[bytes, int]:
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    out = tmp.name
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                path,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                out,
            ],
            timeout=180,
        )
        with open(out, "rb") as fh:
            return fh.read(), sample_rate
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _windows(pcm: bytes, sample_rate: int, window_seconds: float) -> list[dict[str, Any]]:
    width = 2  # s16le
    hop = max(1, int(sample_rate * window_seconds))
    n = len(pcm) // width
    out: list[dict[str, Any]] = []
    for i in range(0, n, hop):
        chunk = pcm[i * width : (i + hop) * width]
        if len(chunk) < width * 4:
            continue
        # audioop.rms / max on signed 16-bit frames.
        rms = audioop.rms(chunk, width)
        peak = 0
        for (sample,) in struct.iter_unpack("<h", chunk[: len(chunk) - (len(chunk) % 2)]):
            a = abs(sample)
            if a > peak:
                peak = a
        t = i / sample_rate
        out.append(
            {
                "at_seconds": round(t, 4),
                "rms": rms,
                "peak": peak,
            }
        )
    return out


def _classify(kind_hint: str | None, rel_peak: float, rel_rms: float) -> str:
    if kind_hint:
        return kind_hint
    if rel_peak >= 4.0 or rel_rms >= 3.5:
        return "peak"
    if rel_peak >= 2.2 or rel_rms >= 2.0:
        return "loud"
    return "rise"


def analyze_audio(
    path: str,
    window_seconds: float = _DEFAULT_WINDOW,
    spike_ratio: float = 2.2,
    max_spikes: int = 40,
) -> dict[str, Any]:
    """Find loudness peaks relative to the file's own median RMS/peak."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_audio"):
        return {
            "ok": True,
            "path": os.path.abspath(path),
            "has_audio": False,
            "duration_seconds": probe.get("duration_seconds", 0),
            "spikes": [],
            "windows": [],
            "note": "no audio stream",
        }
    try:
        pcm, sr = _decode_mono_pcm(path)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    if not pcm:
        return {"ok": False, "error": "decoded audio was empty"}

    windows = _windows(pcm, sr, window_seconds)
    if not windows:
        return {"ok": False, "error": "no analysis windows"}

    rms_vals = sorted(w["rms"] for w in windows)
    peak_vals = sorted(w["peak"] for w in windows)
    median_rms = rms_vals[len(rms_vals) // 2] or 1
    median_peak = peak_vals[len(peak_vals) // 2] or 1
    max_peak = peak_vals[-1]

    spikes: list[dict[str, Any]] = []
    last_t = -999.0
    ranked = sorted(windows, key=lambda w: w["peak"], reverse=True)
    for w in ranked:
        rel_peak = w["peak"] / median_peak
        rel_rms = w["rms"] / median_rms
        if rel_peak < spike_ratio and rel_rms < spike_ratio:
            continue
        if w["at_seconds"] - last_t < 0.35 and spikes:
            # Keep the louder of two close spikes.
            if w["peak"] > spikes[-1]["peak"]:
                spikes[-1] = {**w}
            continue
        kind = _classify(None, rel_peak, rel_rms)
        spikes.append(
            {
                "at_seconds": w["at_seconds"],
                "kind": kind,
                "peak": w["peak"],
                "rms": w["rms"],
                "rel_peak": round(rel_peak, 3),
                "rel_rms": round(rel_rms, 3),
            }
        )
        last_t = w["at_seconds"]
        if len(spikes) >= max_spikes:
            break
    spikes.sort(key=lambda s: s["at_seconds"])

    # Integrated-ish stats from the PCM (linear 16-bit).
    global_rms = audioop.rms(pcm, 2)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "has_audio": True,
        "duration_seconds": probe.get("duration_seconds", 0),
        "sample_rate": sr,
        "window_seconds": window_seconds,
        "median_rms": median_rms,
        "median_peak": median_peak,
        "max_peak": max_peak,
        "global_rms": global_rms,
        "spike_count": len(spikes),
        "spikes": spikes,
    }
