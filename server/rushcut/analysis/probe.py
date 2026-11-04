"""probe_media — ffprobe wrapper. No NLE required."""
from __future__ import annotations

import os
import tempfile
import time
from typing import Any

from .ffmpeg_util import FfmpegError, ffprobe_json, run, which_ffmpeg

DEFAULT_STILL_WRITING_S = 2.0


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


def _estimate_duration_from_packets(path: str) -> float | None:
    """Scan the last packets when the container header has no duration."""
    try:
        data = ffprobe_json(
            path,
            extra=[
                "-read_intervals",
                "-5",
                "-show_packets",
                "-show_entries",
                "packet=pts_time,dts_time,duration_time",
            ],
        )
    except FfmpegError:
        return None
    last = 0.0
    found = False
    for pkt in data.get("packets") or []:
        if not isinstance(pkt, dict):
            continue
        t = None
        for key in ("pts_time", "dts_time"):
            raw = pkt.get(key)
            if raw in (None, "", "N/A"):
                continue
            try:
                t = float(raw)
                break
            except (TypeError, ValueError):
                continue
        if t is None:
            continue
        dur = 0.0
        raw_dur = pkt.get("duration_time")
        if raw_dur not in (None, "", "N/A"):
            try:
                dur = float(raw_dur)
            except (TypeError, ValueError):
                dur = 0.0
        last = max(last, t + max(dur, 0.0))
        found = True
    return last if found and last > 0 else None


def _cues_missing(data: dict[str, Any], header_duration_missing: bool, format_name: str | None) -> bool:
    fmt = str(format_name or "").lower()
    is_mkv = "matroska" in fmt or "webm" in fmt
    saw_index = False
    for stream in data.get("streams") or []:
        raw = stream.get("nb_index_entries")
        if raw in (None, "", "N/A"):
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        saw_index = True
        if count > 0:
            return False
    if saw_index:
        return True
    return bool(header_duration_missing and is_mkv)


def _speech_ratio(path: str, stream_index: int, max_seconds: float = 15.0) -> float | None:
    """Energy VAD on one audio stream. Fraction of windows above the noise floor."""
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                path,
                "-map",
                f"0:{int(stream_index)}",
                "-t",
                f"{max_seconds:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                tmp.name,
            ],
            timeout=60,
        )
        with open(tmp.name, "rb") as fh:
            pcm = fh.read()
    except (FfmpegError, OSError):
        return None
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    width = 2
    hop = 160  # 20 ms at 8 kHz
    if len(pcm) < hop * width * 4:
        return 0.0
    import struct

    n = len(pcm) // width
    rms_vals: list[float] = []
    for i in range(0, n, hop):
        chunk = pcm[i * width : (i + hop) * width]
        if len(chunk) < width * 4:
            continue
        total = 0
        count = len(chunk) // 2
        for (sample,) in struct.iter_unpack("<h", chunk[: count * 2]):
            total += sample * sample
        rms_vals.append((total / count) ** 0.5 if count else 0.0)
    if not rms_vals:
        return 0.0
    ranked = sorted(rms_vals)
    noise = ranked[max(0, len(ranked) // 5)]
    threshold = max(noise * 3.0, 200.0)
    active = sum(1 for rms in rms_vals if rms >= threshold)
    return round(active / len(rms_vals), 4)


def file_still_writing(
    path: str,
    window_s: float = DEFAULT_STILL_WRITING_S,
    poll_s: float = 0.0,
) -> dict[str, Any]:
    """True when size/mtime changed within ``window_s`` (still recording)."""
    if not path:
        return {"ok": False, "error": "path is required", "still_writing": False}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}", "still_writing": False}
    try:
        window = float(window_s)
    except (TypeError, ValueError):
        window = DEFAULT_STILL_WRITING_S
    if window < 0:
        window = 0.0
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "still_writing": False}
    size = int(st.st_size)
    age = max(0.0, time.time() - float(st.st_mtime))
    changed = False
    if poll_s and float(poll_s) > 0:
        time.sleep(float(poll_s))
        try:
            st2 = os.stat(path)
        except OSError as exc:
            return {"ok": False, "error": str(exc), "still_writing": False}
        changed = int(st2.st_size) != size or float(st2.st_mtime) > float(st.st_mtime) + 1e-6
        size = int(st2.st_size)
        age = max(0.0, time.time() - float(st2.st_mtime))
    still = bool(changed or age < window)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "still_writing": still,
        "size_bytes": size,
        "mtime_age_seconds": round(age, 4),
        "window_s": window,
    }


def container_warnings(probe: dict[str, Any] | None) -> list[str]:
    warnings: list[str] = []
    if not probe:
        return warnings
    if probe.get("unfinalized_container") or probe.get("header_duration_missing") or probe.get("cues_missing"):
        warnings.append("unfinalized_container")
    if probe.get("still_writing"):
        warnings.append("still_writing")
    return warnings


