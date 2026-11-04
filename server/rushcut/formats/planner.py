"""Beat sheets for story formats, and YouTube chapter lists from beats.

A beat is a point the caller logged on the footage: ``{t_sec, label,
value?}``, optionally with ``strength`` (a number) or ``cold_open`` (true).
``t_sec`` is seconds from the start of the footage. Nothing here opens
media or ranks footage.
"""
from __future__ import annotations

import itertools
import math
import re
import string
from typing import Any

from .library import format_ids, get_story_format

HOOK_SECONDS = 15.0
ENDING_SECONDS = 20.0
MIN_SECTION_SECONDS = 20.0
MAX_SECTIONS = 30
FLASH_FORWARD_SECONDS = 2.5
FLASH_FORWARD_LEAD_SECONDS = 1.0
VO_MAX_SECONDS = 10.0
START_STATE_MAX_S = 30.0  # a valued beat this early is the start state, not a finished step
CHAPTER_MIN_COUNT = 3
CHAPTER_MIN_SECONDS = 10.0
CHAPTER_TITLE_MAX = 60

# First number in a value: 1032798, "$1,032,798", "48.2K", "Tier 3", "3 of 10".
# A k/m/b suffix counts only when no letter follows it ("10 min" is 10).
_NUM_RE = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(?:([kKmMbB])(?![A-Za-z]))?")
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_value(value: Any) -> float | None:
    """Best-effort number from a beat value, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    match = _NUM_RE.search(str(value))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return number * _SUFFIX.get((match.group(2) or "").lower(), 1.0)


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value).strip()


def format_timestamp(seconds: float) -> str:
    """YouTube description timestamp: m:ss, or h:mm:ss from one hour."""
    total = max(0, math.floor(float(seconds) + 1e-6))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _window(start: float, end: float) -> str:
    return f"{format_timestamp(start)}-{format_timestamp(end)}"


def _normalize_beats(beats: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(beats, list) or not beats:
        return [], "beats must be a non-empty list of {t_sec, label, value?}"
    out: list[dict[str, Any]] = []
    for i, beat in enumerate(beats):
        if not isinstance(beat, dict):
            return [], f"beat {i} is not an object"
        raw_t = beat.get("t_sec", beat.get("t"))
        try:
            t = float(raw_t)
        except (TypeError, ValueError):
            return [], f"beat {i} needs a numeric t_sec"
        if not math.isfinite(t) or t < 0:
            return [], f"beat {i}: t_sec must be a finite number >= 0"
        item: dict[str, Any] = {
            "t_sec": round(t, 3),
            "label": str(beat.get("label") or "").strip() or f"Beat {i + 1}",
            "_order": i,
        }
        value = beat.get("value")
        if isinstance(value, str) and not value.strip():
            value = None
        if value is not None:
            item["value"] = value
            item["_number"] = parse_value(value)
        strength = beat.get("strength")
        if isinstance(strength, (int, float)) and not isinstance(strength, bool) and math.isfinite(strength):
            item["strength"] = float(strength)
        if beat.get("cold_open") is True:
            item["cold_open"] = True
        out.append(item)
    out.sort(key=lambda b: (b["t_sec"], b["_order"]))
    return out, None


def _public(beat: dict[str, Any]) -> dict[str, Any]:
    out = {"t_sec": beat["t_sec"], "label": beat["label"]}
    if "value" in beat:
        out["value"] = beat["value"]
    return out


def _title_for(beat: dict[str, Any]) -> str:
    title = beat["label"]
    if "value" in beat:
        shown = _display(beat["value"])
        if shown and shown.lower() not in title.lower():
            title = f"{title}: {shown}"
    if len(title) > CHAPTER_TITLE_MAX:
        title = title[: CHAPTER_TITLE_MAX - 3].rstrip() + "..."
    return title


class _KeepMissing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fill(template: str, slots: dict[str, str]) -> tuple[str, list[str]]:
    names = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    text = template.format_map(_KeepMissing(slots))
    return text, sorted(name for name in names if name not in slots)


def validate_chapters(
    chapters: list[dict[str, Any]],
    total_seconds: float | None = None,
    min_count: int = CHAPTER_MIN_COUNT,
    min_length_s: float = CHAPTER_MIN_SECONDS,
) -> dict[str, Any]:
    """YouTube rules: first chapter at 0:00, at least 3, each at least 10 s."""
    problems: list[str] = []
    if not chapters:
        return {"valid": False, "problems": ["no chapters"]}
    if float(chapters[0]["t_sec"]) != 0.0:
        problems.append("the first chapter must start at 0:00")
    if len(chapters) < min_count:
        problems.append(f"YouTube needs at least {min_count} chapters; got {len(chapters)}")
    for prev, nxt in itertools.pairwise(chapters):
        length = float(nxt["t_sec"]) - float(prev["t_sec"])
        if length < min_length_s:
            problems.append(
                f"'{prev['title']}' is {length:g} s long; each chapter needs {min_length_s:g} s or more"
            )
    if total_seconds is not None:
        tail = float(total_seconds) - float(chapters[-1]["t_sec"])
        if tail < min_length_s:
            problems.append(
                f"'{chapters[-1]['title']}' is {tail:g} s long; each chapter needs {min_length_s:g} s or more"
            )
    return {"valid": not problems, "problems": problems}


def _chapter_payload(chapters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    out = [
        {"t_sec": float(c["t_sec"]), "timestamp": format_timestamp(c["t_sec"]), "title": c["title"]}
        for c in chapters
    ]
    return out, "\n".join(f"{c['timestamp']} {c['title']}" for c in out)


def chapters_from_beats(
    beats: list[dict[str, Any]],
    total_seconds: float | None = None,
    min_chapter_seconds: float = CHAPTER_MIN_SECONDS,
    intro_title: str = "Intro",
) -> dict[str, Any]:
    """YouTube-valid chapters from beats on the *edited* timeline.

    The first chapter is forced to 0:00 (a beat in the first
    ``min_chapter_seconds`` moves there; otherwise ``intro_title`` is added).
    Beats closer than ``min_chapter_seconds`` to the previous chapter, or to
    the end when ``total_seconds`` is given, are dropped and listed.
    """
    norm, error = _normalize_beats(beats)
    if error:
        return {"ok": False, "error": error}
    total: float | None = None
    if total_seconds is not None:
        try:
            total = float(total_seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "total_seconds must be a number"}
        if not math.isfinite(total) or total <= 0:
            return {"ok": False, "error": "total_seconds must be > 0"}
    min_len = max(CHAPTER_MIN_SECONDS, float(min_chapter_seconds))

    chapters: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    rest = norm
    if norm[0]["t_sec"] < min_len:
        chapters.append({"t_sec": 0.0, "title": _title_for(norm[0])})
        rest = norm[1:]
    else:
        chapters.append({"t_sec": 0.0, "title": (intro_title or "Intro").strip() or "Intro"})
    for beat in rest:
        t = float(math.floor(beat["t_sec"]))
        title = _title_for(beat)
        if total is not None and t >= total:
            dropped.append({"t_sec": beat["t_sec"], "title": title, "reason": "after the end of the video"})
            continue
        if t - chapters[-1]["t_sec"] < min_len:
            dropped.append(
                {
                    "t_sec": beat["t_sec"],
                    "title": title,
                    "reason": f"less than {min_len:g} s after '{chapters[-1]['title']}'",
                }
            )
            continue
        chapters.append({"t_sec": t, "title": title})
    if total is not None:
        while len(chapters) > 1 and total - chapters[-1]["t_sec"] < min_len:
            last = chapters.pop()
            dropped.append(
                {"t_sec": last["t_sec"], "title": last["title"], "reason": f"less than {min_len:g} s before the end"}
            )
    check = validate_chapters(chapters, total, CHAPTER_MIN_COUNT, min_len)
    payload, text = _chapter_payload(chapters)
    result: dict[str, Any] = {
        "ok": check["valid"],
        "valid": check["valid"],
        "chapters": payload,
        "text": text,
        "dropped": dropped,
        "problems": check["problems"],
    }
    if not check["valid"]:
        result["error"] = "; ".join(check["problems"])
    return result


def _group_sections(beats: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Close a section on every beat that has a value; return (sections, tail)."""
    sections: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for beat in beats:
        current.append(beat)
        if "value" in beat:
            sections.append(current)
            current = []
    return sections, current


