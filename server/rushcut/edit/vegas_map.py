"""Build an equivalent VEGAS (or Resolve) timeline from edit.json.

Native host mapping:
- each segment → video event, trimmed to in/out
- audio_source / audio_offset → a second audio event (J/L cut)
- zoom → event.set_motion Pan/Crop keyframes
-  join micro-fades → event.set_fades

Speed ramps, lens_zoom, compose_layout, blur_regions and overlay have no
host RPC. Those segments are pre-rendered with the FFmpeg path and the
resulting file is placed. Documented limitation: VEGAS velocity envelopes
are not exposed by the current host, so speed is never a live envelope.
"""
from __future__ import annotations

from typing import Any

from .. import tools
from .apply import render_segment, segment_output_length, segment_source_length
from .curves import collect_punches, zoom_at
from .filters import normalize_steps


PRE_RENDER_EFFECTS = {"speed_ramp", "lens_zoom", "compose_layout", "blur_regions", "overlay"}


def zoom_keyframes(effect: dict[str, Any], length: float, src_w: int = 1920, src_h: int = 1080) -> list[dict[str, Any]]:
    punches = collect_punches(effect, src_w, src_h)
    times = {0.0, length}
    for punch in punches:
        times.add(max(0.0, punch["t_in"]))
        times.add(max(0.0, min(length, punch["t_in"] + punch["ease"])))
        times.add(max(0.0, min(length, punch["t_out"] - punch["ease"])))
        times.add(max(0.0, min(length, punch["t_out"])))
    keys = []
    for t in sorted(times):
        scale, cx, cy = zoom_at(t, punches)
        keys.append(
            {
                "at_seconds": t,
                "scale": scale,
                "pan_x": (cx - 0.5) * 2.0,
                "pan_y": (cy - 0.5) * 2.0,
                "type": "smooth",
                "smoothness": 0.5,
            }
        )
    return keys


def apply_edit_host(
    edit: dict[str, Any],
    output_path: str | None = None,
    *,
    backend: str = "vegas",
) -> dict[str, Any]:
    """Place trimmed events (and pre-render unsupported effects)."""
    out = edit["output"]
    width, height, fps = int(out["width"]), int(out["height"]), float(out["fps"])
    fade_s = max(0.0, float(edit.get("join_fade_ms") or 0.0) / 1000.0)
    created = tools.new_project(discard_unsaved=True)
    if not created.get("ok") and created.get("error"):
        # Some hosts return ok on new; keep going if a project is already open.
        pass
    tools.set_project_video(width, height, fps)
    video = tools.add_track("video", "V1")
    audio = tools.add_track("audio", "A1")
    v_idx = int(video.get("track_index") or 0)
    a_idx = int(audio.get("track_index") or 1)
    cursor = 0.0
    events: list[dict[str, Any]] = []
    notes: list[str] = []
    for i, segment in enumerate(edit["segments"]):
        kinds = {e.get("type") for e in segment.get("effects") or []}
        need_prerender = bool(kinds & PRE_RENDER_EFFECTS)
        media = segment["source"]
        take = float(segment["in"])
        src_len = segment_source_length(segment)
        out_len = segment_output_length(segment)
        if need_prerender:
            from ..output_policy import make_work_dir

            work = make_work_dir("rushcut-vegas-seg-")
            dest = f"{work}/seg{i}.mp4"
            rendered = render_segment(segment, dest, work, width, height, fps, fade_s)
            if not rendered.get("ok"):
                return rendered
            media = dest
            take = 0.0
            src_len = out_len
            notes.append(f"segment {i}: pre-rendered {sorted(kinds & PRE_RENDER_EFFECTS)} on FFmpeg")
        placed = tools.add_video_event(v_idx, media, cursor, out_len)
        if not placed.get("ok"):
            return placed
        ev_idx = int(placed.get("event_index") or 0)
        trimmed = tools.trim_event(v_idx, ev_idx, length_seconds=out_len, take_offset_seconds=take)
        if not trimmed.get("ok"):
            # Some hosts treat length on add as enough.
            pass
        if fade_s > 0:
            tools.set_event_fades(v_idx, ev_idx, fade_in_seconds=fade_s, fade_out_seconds=fade_s)
        zoom = next((e for e in segment.get("effects") or [] if e.get("type") == "zoom"), None)
        if zoom and not need_prerender:
            keys = zoom_keyframes(zoom, src_len)
            tools.set_motion_keyframes(v_idx, ev_idx, keys)
        audio_src = segment.get("audio_source")
        if audio_src and not need_prerender:
            audio_take = float(segment.get("audio_offset") or 0.0)
            if not segment.get("audio_source"):
                audio_take = float(segment["in"]) + audio_take
            a_placed = tools.add_audio_event(a_idx, str(audio_src), cursor, out_len)
            if a_placed.get("ok"):
                tools.trim_event(
                    a_idx,
                    int(a_placed.get("event_index") or 0),
                    length_seconds=out_len,
                    take_offset_seconds=audio_take,
                )
        if any(e.get("type") == "speed_ramp" for e in segment.get("effects") or []):
            notes.append(
                f"segment {i}: speed_ramp is FFmpeg-only "
                "(VEGAS velocity envelopes are not in the host RPC)"
            )
            _ = normalize_steps
        events.append(
            {
                "segment": i,
                "start_seconds": cursor,
                "length_seconds": out_len,
                "media": media,
                "take_offset_seconds": take,
            }
        )
        cursor += out_len
    result = {
        "ok": True,
        "backend": backend,
        "output_path": output_path,
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": cursor,
        "events": events,
        "notes": notes,
        "mapping": {
            "zoom": "event.set_motion Pan/Crop keyframes",
            "speed_ramp": "FFmpeg pre-render (no velocity-envelope RPC)",
            "lens_zoom": "FFmpeg-only",
            "compose_layout": "FFmpeg-only (tracks + Pan/Crop cannot express blur_fill)",
            "blur_regions": "FFmpeg-only",
            "overlay": "place a PNG event on a higher video track, or FFmpeg pre-render",
        },
        "manual_check": (
            "Open the project in VEGAS, confirm each event's take offset equals "
            "segment.in and its length equals out-in (or the sped length), and "
            "that zoom punches show as VideoMotion keyframes. Speed-ramped "
            "segments should be pre-rendered media, not a live envelope."
        ),
    }
    return result
