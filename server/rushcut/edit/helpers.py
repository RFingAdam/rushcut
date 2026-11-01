"""Crossfade and music/voice ducking on top of fades + envelopes."""
from __future__ import annotations

from typing import Any

from .. import tools


def apply_crossfade(
    track_index: int,
    event_index: int,
    next_event_index: int | None = None,
    seconds: float = 0.4,
    dissolve: bool = True,
) -> dict[str, Any]:
    """Overlap two adjacent events and put reciprocal fades on the join."""
    if next_event_index is None:
        next_event_index = event_index + 1
    state = tools.get_project_state()
    if not state.get("ok"):
        return state
    events = [
        e
        for e in state.get("events") or []
        if e.get("track_index") == track_index
        and e.get("event_index") in {event_index, next_event_index}
    ]
    by_idx = {e["event_index"]: e for e in events}
    left = by_idx.get(event_index)
    right = by_idx.get(next_event_index)
    if not left or not right:
        return {"ok": False, "error": "both events must exist on the track"}
    left_end = float(left["start_seconds"]) + float(left["length_seconds"])
    desired_right = left_end - seconds
    if desired_right < 0:
        desired_right = 0.0
    moved = tools.move_event(track_index, next_event_index, desired_right)
    if not moved.get("ok"):
        return moved
    fade_left = tools.set_event_fades(
        track_index,
        event_index,
        fade_out_seconds=seconds,
        dissolve=dissolve,
        curve="smooth",
        reciprocal_curve="smooth",
    )
    fade_right = tools.set_event_fades(
        track_index,
        next_event_index,
        fade_in_seconds=seconds,
        dissolve=False,
        curve="smooth",
    )
    ok = fade_left.get("ok") and fade_right.get("ok")
    return {
        "ok": bool(ok),
        "track_index": track_index,
        "event_index": event_index,
        "next_event_index": next_event_index,
        "seconds": seconds,
        "right_start_seconds": desired_right,
        "moved": moved,
        "fade_left": fade_left,
        "fade_right": fade_right,
        "used_mode": state.get("used_mode") or moved.get("used_mode"),
    }


def apply_ducking(
    music_track_index: int,
    voice_track_index: int,
    reduction_db: float = -12.0,
    pad_seconds: float = 0.15,
    rest_level: float = 1.0,
) -> dict[str, Any]:
    """Duck the music Volume envelope wherever the voice track has events."""
    state = tools.get_project_state()
    if not state.get("ok"):
        return state
    voice = [
        e
        for e in state.get("events") or []
        if e.get("track_index") == voice_track_index
    ]
    if not voice:
        return {"ok": False, "error": "voice track has no events to duck under"}
    duck = 10 ** (reduction_db / 20.0)
    points: list[dict[str, Any]] = [{"at_seconds": 0.0, "value": rest_level, "curve": "smooth"}]
    for ev in sorted(voice, key=lambda e: e.get("start_seconds") or 0):
        start = max(0.0, float(ev["start_seconds"]) - pad_seconds)
        end = float(ev["start_seconds"]) + float(ev["length_seconds"]) + pad_seconds
        points.append({"at_seconds": start, "value": rest_level, "curve": "smooth"})
        points.append({"at_seconds": start + 0.05, "value": duck, "curve": "smooth"})
        points.append({"at_seconds": max(start + 0.1, end - 0.05), "value": duck, "curve": "smooth"})
        points.append({"at_seconds": end, "value": rest_level, "curve": "smooth"})
    result = tools.set_envelope_points(music_track_index, "Volume", points)
    result = dict(result)
    result["duck_gain"] = duck
    result["reduction_db"] = reduction_db
    result["voice_events"] = len(voice)
    result["used_mode"] = state.get("used_mode") or result.get("used_mode")
    return result
