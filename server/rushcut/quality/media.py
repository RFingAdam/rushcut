"""check_media: loudness, duration, fps, and a speech-boundary re-check on the render."""
from __future__ import annotations

import os
from typing import Any

from ..analysis.loudness import measure_loudness
from ..analysis.probe import probe_media
from ..analysis.transcript import analyze_transcript
from .speech import check_speech_boundaries, cut_points_from_cuts


def output_join_times(cuts: Any) -> list[float]:
    """Internal join times on the *output* timeline, from a source cut list.

    A list of bare numbers is treated as output-timeline cut points already.
    Segment in/out pairs contribute their lengths in order; the last endpoint
    (the end of the file) is not a join.
    """
    if cuts is None:
        return []
    if isinstance(cuts, dict):
        if any(k in cuts for k in ("cuts", "segments", "events", "clips")):
            return output_join_times(
                cuts.get("cuts") or cuts.get("segments") or cuts.get("events") or cuts.get("clips")
            )
    points = cut_points_from_cuts(cuts)
    if points and all(p.get("role") == "cut" for p in points):
        return sorted({round(float(p["at_seconds"]), 3) for p in points})
    by_index: dict[int, dict[str, float]] = {}
    order: list[int] = []
    for point in points:
        idx = int(point.get("segment_index") or 0)
        if idx not in by_index:
            order.append(idx)
        slot = by_index.setdefault(idx, {})
        role = point.get("role")
        t = float(point["at_seconds"])
        if role == "in":
            slot["in"] = t
        elif role == "out":
            slot["out"] = t
    cursor = 0.0
    joins: list[float] = []
    for idx in order:
        slot = by_index[idx]
        if "in" not in slot or "out" not in slot:
            continue
        length = abs(float(slot["out"]) - float(slot["in"]))
        cursor += length
        joins.append(round(cursor, 3))
    if joins:
        joins = joins[:-1]
    return joins


def check_media(
    path: str,
    *,
    cuts: Any = None,
    transcript: Any = None,
    words: list[dict[str, Any]] | None = None,
    pad_seconds: float = 0.12,
    transcribe: bool = True,
    model_size: str = "base",
    language: str | None = "en",
    offset_map: list[dict[str, Any]] | None = None,
    session_seconds: float | None = None,
) -> dict[str, Any]:
    """One call: integrated LUFS, true peak, duration, fps, chopped-word flags."""
    if offset_map is not None and session_seconds is not None:
        from ..analysis.session import lookup_session_time

        hit = lookup_session_time(offset_map, session_seconds)
        if not hit.get("ok"):
            return hit
        path = str(hit["path"])
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    loud = measure_loudness(path)
    if not loud.get("ok") and loud.get("has_audio") is not False:
        # loudness failure is reported but duration/fps still return
        pass

    speech_doc: Any = transcript
    if words is not None:
        speech_doc = {"words": words}
    if isinstance(speech_doc, dict) and not speech_doc.get("words") and words:
        speech_doc = {**speech_doc, "words": words}

    joins = output_join_times(cuts)
    speech_check: dict[str, Any] | None = None
    if joins:
        if speech_doc is None and transcribe:
            analyzed = analyze_transcript(path, model_size=model_size, language=language, diarize=False)
            if analyzed.get("ok") and analyzed.get("words"):
                speech_doc = analyzed
            else:
                speech_check = {
                    "id": "speech_boundaries",
                    "pass": True,
                    "skipped": True,
                    "note": analyzed.get("error") or "no transcript for output re-check",
                    "violations": [],
                    "value": 0,
                    "limit": 0,
                }
        if speech_doc is not None:
            speech_check = check_speech_boundaries(
                joins,
                speech_doc,
                pad_seconds=float(pad_seconds),
                mode="fail",
            )

    chopped = list((speech_check or {}).get("violations") or [])
    loud_ok = bool(loud.get("ok"))
    speech_ok = True if speech_check is None or speech_check.get("skipped") else bool(speech_check.get("pass"))
    ok = bool(probe.get("ok")) and (loud_ok or loud.get("has_audio") is False) and speech_ok
    result: dict[str, Any] = {
        "ok": ok,
        "path": os.path.abspath(path),
        "duration_seconds": probe.get("duration_seconds"),
        "fps": probe.get("fps"),
        "width": probe.get("width"),
        "height": probe.get("height"),
        "integrated_lufs": loud.get("integrated_lufs"),
        "true_peak_dbtp": loud.get("true_peak_dbtp"),
        "has_audio": probe.get("has_audio"),
        "has_video": probe.get("has_video"),
        "join_times": joins,
        "speech_boundaries": speech_check,
        "chopped_words": chopped,
        "chopped_word_count": len(chopped),
    }
    if not loud_ok and loud.get("error"):
        result["loudness_error"] = loud.get("error")
    return result