def _covers(sections: list[list[dict[str, Any]]], first_t: float) -> list[float]:
    covers = []
    prev_end = first_t
    for section in sections:
        end = section[-1]["t_sec"]
        covers.append(max(0.0, end - prev_end))
        prev_end = end
    return covers


def _merge_to(sections: list[list[dict[str, Any]]], limit: int, first_t: float) -> int:
    """Fold the shortest section into its neighbour until ``limit`` remain."""
    merges = 0
    while len(sections) > max(1, limit):
        covers = _covers(sections, first_t)
        k = covers.index(min(covers))
        if k == len(sections) - 1:
            k -= 1
        sections[k : k + 2] = [sections[k] + sections[k + 1]]
        merges += 1
    return merges


def _allocate(total: float, weights: list[float], floor: float) -> list[float]:
    """Split ``total`` in proportion to ``weights`` with every share >= ``floor``."""
    n = len(weights)
    fixed = [False] * n
    while True:
        free_total = total - floor * sum(fixed)
        free_weight = sum(w for w, f in zip(weights, fixed) if not f)
        free_count = n - sum(fixed)
        shares = [
            floor if fixed[i] else (free_total * weights[i] / free_weight if free_weight > 0 else free_total / free_count)
            for i in range(n)
        ]
        newly = [i for i in range(n) if not fixed[i] and shares[i] < floor]
        if not newly:
            return shares
        for i in newly:
            fixed[i] = True


