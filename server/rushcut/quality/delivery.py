"""Shorts delivery checks: duration cap and first-frame black."""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media

DEFAULT_MAX_SECONDS = 30.0
DEFAULT_BLACK_LUMA = 16.0


def stream_max_duration(path: str | None = None, probe: dict[str, Any] | None = None) -> float:
    """Longest stream duration. Does not fall back to format duration alone."""
    info = probe
    if info is None:
        if not path:
            return 0.0
        info = probe_media(path)
    if not info or not info.get("ok"):
        return 0.0
    streams = info.get("stream_durations") or []
    if streams:
        return float(max(streams))
    raw = info.get("stream_max_duration_seconds")
    if raw not in (None, "", "N/A"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 0.0


def check_duration_cap(
    path: str,
    *,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    probe: dict[str, Any] | None = None,
    mode: str = "fail",
) -> dict[str, Any]:
    """Fail when the longest stream is at or above ``max_seconds``."""
    if str(mode).lower() == "off":
        return {
            "id": "duration_cap",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "value": 0,
            "limit": float(max_seconds),
        }
    info = probe or probe_media(path)
    if not info.get("ok"):
        return {
            "id": "duration_cap",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(max_seconds),
            "error": info.get("error") or "probe failed",
        }
    value = stream_max_duration(path, probe=info)
    limit = float(max_seconds)
    passed = value < limit
    return {
        "id": "duration_cap",
        "pass": passed,
        "mode": str(mode).lower(),
        "value": round(value, 6),
        "limit": limit,
        "format_duration_seconds": info.get("format_duration_seconds"),
        "stream_durations": list(info.get("stream_durations") or []),
        "detail": None if passed else f"{value:.3f}s >= {limit:.3f}s (stream-max)",
        "path": os.path.abspath(path) if path else None,
    }


_YAVG_RE = re.compile(r"(?:YAVG:|signalstats\.YAVG=)\s*([0-9.]+)")


def _mean_luma(image_path: str) -> float | None:
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-i",
                image_path,
                "-vf",
                "signalstats,metadata=print",
                "-f",
                "null",
                "-",
            ],
            timeout=30,
        )
    except FfmpegError:
        return None
    text = (proc.stderr or proc.stdout or "")
    match = _YAVG_RE.search(text)
    if not match:
        return None
    return float(match.group(1))


def check_frame1_not_black(
    path: str,
    *,
    black_luma: float = DEFAULT_BLACK_LUMA,
    mode: str = "fail",
) -> dict[str, Any]:
    """Fail when frame 0 is at or below the near-black luma threshold."""
    if str(mode).lower() == "off":
        return {
            "id": "frame1_not_black",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "value": 0,
            "limit": float(black_luma),
        }
    if not path or not os.path.isfile(path):
        return {
            "id": "frame1_not_black",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(black_luma),
            "error": f"file not found: {path}",
        }
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    luma = None
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                path,
                "-vf",
                "select=eq(n\\,0)",
                "-vframes",
                "1",
                tmp.name,
            ],
            timeout=60,
        )
        luma = _mean_luma(tmp.name)
    except FfmpegError as exc:
        return {
            "id": "frame1_not_black",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(black_luma),
            "error": str(exc),
        }
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    if luma is None:
        return {
            "id": "frame1_not_black",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(black_luma),
            "error": "could not measure first-frame luma",
        }
    limit = float(black_luma)
    passed = luma > limit
    return {
        "id": "frame1_not_black",
        "pass": passed,
        "mode": str(mode).lower(),
        "value": round(luma, 3),
        "limit": limit,
        "detail": None if passed else f"frame 1 luma {luma:.1f} <= {limit:.1f}",
        "path": os.path.abspath(path),
    }
