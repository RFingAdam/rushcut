"""Integrated loudness (EBU R128) via ffmpeg ebur128. No NLE required."""
from __future__ import annotations

import os
import re
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media

_I_RE = re.compile(r"\bI:\s*([+-]?\d+(?:\.\d+)?)\s*LUFS", re.IGNORECASE)
_TP_RE = re.compile(
    r"True peak:\s*([+-]?\d+(?:\.\d+)?)\s*dB(?:TP|FS)?",
    re.IGNORECASE,
)
_PEAK_RE = re.compile(r"\bPeak:\s*([+-]?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)


def measure_loudness(path: str) -> dict[str, Any]:
    """Return integrated LUFS and true peak for a media file."""
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
            "integrated_lufs": None,
            "true_peak_dbtp": None,
            "note": "no audio stream",
        }
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-nostats",
                "-i",
                path,
                "-filter_complex",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            timeout=180,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc), "path": os.path.abspath(path)}
    text = proc.stderr or proc.stdout or ""
    # Prefer the Summary block at the end.
    summary = text
    idx = text.lower().rfind("summary:")
    if idx >= 0:
        summary = text[idx:]
    integrated = None
    match_i = _I_RE.search(summary) or _I_RE.search(text)
    if match_i:
        integrated = float(match_i.group(1))
    true_peak = None
    match_tp = _TP_RE.search(summary) or _TP_RE.search(text)
    if match_tp:
        true_peak = float(match_tp.group(1))
    else:
        match_peak = _PEAK_RE.search(summary) or _PEAK_RE.search(text)
        if match_peak:
            true_peak = float(match_peak.group(1))
    if integrated is None:
        return {"ok": False, "error": "ebur128 did not report integrated loudness", "stderr": text[-800:]}
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "has_audio": True,
        "integrated_lufs": round(integrated, 3),
        "true_peak_dbtp": round(true_peak, 3) if true_peak is not None else None,
    }