def _pick_cold_open(beats: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    flagged = [b for b in beats if b.get("cold_open")]
    if flagged:
        return flagged[-1], "beat marked cold_open"
    strong = [b for b in beats if "strength" in b]
    if strong:
        return max(strong, key=lambda b: (b["strength"], b["t_sec"])), "highest strength"
    valued = [b for b in beats if b.get("_number") is not None]
    if valued:
        return max(valued, key=lambda b: (b["_number"], b["t_sec"])), "largest value (the payoff number)"
    return beats[-1], "last beat (no values or strengths given)"


def plan_story_edit(
    format_id: str,
    beats: list[dict[str, Any]],
    target_minutes: float,
    slots: dict[str, Any] | None = None,
    vo_gap_seconds: float = 8.0,
    max_sections: int | None = None,
) -> dict[str, Any]:
    """Beat sheet for one story format.

    Returns a cold-open pick, sections that each end on a beat with a value,
    a YouTube-valid chapter list for the planned cut, VO slots wherever two
    beats are ``vo_gap_seconds`` or more apart in the source, and title,
    thumbnail and hook lines filled from the format's templates. Placeholders
    the beats cannot fill come from ``slots``; anything still unfilled is
    left as ``{name}`` and listed in ``missing_slots``.
    """
    fmt = get_story_format(format_id)
    if fmt is None:
        return {"ok": False, "error": f"unknown format_id {format_id!r}", "available": format_ids()}
    norm, error = _normalize_beats(beats)
    if error:
        return {"ok": False, "error": error}
    try:
        minutes = float(target_minutes)
    except (TypeError, ValueError):
        return {"ok": False, "error": "target_minutes must be a number"}
    min_minutes = (HOOK_SECONDS + MIN_SECTION_SECONDS + ENDING_SECONDS) / 60.0
    if not math.isfinite(minutes) or minutes < min_minutes:
        return {
            "ok": False,
            "error": f"target_minutes must be at least {min_minutes:.2f} (hook + one section + ending)",
        }
    if slots is not None and not isinstance(slots, dict):
        return {"ok": False, "error": "slots must be an object of {name: text}"}
    try:
        vo_gap = float(vo_gap_seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "vo_gap_seconds must be a number"}
    if not math.isfinite(vo_gap) or vo_gap <= 0:
        return {"ok": False, "error": "vo_gap_seconds must be > 0"}

    warnings: list[str] = []
    lo, hi = fmt["length_minutes"]
    if not lo <= minutes <= hi:
        warnings.append(f"{fmt['name']} usually runs {lo}-{hi} min; the target is {minutes:g} min.")

    # t_sec counts from the start of the footage, so section 1 covers 0 -> its closing beat.
    first_t = 0.0
    source_span = norm[-1]["t_sec"]
    body_target = minutes * 60.0 - HOOK_SECONDS - ENDING_SECONDS
    body = max(MIN_SECTION_SECONDS, min(body_target, source_span))
    if source_span < body_target:
        warnings.append(
            f"The beats span {format_timestamp(source_span)} of footage, less than the "
            f"{format_timestamp(body_target)} the sections need for a {minutes:g} min cut. "
            f"The plan is {format_timestamp(body + HOOK_SECONDS + ENDING_SECONDS)} long; log beats "
            "across more footage or lower target_minutes."
        )

    sections, tail = _group_sections(norm)
    starts_on_value = bool(sections) and len(sections[0]) == 1 and sections[0][0]["t_sec"] < START_STATE_MAX_S
    if len(sections) > 1 and starts_on_value:
        # A value on the very first beat is the start state, not a finished step.
        sections[0:2] = [sections[0] + sections[1]]
    elif len(sections) == 1 and starts_on_value and tail:
        warnings.append("Only the first beat has a value; add values to the beats that close each step.")
        sections, tail = [sections[0] + tail], []
    values_missing = not sections
    if values_missing:
        warnings.append(
            "No beat has a value, so no section can end on a number. Add a value to the beat that closes each step."
        )
        sections, tail = [list(norm)], []
    limit = max(1, min(MAX_SECTIONS, int(body // MIN_SECTION_SECONDS)))
    if max_sections is not None:
        limit = max(1, min(limit, int(max_sections)))
    merges = _merge_to(sections, limit, first_t)
    if merges:
        warnings.append(
            f"Merged {merges} short section(s) to fit {limit} in {minutes:g} min; merged sections end on their last value."
        )
    shares = _allocate(body, [max(c, 1.0) for c in _covers(sections, first_t)], MIN_SECTION_SECONDS)

    cold, cold_reason = _pick_cold_open(norm)
    valued = [b for b in norm if "value" in b]
    filled_slots: dict[str, str] = {"n": str(len(sections)), "cold_open_label": cold["label"]}
    filled_slots["cold_open_value"] = _display(cold["value"]) if "value" in cold else cold["label"]
    if valued:
        filled_slots["start_value"] = _display(valued[0]["value"])
        filled_slots["end_value"] = _display(valued[-1]["value"])
    for key, val in (slots or {}).items():
        if val is not None and str(val).strip():
            filled_slots[str(key)] = str(val).strip()

    missing: set[str] = set()

    def fill(template: str) -> dict[str, Any]:
        text, gaps = _fill(template, filled_slots)
        missing.update(gaps)
        return {"text": text, "missing_slots": gaps}

    ff_start = max(0.0, cold["t_sec"] - FLASH_FORWARD_LEAD_SECONDS)
    cold_open = {
        **_public(cold),
        "reason": cold_reason,
        "flash_forward": {
            "source_start_s": round(ff_start, 3),
            "source_end_s": round(ff_start + FLASH_FORWARD_SECONDS, 3),
            "seconds": FLASH_FORWARD_SECONDS,
            "place_at_s": 0.0,
        },
    }
    hook = []
    for step in fmt["hook_0_15s"]:
        start, end = step["window_s"]
        line = fill(step["template"])
        hook.append({"window": _window(start, end), "window_s": [start, end], "beat": step["beat"], **line})

    narration = fmt["narration"]
    wpm_lo, wpm_hi = narration["wpm_target"]
    wpm_mid = (wpm_lo + wpm_hi) / 2.0
    bridges = narration.get("bridge_templates") or ["That put me at {value}."]

    plan_sections = []
    cursor = HOOK_SECONDS
    prev_end = first_t
    for i, (section, share) in enumerate(zip(sections, shares)):
        closing = section[-1]
        entry: dict[str, Any] = {
            "index": i + 1,
            "title": _title_for(closing),
            "source_from_s": prev_end,
            "source_to_s": closing["t_sec"],
            "beats": [_public(b) for b in section],
            "ends_on": _public(closing) if "value" in closing else None,
            "planned_start_s": float(round(cursor)),
            "planned_start": format_timestamp(round(cursor)),
            "planned_seconds": round(share, 1),
        }
        if "value" in closing:
            entry["closing_card"] = f"{closing['label']}: {_display(closing['value'])}"
        else:
            entry["warning"] = "ends without a value; add one to the closing beat"
        plan_sections.append(entry)
        cursor += share
        prev_end = closing["t_sec"]
    ending_start = float(round(cursor))
    planned_total = float(round(cursor + ENDING_SECONDS))
    ending = {
        "planned_start_s": ending_start,
        "planned_start": format_timestamp(ending_start),
        "planned_seconds": ENDING_SECONDS,
        "rule": fmt.get("ending") or "Verdict, then subscribe.",
        "beats": [_public(b) for b in tail],
    }

    goal = filled_slots.get("goal")
    hook_title = f"The goal: {goal}" if goal else "The goal"
    if len(hook_title) > CHAPTER_TITLE_MAX:
        hook_title = hook_title[: CHAPTER_TITLE_MAX - 3].rstrip() + "..."
    chapter_rows = [{"t_sec": 0.0, "title": hook_title}]
    chapter_rows += [{"t_sec": s["planned_start_s"], "title": s["title"]} for s in plan_sections]
    chapter_rows.append({"t_sec": ending_start, "title": "Verdict"})
    check = validate_chapters(chapter_rows, planned_total)
    chapters, chapters_text = _chapter_payload(chapter_rows)

    section_of = {id(b): s["index"] for s, group in zip(plan_sections, sections) for b in group}
    vo_slots = []
    last_value: str | None = None
    for a, b in itertools.pairwise(norm):
        if "value" in a:
            last_value = _display(a["value"])
        gap = b["t_sec"] - a["t_sec"]
        if gap < vo_gap:
            continue
        if "value" in b:
            purpose = "open_loop"
            note = f"Tease the number '{b['label']}' lands on without saying it yet."
        elif "value" in a:
            purpose = "bridge"
            note = f"Past-tense bridge from {last_value} into the next step."
        else:
            purpose = "bridge"
            note = "Past-tense bridge that keeps the goal in view."
        vo_seconds = round(min(gap, VO_MAX_SECONDS), 1)
        template = bridges[len(vo_slots) % len(bridges)]
        # Lines restate the last number already shown, never the one coming up.
        line = template.format_map(_KeepMissing({"value": last_value} if last_value else {}))
        slot = {
            "after": _public(a),
            "before": _public(b),
            "section": section_of.get(id(b)),
            "source_start_s": a["t_sec"],
            "source_end_s": b["t_sec"],
            "gap_s": round(gap, 3),
            "purpose": purpose,
            "note": note,
            "suggested_vo_seconds": vo_seconds,
            "suggested_words": round(vo_seconds * wpm_mid / 60.0),
            "example_line": line,
        }
        if gap > 3 * VO_MAX_SECONDS:
            slot["cut_note"] = "Source gap is long: cut most of it and cover the jump in one VO line."
        vo_slots.append(slot)

    thumb = fmt["thumbnail_template"]
    result = {
        "ok": True,
        "format": {"id": fmt["id"], "name": fmt["name"]},
        "target_minutes": minutes,
        "planned_seconds": planned_total,
        "planned_length": format_timestamp(planned_total),
        "source_span_s": round(source_span, 3),
        "compression": round(source_span / body, 2) if body > 0 and source_span > 0 else None,
        "cold_open": cold_open,
        "hook": hook,
        "sections": plan_sections,
        "ending": ending,
        "chapters": chapters,
        "chapters_text": chapters_text,
        "chapters_valid": check["valid"],
        "chapter_problems": check["problems"],
        "vo_slots": vo_slots,
        "vo_gap_seconds": vo_gap,
        "titles": [fill(t) for t in fmt["title_templates"]],
        "thumbnails": {
            "layout": thumb.get("layout"),
            "text_options": [fill(t) for t in thumb.get("text_options") or []],
            "notes": thumb.get("notes") or [],
        },
        "narration": {"style": narration.get("style"), "wpm_target": [wpm_lo, wpm_hi],
                      "overall_wpm_min": narration.get("overall_wpm_min")},
        "section_end_rule": fmt["section_end_rule"],
        "chapters_guidance": fmt["chapters"],
        "pitfalls": fmt["pitfalls"],
        "slots_used": filled_slots,
        "missing_slots": sorted(missing),
        "warnings": warnings,
    }
    return result


_START_KEYS = ("start_seconds", "start", "in_seconds", "in", "source_in")
_END_KEYS = ("end_seconds", "end", "out_seconds", "out", "source_out")
_SPEED_KEYS = ("speed", "rate", "playback_rate", "speed_factor")
DEFAULT_CAP_SECONDS = 30.0
DEFAULT_CAP_FPS = 30.0


def _first_num(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def _segments_from_spec(spec: Any) -> list[dict[str, Any]]:
    if spec is None:
        return []
    if isinstance(spec, dict):
        for key in ("segments", "clips", "events", "cuts"):
            if key in spec and spec[key] is not None:
                return _segments_from_spec(spec[key])
        return [spec]
    if isinstance(spec, list):
        out: list[dict[str, Any]] = []
        for item in spec:
            if isinstance(item, dict):
                out.append(item)
        return out
    return []


def _source_span(segment: dict[str, Any]) -> float:
    start = _first_num(segment, _START_KEYS)
    end = _first_num(segment, _END_KEYS)
    if start is not None and end is not None:
        return max(0.0, end - start)
    length = _first_num(segment, ("length_seconds", "duration_seconds", "length", "source_seconds"))
    if length is not None:
        return max(0.0, length)
    return 0.0


def _ramp_output_seconds(source_seconds: float, keypoints: list[dict[str, Any]]) -> float:
    """Integrate dt_source / speed along a piecewise-linear speed ramp."""
    if source_seconds <= 0:
        return 0.0
    points: list[tuple[float, float]] = []
    for raw in keypoints:
        if not isinstance(raw, dict):
            continue
        at = _first_num(raw, ("at_seconds", "at", "t", "source_seconds"))
        speed = _first_num(raw, _SPEED_KEYS)
        if at is None or speed is None:
            continue
        points.append((at, max(1e-6, speed)))
    if not points:
        return source_seconds
    points.sort()
    if points[0][0] > 0:
        points.insert(0, (0.0, points[0][1]))
    if points[-1][0] < source_seconds:
        points.append((source_seconds, points[-1][1]))
    total = 0.0
    for (t0, s0), (t1, s1) in zip(points, points[1:]):
        span = t1 - t0
        if span <= 0:
            continue
        if abs(s1 - s0) < 1e-9:
            total += span / s0
        else:
            total += span * math.log(s1 / s0) / (s1 - s0)
    return total


def _segment_output_seconds(segment: dict[str, Any]) -> float:
    source = _source_span(segment)
    ramp = segment.get("speed_ramp") or segment.get("ramp") or segment.get("speed_keyframes")
    if isinstance(ramp, list) and ramp:
        return _ramp_output_seconds(source, ramp)
    speed = _first_num(segment, _SPEED_KEYS)
    if speed is None:
        speed = 1.0
    speed = max(1e-6, float(speed))
    return source / speed


def predicted_output_seconds(spec: Any) -> float:
    """Output timeline length from a segment spec, including speed ramps."""
    return sum(_segment_output_seconds(seg) for seg in _segments_from_spec(spec))


def fits_cap(spec: Any, max_seconds: float | None = None, fps: float | None = None) -> dict[str, Any]:
    """Predict output duration from a segment spec and compare it to a cap.

    Speed ramps are integrated as piecewise-linear ``source / speed``. The
    prediction is intended to land within ±1 frame of a later render so trims
    can be planned before encode.
    """
    payload = spec if isinstance(spec, dict) else {"segments": spec}
    if max_seconds is None:
        shorts = payload.get("shorts") if isinstance(payload, dict) else None
        if isinstance(shorts, dict) and shorts.get("max_seconds") is not None:
            max_seconds = float(shorts["max_seconds"])
        else:
            raw = payload.get("max_seconds") if isinstance(payload, dict) else None
            max_seconds = float(raw) if raw is not None else DEFAULT_CAP_SECONDS
    if fps is None:
        raw_fps = payload.get("fps") if isinstance(payload, dict) else None
        fps = float(raw_fps) if raw_fps else DEFAULT_CAP_FPS
    fps = float(fps) if fps and float(fps) > 0 else DEFAULT_CAP_FPS
    frame = 1.0 / fps
    predicted = predicted_output_seconds(spec)
    limit = float(max_seconds)
    fits = predicted < limit
    return {
        "ok": True,
        "fits": fits,
        "predicted_seconds": round(predicted, 6),
        "max_seconds": limit,
        "fps": fps,
        "frame_seconds": frame,
        "over_seconds": round(predicted - limit, 6),
        "within_one_frame": abs(predicted - round(predicted / frame) * frame) <= frame + 1e-9,
    }