def attach_container_warnings(
    result: dict[str, Any],
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra = container_warnings(probe or result)
    warnings = list(result.get("warnings") or [])
    for item in extra:
        if item not in warnings:
            warnings.append(item)
    result["warnings"] = warnings
    return result


def probe_media(path: str, *, speech_ratio: bool = True) -> dict[str, Any]:
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
    video_duration = None
    audio_duration = None
    audio_streams: list[dict[str, Any]] = []
    for stream in data.get("streams") or []:
        stream_dur = _stream_duration(stream)
        codec_type = stream.get("codec_type")
        if codec_type == "video" and video is None:
            video = stream
            video_duration = stream_dur
        elif codec_type == "audio":
            if audio is None:
                audio = stream
                audio_duration = stream_dur
            try:
                abs_index = int(stream.get("index"))
            except (TypeError, ValueError):
                abs_index = len(audio_streams)
            tags = stream.get("tags") or {}
            entry: dict[str, Any] = {
                "index": abs_index,
                "audio_index": len(audio_streams),
                "codec": stream.get("codec_name"),
                "channels": int(stream.get("channels") or 0) if stream.get("channels") else None,
                "sample_rate": int(stream.get("sample_rate") or 0) if stream.get("sample_rate") else None,
                "channel_layout": stream.get("channel_layout"),
                "title": tags.get("title") or tags.get("TITLE"),
                "handler_name": tags.get("handler_name") or tags.get("HANDLER_NAME"),
                "speech_ratio": None,
            }
            if speech_ratio:
                entry["speech_ratio"] = _speech_ratio(path, abs_index)
            audio_streams.append(entry)
        if stream_dur is not None:
            stream_durations.append(stream_dur)
            if duration <= 0 and stream_dur > duration:
                duration = stream_dur
                duration_estimated = True

    packet_duration = None
    if header_duration_missing or duration <= 0:
        packet_duration = _estimate_duration_from_packets(path)
        if packet_duration and packet_duration > duration:
            duration = packet_duration
            duration_estimated = True

    stream_max = max(stream_durations) if stream_durations else duration
    if packet_duration and packet_duration > stream_max:
        stream_max = packet_duration

    try:
        st = os.stat(path)
        mtime_age = max(0.0, time.time() - float(st.st_mtime))
    except OSError:
        mtime_age = None
    format_name = fmt.get("format_name")
    cues_missing = _cues_missing(data, header_duration_missing, format_name)
    unfinalized = bool(header_duration_missing or cues_missing or duration <= 0)

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "duration_seconds": stream_max if stream_max else duration,
        "format_duration_seconds": format_duration,
        "stream_durations": stream_durations,
        "stream_max_duration_seconds": stream_max,
        "video_duration_seconds": video_duration,
        "audio_duration_seconds": audio_duration,
        "size_bytes": int(fmt.get("size") or 0) if str(fmt.get("size") or "").isdigit() else None,
        "format_name": format_name,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "header_duration_missing": header_duration_missing,
        "cues_missing": cues_missing,
        "duration_estimated": duration_estimated,
        "unfinalized_container": unfinalized,
        "still_writing": False,
        "mtime_age_seconds": None if mtime_age is None else round(mtime_age, 4),
        "audio_streams": audio_streams,
        "audio_stream_count": len(audio_streams),
    }
    if packet_duration is not None:
        result["packet_duration_seconds"] = round(float(packet_duration), 6)
    attach_container_warnings(result)
    if video:
        result["width"] = int(video.get("width") or 0)
        result["height"] = int(video.get("height") or 0)
        result["fps"] = _fps(video)
        result["video_codec"] = video.get("codec_name")
        result["pix_fmt"] = video.get("pix_fmt")
        result["video_profile"] = video.get("profile")
        result["video_level"] = video.get("level")
        result["video_time_base"] = video.get("time_base")
        result["r_frame_rate"] = video.get("r_frame_rate")
        result["field_order"] = video.get("field_order")
        raw_frames = video.get("nb_frames")
        try:
            result["nb_frames"] = int(raw_frames) if raw_frames not in (None, "", "N/A") else None
        except (TypeError, ValueError):
            result["nb_frames"] = None
    if audio:
        result["audio_codec"] = audio.get("codec_name")
        result["sample_rate"] = int(audio.get("sample_rate") or 0) if audio.get("sample_rate") else None
        result["channels"] = int(audio.get("channels") or 0) if audio.get("channels") else None
        result["sample_fmt"] = audio.get("sample_fmt")
        result["channel_layout"] = audio.get("channel_layout")
        result["audio_profile"] = audio.get("profile")
        result["audio_time_base"] = audio.get("time_base")
    return result


def stream_copy_signature(probe: dict[str, Any]) -> tuple[Any, ...]:
    """Codec parameters that must match for a concat demuxer stream-copy."""
    fps = probe.get("fps")
    try:
        fps_key = round(float(fps), 3) if fps else None
    except (TypeError, ValueError):
        fps_key = None
    return (
        bool(probe.get("has_video")),
        bool(probe.get("has_audio")),
        probe.get("video_codec"),
        int(probe.get("width") or 0),
        int(probe.get("height") or 0),
        probe.get("pix_fmt"),
        fps_key,
        probe.get("r_frame_rate"),
        probe.get("video_profile"),
        probe.get("video_level"),
        probe.get("audio_codec"),
        probe.get("sample_rate"),
        probe.get("channels"),
        probe.get("sample_fmt"),
        probe.get("channel_layout"),
        probe.get("audio_profile"),
    )


def streams_compatible(probes: list[dict[str, Any]]) -> bool:
    """True when every probe shares stream-copy codec parameters."""
    if not probes or any(not p.get("ok") for p in probes):
        return False
    first = stream_copy_signature(probes[0])
    return all(stream_copy_signature(p) == first for p in probes[1:])
