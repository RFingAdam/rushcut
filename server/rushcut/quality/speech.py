"""Speech-boundary and turn-completeness checks for any director's cut list."""
from __future__ import annotations

from typing import Any

from ..analysis.transcript import speech_spans_from_words, word_times

_START_KEYS = ("start_seconds", "start", "in_seconds", "in", "source_in")
_END_KEYS = ("end_seconds", "end", "out_seconds", "out", "source_out")


def _num(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def cut_points_from_cuts(cuts: Any) -> list[dict[str, Any]]:
    """Normalize a director cut list into in/out/cut points in source seconds."""
    if cuts is None:
        return []
    if isinstance(cuts, dict):
        if "cuts" in cuts or "segments" in cuts or "events" in cuts:
            points: list[dict[str, Any]] = []
            if "cuts" in cuts:
                points.extend(cut_points_from_cuts(cuts.get("cuts")))
            for key in ("segments", "events", "clips"):
                if key in cuts:
                    points.extend(cut_points_from_cuts(cuts.get(key)))
            return points
        return _points_from_segment(cuts, 0)
    points = []
    for index, item in enumerate(cuts):
        if isinstance(item, (int, float)):
            points.append({"at_seconds": float(item), "role": "cut", "segment_index": index})
            continue
        if isinstance(item, dict):
            points.extend(_points_from_segment(item, index))
    return points


def _points_from_segment(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    start = _num(item, _START_KEYS)
    end = _num(item, _END_KEYS)
    if end is None and start is not None:
        length = _num(item, ("length_seconds", "duration_seconds", "length"))
        if length is not None:
            end = start + length
    points: list[dict[str, Any]] = []
    extra = {k: item.get(k) for k in ("path", "source", "media_path", "speaker") if item.get(k)}
    if start is not None:
        points.append({"at_seconds": start, "role": "in", "segment_index": index, **extra})
    if end is not None:
        points.append({"at_seconds": end, "role": "out", "segment_index": index, **extra})
    return points


def _spans_from_speech(speech: Any) -> list[dict[str, Any]]:
    if speech is None:
        return []
    if isinstance(speech, dict):
        if "speech_spans" in speech:
            return _spans_from_speech(speech.get("speech_spans"))
        if "words" in speech:
            return speech_spans_from_words(list(speech.get("words") or []))
        if "spans" in speech:
            return _spans_from_speech(speech.get("spans"))
        pair = word_times(speech)
        if pair is not None:
            return [
                {
                    "start_seconds": pair[0],
                    "end_seconds": pair[1],
                    "text": speech.get("word") or speech.get("text") or "",
                    "speaker": speech.get("speaker"),
                }
            ]
        return []
    spans: list[dict[str, Any]] = []
    for item in speech:
        if not isinstance(item, dict):
            continue
        pair = word_times(item)
        if pair is None:
            continue
        spans.append(
            {
                "start_seconds": round(pair[0], 3),
                "end_seconds": round(pair[1], 3),
                "text": item.get("word") or item.get("text") or "",
                "speaker": item.get("speaker"),
                "word_count": item.get("word_count"),
            }
        )
    return spans


def check_speech_boundaries(
    cuts: Any,
    speech: Any,
    *,
    pad_seconds: float = 0.12,
    mode: str = "fail",
) -> dict[str, Any]:
    """Warn/fail when a cut lands inside speech, dilated by pad_seconds.

    A cut at exactly ``span.start - pad`` or ``span.end + pad`` is allowed
    (aligned to the padded boundary). A cut strictly inside
    ``(start - pad, end + pad)`` is a violation. Directors should snap in/out
    points to silence, not to the middle of a word or sentence.
    """
    if str(mode).lower() == "off":
        return {
            "id": "speech_boundaries",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "pad_seconds": float(pad_seconds),
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    points = cut_points_from_cuts(cuts)
    spans = _spans_from_speech(speech)
    pad = float(pad_seconds)
    violations: list[dict[str, Any]] = []
    for point in points:
        t = float(point["at_seconds"])
        for span in spans:
            start = float(span["start_seconds"])
            end = float(span["end_seconds"])
            lo = start - pad
            hi = end + pad
            if lo < t < hi:
                violations.append(
                    {
                        "at_seconds": round(t, 3),
                        "role": point.get("role") or "cut",
                        "segment_index": point.get("segment_index"),
                        "span": {
                            "start_seconds": start,
                            "end_seconds": end,
                            "text": span.get("text") or "",
                            "speaker": span.get("speaker"),
                        },
                        "detail": "cut lands inside speech",
                    }
                )
                break
    failed = bool(violations)
    return {
        "id": "speech_boundaries",
        "pass": not failed,
        "mode": str(mode).lower(),
        "pad_seconds": pad,
        "violations": violations,
        "value": len(violations),
        "limit": 0,
        "at_s": violations[0]["at_seconds"] if violations else None,
    }


def _turns_from_words(
    words: list[dict[str, Any]],
    gap_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for word in words:
        speaker = word.get("speaker")
        pair = word_times(word)
        if not speaker or pair is None:
            continue
        start, end = pair
        if (
            turns
            and turns[-1]["speaker"] == speaker
            and start - float(turns[-1]["end_seconds"]) <= gap_seconds
        ):
            turns[-1]["end_seconds"] = round(max(float(turns[-1]["end_seconds"]), end), 3)
            text = (word.get("word") or word.get("text") or "").strip()
            if text:
                turns[-1]["text"] = (turns[-1].get("text") or "") + " " + text
        else:
            turns.append(
                {
                    "speaker": str(speaker),
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "text": (word.get("word") or word.get("text") or "").strip(),
                }
            )
    for turn in turns:
        turn["text"] = " ".join(str(turn.get("text") or "").split())
    return turns


def _coverage_from_cuts(cuts: Any) -> list[tuple[float, float]]:
    points = cut_points_from_cuts(cuts)
    by_index: dict[int, dict[str, float]] = {}
    for point in points:
        idx = int(point.get("segment_index") or 0)
        role = point.get("role")
        t = float(point["at_seconds"])
        slot = by_index.setdefault(idx, {})
        if role == "in":
            slot["in"] = t
        elif role == "out":
            slot["out"] = t
        elif role == "cut":
            # Bare times are points, not ranges.
            continue
    ranges: list[tuple[float, float]] = []
    for slot in by_index.values():
        if "in" in slot and "out" in slot:
            a, b = slot["in"], slot["out"]
            if b < a:
                a, b = b, a
            ranges.append((a, b))
    ranges.sort()
    return ranges


def _covered(t: float, ranges: list[tuple[float, float]]) -> bool:
    return any(a <= t < b for a, b in ranges)


def check_turn_completeness(
    cuts: Any,
    words: list[dict[str, Any]] | None,
    *,
    reply_window_seconds: float = 2.5,
    mode: str = "warn",
) -> dict[str, Any]:
    """Fail/warn when a kept speaker turn drops the next speaker's reply.

    Generic: uses whatever speaker labels the transcript has. Does not decide
    which clip to keep; it only validates an existing cut list.
    """
    if str(mode).lower() == "off":
        return {
            "id": "turn_completeness",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    labeled = [w for w in (words or []) if w.get("speaker")]
    if not labeled:
        return {
            "id": "turn_completeness",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no speaker labels on words; skip turn-completeness",
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    ranges = _coverage_from_cuts(cuts)
    if not ranges:
        return {
            "id": "turn_completeness",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no kept segments in the cut list",
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    turns = _turns_from_words(labeled)
    window = float(reply_window_seconds)
    violations: list[dict[str, Any]] = []
    for i, turn in enumerate(turns[:-1]):
        nxt = turns[i + 1]
        if turn["speaker"] == nxt["speaker"]:
            continue
        if not _covered(float(turn["end_seconds"]) - 1e-6, ranges):
            continue
        gap = float(nxt["start_seconds"]) - float(turn["end_seconds"])
        if gap < 0 or gap > window:
            continue
        if _covered(float(nxt["start_seconds"]), ranges):
            continue
        violations.append(
            {
                "at_seconds": nxt["start_seconds"],
                "speaker": nxt["speaker"],
                "in_reply_to": turn["speaker"],
                "reply_window_seconds": window,
                "gap_seconds": round(gap, 3),
                "dropped_turn": {
                    "start_seconds": nxt["start_seconds"],
                    "end_seconds": nxt["end_seconds"],
                    "text": nxt.get("text") or "",
                    "speaker": nxt["speaker"],
                },
                "detail": "conversation partner reply dropped",
            }
        )
    failed = bool(violations)
    return {
        "id": "turn_completeness",
        "pass": not failed,
        "mode": str(mode).lower(),
        "reply_window_seconds": window,
        "violations": violations,
        "value": len(violations),
        "limit": 0,
        "at_s": violations[0]["at_seconds"] if violations else None,
    }
