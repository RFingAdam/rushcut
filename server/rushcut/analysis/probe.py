"""probe_media — ffprobe wrapper. No NLE required."""
from __future__ import annotations

import os
from typing import Any

from .ffmpeg_util import FfmpegError, ffprobe_json


def _stream_duration(stream: dict[str, Any]) -> float | None:
    """Duration of one stream, preferring tagged duration then frames/fps."""
    raw = stream.get("duration")
    if raw not in (None, "", "N/A"):
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    tags = stream.get("tags") or {}
    for key in ("DURATION", "duration"):
        tagged = tags.get(key)
        if tagged not in (None, "", "N/A"):
            parsed = _parse_clock(tagged)
            if parsed is not None:
                return parsed
    frames = stream.get("nb_frames")
    fps = _fps(stream)
    if frames not in (None, "", "N/A") and fps:
        try:
            count = float(frames)
        except (TypeError, ValueError):
            count = 0.0
        if count > 0:
            return count / fps
    return None


def _parse_clock(value: Any) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    if len(parts) == 3:
        try:
            hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            return None
        return hours * 3600 + minutes * 60 + seconds
    return None


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
    raw_fmt_duration = fmt.get("duration")
    header_duration_missing = raw_fmt_duration in (None, "", "N/A")
    try:
        duration = float(raw_fmt_duration or 0)
    except (TypeError, ValueError):
        duration = 0.0
        header_duration_missing = True
    format_duration = None if header_duration_missing else duration

    video = None
    audio = None
    duration_estimated = False
    stream_durations: list[float] = []
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream
        stream_dur = _stream_duration(stream)
        if stream_dur is not None:
            stream_durations.append(stream_dur)
            if duration <= 0 and stream_dur > duration:
                duration = stream_dur
                duration_estimated = True

    stream_max = max(stream_durations) if stream_durations else duration

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "duration_seconds": stream_max if stream_max else duration,
        "format_duration_seconds": format_duration,
        "stream_durations": stream_durations,
        "stream_max_duration_seconds": stream_max,
        "size_bytes": int(fmt.get("size") or 0) if str(fmt.get("size") or "").isdigit() else None,
        "format_name": fmt.get("format_name"),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "header_duration_missing": header_duration_missing,
        "duration_estimated": duration_estimated,
        "unfinalized_container": header_duration_missing or duration <= 0,
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
