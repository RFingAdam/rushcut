"""Verify a render with ffprobe and write the edit-metadata sidecar JSON."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from ..analysis.ffmpeg_util import decode_timeout_seconds
from ..analysis.probe import probe_media
from ..output_policy import check_output_path

SIDECAR_KEYS = (
    "length",
    "hook_start_time",
    "caption_style",
    "zoom_count",
    "music",
    "template",
)


def sidecar_path(output_path: str) -> str:
    return output_path + ".edit.json"


def file_md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fps_exact(probe: dict[str, Any]) -> str | None:
    raw = probe.get("r_frame_rate") or probe.get("avg_frame_rate")
    if raw:
        return str(raw)
    fps = probe.get("fps")
    if not fps:
        return None
    return f"{float(fps):.6f}".rstrip("0").rstrip(".")


def _frame_count(probe: dict[str, Any], duration: float) -> int | None:
    if probe.get("nb_frames") not in (None, "", 0):
        try:
            return int(probe["nb_frames"])
        except (TypeError, ValueError):
            pass
    fps = probe.get("fps")
    if fps and duration > 0:
        return int(round(float(fps) * duration))
    return None


def _av_offset_ms(path: str, probe: dict[str, Any]) -> float | None:
    if not probe.get("has_video") or not probe.get("has_audio"):
        return None
    try:
        from ..analysis.ffmpeg_util import ffprobe_json

        data = ffprobe_json(path)
    except Exception:  # noqa: BLE001 — probe failures stay soft
        return None
    video_start = None
    audio_start = None
    for stream in data.get("streams") or []:
        raw = stream.get("start_time")
        if raw in (None, "", "N/A"):
            continue
        try:
            start = float(raw)
        except (TypeError, ValueError):
            continue
        if stream.get("codec_type") == "video" and video_start is None:
            video_start = start
        elif stream.get("codec_type") == "audio" and audio_start is None:
            audio_start = start
    if video_start is None or audio_start is None:
        return 0.0
    return (audio_start - video_start) * 1000.0


def _head_black_s(path: str, probe: dict[str, Any], luma_max: float = 16.0) -> float:
    if not probe.get("has_video"):
        return 0.0
    from ..edit.mediaio import extract_frame, mean_luma

    fps = float(probe.get("fps") or 30.0) or 30.0
    duration = float(probe.get("duration_seconds") or 0)
    limit = min(duration, 2.0)
    t = 0.0
    black = 0.0
    while t < limit:
        grabbed = extract_frame(path, t)
        if not grabbed.get("ok"):
            break
        luma = mean_luma(grabbed["rgb"], int(grabbed["width"]), int(grabbed["height"]))
        if luma > luma_max:
            break
        black = t + 1.0 / fps
        t += 1.0 / fps
    return black


def verify_output(
    path: str,
    *,
    expect_width: int | None = None,
    expect_height: int | None = None,
    expect_audio: bool = True,
    duration_tolerance: float = 0.6,
    expect_duration: float | None = None,
    decode: bool = True,
    decode_timeout: float | None = None,
    measure_loudness: bool = False,
    measure_head_black: bool = False,
) -> dict[str, Any]:
    """ffprobe + optional full-decode QA.

    Always returns ``moov_ok``, ``decode_errors`` and ``md5``. ``ok`` is
    false when the moov atom is missing or the decode reports errors, even
    if the other expectation checks pass. Duration mismatches vs
    ``expect_duration`` also set ``ok`` false.
    """
    from .atomic import count_decode_errors, file_md5, has_moov_atom

    probe = probe_media(path)
    md5 = file_md5(path)
    moov_ok = bool(probe.get("ok")) and has_moov_atom(path)
    if probe.get("unfinalized_container") or probe.get("header_duration_missing"):
        moov_ok = False
    decode_errors = 0
    if decode and os.path.isfile(path):
        probed_duration = float(
            probe.get("stream_max_duration_seconds") or probe.get("duration_seconds") or 0
        )
        decode_errors = count_decode_errors(
            path, timeout=decode_timeout_seconds(probed_duration, decode_timeout)
        )
    elif not os.path.isfile(path):
        decode_errors = 1
    problems: list[str] = []
    extras = {
        "fps_exact": None,
        "frame_count": None,
        "av_offset_ms": None,
        "lufs": None,
        "true_peak": None,
        "head_black_s": None,
    }
    if not probe.get("ok"):
        if probe.get("error"):
            problems.append(str(probe.get("error")))
        result = dict(probe)
        result["ok"] = False
        result["moov_ok"] = moov_ok
        result["decode_errors"] = decode_errors
        result["md5"] = md5
        result["problems"] = problems
        result["duration_seconds"] = float(
            probe.get("stream_max_duration_seconds") or probe.get("duration_seconds") or 0
        )
        result.update(extras)
        return result
    if expect_audio and not probe.get("has_audio"):
        problems.append("no audio stream")
    if expect_width and int(probe.get("width") or 0) != expect_width:
        problems.append(f"width {probe.get('width')} != {expect_width}")
    if expect_height and int(probe.get("height") or 0) != expect_height:
        problems.append(f"height {probe.get('height')} != {expect_height}")
    duration = float(probe.get("stream_max_duration_seconds") or probe.get("duration_seconds") or 0)
    if expect_duration is not None:
        if abs(duration - expect_duration) > duration_tolerance:
            problems.append(f"duration {duration:.2f}s != {expect_duration:.2f}s ±{duration_tolerance}")
    if not moov_ok:
        problems.append("missing moov atom")
    if decode_errors > 0:
        problems.append(f"{decode_errors} decode error(s)")
    extras["fps_exact"] = _fps_exact(probe)
    extras["frame_count"] = _frame_count(probe, duration)
    extras["av_offset_ms"] = _av_offset_ms(path, probe)
    if measure_loudness and probe.get("has_audio"):
        try:
            from ..analysis.loudness import measure_loudness as _loud

            loud = _loud(path)
            extras["lufs"] = loud.get("integrated_lufs")
            extras["true_peak"] = loud.get("true_peak_dbtp")
        except Exception:  # noqa: BLE001 — loudness is optional QA
            extras["lufs"] = None
            extras["true_peak"] = None
    if measure_head_black and probe.get("has_video"):
        try:
            extras["head_black_s"] = _head_black_s(path, probe)
        except Exception:  # noqa: BLE001 — head-black is optional QA
            extras["head_black_s"] = None
    return {
        "ok": not problems,
        "path": probe.get("path"),
        "probe": probe,
        "problems": problems,
        "moov_ok": moov_ok,
        "decode_errors": decode_errors,
        "md5": md5,
        "duration_seconds": duration,
        **extras,
    }


def write_sidecar(
    output_path: str,
    edit_meta: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refused = check_output_path(output_path)
    if refused:
        return refused
    meta = {k: (edit_meta or {}).get(k) for k in SIDECAR_KEYS}
    # Accept a few aliases from callers.
    if meta["length"] is None:
        meta["length"] = (edit_meta or {}).get("length_seconds")
    if meta["music"] is None:
        meta["music"] = bool((edit_meta or {}).get("music_on") if edit_meta and "music_on" in edit_meta else (edit_meta or {}).get("music"))
    if timeline and isinstance(timeline, dict):
        meta = dict(meta)
        if timeline.get("segments") and "segments" not in meta:
            meta["segments"] = timeline["segments"]
        if timeline.get("output") is not None:
            meta["output"] = timeline.get("output")
        if timeline.get("join_fade_ms") is not None:
            meta["join_fade_ms"] = timeline.get("join_fade_ms")
        if timeline.get("effects") is not None:
            meta["effects"] = timeline.get("effects")
        if timeline.get("schema"):
            meta["schema"] = timeline.get("schema")
    payload = {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "edit": meta,
    }
    if timeline and isinstance(timeline, dict) and timeline.get("segments"):
        payload["schema"] = timeline.get("schema") or "rushcut.edit/v1"
        payload["segments"] = timeline["segments"]
        payload["output"] = timeline.get("output")
        payload["join_fade_ms"] = timeline.get("join_fade_ms")
        if timeline.get("effects"):
            payload["effects"] = timeline["effects"]
    if extra:
        payload["extra"] = extra
    dest = sidecar_path(output_path)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    payload["sidecar_path"] = dest
    return payload
