"""probe_media — ffprobe wrapper. No NLE required."""
from __future__ import annotations

import os
from typing import Any

from .ffmpeg_util import FfmpegError, ffprobe_json


def _fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if isinstance(raw, str) and "/" in raw:
            num, den = raw.split("/", 1)
            try:
                n, d = float(num), float(den)
                if d:
                    return n / d
            except ValueError:
                continue
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    return None


def probe_media(path: str) -> dict[str, Any]:
    """Return duration, video size, fps, and audio presence for a media file."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        data = ffprobe_json(path)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    video = None
    audio = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "duration_seconds": duration,
        "size_bytes": int(fmt.get("size") or 0) if str(fmt.get("size") or "").isdigit() else None,
        "format_name": fmt.get("format_name"),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }
    if video:
        result["width"] = int(video.get("width") or 0)
        result["height"] = int(video.get("height") or 0)
        result["fps"] = _fps(video)
        result["video_codec"] = video.get("codec_name")
        result["pix_fmt"] = video.get("pix_fmt")
    if audio:
        result["audio_codec"] = audio.get("codec_name")
        result["sample_rate"] = int(audio.get("sample_rate") or 0) if audio.get("sample_rate") else None
        result["channels"] = int(audio.get("channels") or 0) if audio.get("channels") else None
    return result
