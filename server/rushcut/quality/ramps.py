"""Flag speed-ramp steps that overlap voiced words."""
from __future__ import annotations

import copy
import re
from typing import Any

from ..analysis.transcript import word_times

DEFAULT_PAD_SECONDS = 0.08
_SPEED_KEYS = ("speed", "rate", "factor", "speed_factor", "playback_rate")
_START_KEYS = ("start_seconds", "start", "in_seconds", "in", "source_in", "t0")
_END_KEYS = ("end_seconds", "end", "out_seconds", "out", "source_out", "t1")
_STEP_KEYS = ("speed_ramp", "speed_ramps", "ramps", "steps", "speed_steps")
_BLEEP_TEXT = re.compile(r"^(\[bleep\]|\[censored\]|\[expletive\]|\*+)$", re.IGNORECASE)
_CONF_KEYS = ("confidence", "score", "prob", "probability")


def _num(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def _set_num(item: dict[str, Any], keys: tuple[str, ...], value: float) -> None:
    rounded = round(float(value), 4)
    for key in keys:
        if key in item:
            item[key] = rounded
            return
    item[keys[0]] = rounded


def _speed_of(item: dict[str, Any]) -> float | None:
    value = _num(item, _SPEED_KEYS)
    if value is None:
        return None
    return float(value)


def _is_bleeped(word: dict[str, Any]) -> bool:
    for key in ("bleeped", "bleep", "censored", "profanity", "expletive"):
        if word.get(key):
            return True
    text = str(word.get("word") or word.get("text") or "").strip()
    return bool(_BLEEP_TEXT.match(text))


def _confidence(word: dict[str, Any]) -> float:
    for key in _CONF_KEYS:
        if key in word and word[key] is not None:
            try:
                return float(word[key])
            except (TypeError, ValueError):
                continue
    return 1.0


def _words_list(words: Any) -> list[dict[str, Any]]:
    if words is None:
        return []
    if isinstance(words, dict):
        return list(words.get("words") or words.get("tokens") or [])
    return [w for w in words if isinstance(w, dict)]


def _eligible_words(words: Any, confidence_min: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in _words_list(words):
        if word_times(word) is None:
            continue
        if _is_bleeped(word):
            out.append(word)
            continue
        if _confidence(word) + 1e-12 < float(confidence_min):
            continue
        out.append(word)
    return out


def _word_label(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip()


def _collect_step_lists(obj: Any, trail: list[str] | None = None) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """Find stepped-ramp lists anywhere on an edit / plan / segment."""
    trail = trail or []
    found: list[tuple[list[dict[str, Any]], list[str]]] = []
    if isinstance(obj, dict):
        kind = str(obj.get("type") or obj.get("effect") or "").lower()
        for key in _STEP_KEYS:
            raw = obj.get(key)
            if (
                isinstance(raw, list)
                and raw
                and all(isinstance(x, dict) for x in raw)
                and (
                    key in {"ramps", "steps"}
                    or kind in {"speed_ramp", "speed", "ramp"}
                    or _speed_of(raw[0]) is not None
                )
            ):
                found.append((raw, trail + [key]))
        for key, value in obj.items():
            if key in _STEP_KEYS:
                continue
            if key in {"words", "transcript", "envelope", "must_include"}:
                continue
            found.extend(_collect_step_lists(value, trail + [key]))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_collect_step_lists(item, trail + [str(i)]))
    return found


def extract_ramp_steps(spec: Any) -> list[dict[str, Any]]:
    """Normalize ramps from a cut list, edit, plan, or explicit step list."""
    steps: list[dict[str, Any]] = []
    seen: set[tuple[float, float, float]] = set()

    def add(start: float, end: float, speed: float, *, extra: dict[str, Any] | None = None) -> None:
        key = (round(start, 4), round(end, 4), round(speed, 4))
        if key in seen:
            return
        seen.add(key)
        item = {
            "start_seconds": round(float(start), 4),
            "end_seconds": round(float(end), 4),
            "speed": float(speed),
            "step_index": len(steps),
        }
        if extra:
            for field in ("id", "path", "source", "label", "segment_index"):
                if extra.get(field) is not None:
                    item[field] = extra[field]
        steps.append(item)

    if spec is None:
        return []
    if isinstance(spec, list) and spec and all(isinstance(x, dict) and _speed_of(x) is not None for x in spec):
        for raw in spec:
            start = _num(raw, _START_KEYS)
            end = _num(raw, _END_KEYS)
            speed = _speed_of(raw)
            if start is None or end is None or speed is None:
                continue
            add(start, end, speed, extra=raw)
        return steps

    candidates: list[Any] = []
    if isinstance(spec, dict):
        for key in ("speed_ramps", "ramps", "effects", "plan", "edit"):
            if spec.get(key) is not None:
                candidates.append(spec.get(key))
        for key in ("segments", "events", "clips", "cuts"):
            if spec.get(key) is not None:
                candidates.append(spec.get(key))
        if _speed_of(spec) is not None or any(k in spec for k in _STEP_KEYS):
            candidates.append(spec)
    else:
        candidates.append(spec)

    def walk(node: Any, segment_index: int | None = None) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, i if segment_index is None else segment_index)
            return
        if not isinstance(node, dict):
            return
        kind = str(node.get("type") or node.get("effect") or "").lower()
        extra = {"id": node.get("id") or node.get("label"), "path": node.get("path"), "segment_index": segment_index}
        nested = False
        for key in _STEP_KEYS:
            raw = node.get(key)
            if not isinstance(raw, list):
                continue
            for raw_step in raw:
                if not isinstance(raw_step, dict):
                    continue
                start = _num(raw_step, _START_KEYS)
                end = _num(raw_step, _END_KEYS)
                speed = _speed_of(raw_step)
                if start is None or end is None or speed is None:
                    continue
                add(start, end, speed, extra={**extra, **raw_step})
                nested = True
        speed = _speed_of(node)
        start = _num(node, _START_KEYS)
        end = _num(node, _END_KEYS)
        if (
            speed is not None
            and start is not None
            and end is not None
            and not nested
            and (kind in {"speed_ramp", "speed", "ramp", ""} or speed != 1.0)
        ):
            add(start, end, speed, extra=extra)

    for candidate in candidates:
        walk(candidate)

    # Also pick up effects: [{type: speed_ramp, steps: [...]}] already covered.
    # Dedup already handled.
    for i, step in enumerate(steps):
        step["step_index"] = i
    return steps


def _first_overlapping_word(
    step: dict[str, Any],
    words: list[dict[str, Any]],
    pad_seconds: float,
) -> dict[str, Any] | None:
    start = float(step["start_seconds"])
    end = float(step["end_seconds"])
    pad = float(pad_seconds)
    hits: list[dict[str, Any]] = []
    for word in words:
        pair = word_times(word)
        if pair is None:
            continue
        ws, we = pair
        # Ramp may not extend past word.start - pad. Equality is allowed.
        if start < we and end > ws - pad:
            hits.append(word)
    if not hits:
        return None
    hits.sort(key=lambda w: word_times(w)[0])  # type: ignore[index]
    return hits[0]


def _shift_step_end(step_list: list[dict[str, Any]], index: int, new_end: float) -> None:
    if not (0 <= index < len(step_list)):
        return
    item = step_list[index]
    old_end = _num(item, _END_KEYS)
    _set_num(item, _END_KEYS if any(k in item for k in _END_KEYS) else ("end_seconds",), new_end)
    if old_end is None:
        return
    if index + 1 < len(step_list):
        nxt = step_list[index + 1]
        nxt_start = _num(nxt, _START_KEYS)
        if nxt_start is not None and abs(nxt_start - old_end) < 1e-3:
            _set_num(nxt, _START_KEYS, new_end)


def check_speed_ramps(
    spec: Any,
    words: Any = None,
    *,
    pad_seconds: float = DEFAULT_PAD_SECONDS,
    confidence_min: float = 0.0,
    shift: bool = False,
    mode: str = "fail",
) -> dict[str, Any]:
    """Fail when a ramp step with speed ≠ 1 covers a word timestamp.

    Stepped ramps (1/2/4/6x) are reported individually. Profanity-bleeped
    words always count. ``shift=True`` moves an offending step's end to
    ``word.start - pad`` (default pad 80 ms).
    """
    if str(mode).lower() == "off":
        return {
            "id": "speed_ramps",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "steps": [],
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    steps = extract_ramp_steps(spec)
    if not steps:
        return {
            "id": "speed_ramps",
            "ok": True,
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no speed-ramp steps on the spec",
            "steps": [],
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    eligible = _eligible_words(words, confidence_min)
    working = copy.deepcopy(spec) if shift else spec
    if shift:
        # Re-extract from the clone so we can mutate nested step lists.
        step_lists = _collect_step_lists(working)
        # Also handle whole-segment speed fields.
        for step in extract_ramp_steps(working):
            if abs(float(step["speed"]) - 1.0) < 1e-6:
                continue
            word = _first_overlapping_word(step, eligible, pad_seconds)
            if word is None:
                continue
            ws = float(word_times(word)[0])  # type: ignore[index]
            new_end = ws - float(pad_seconds)
            if new_end <= float(step["start_seconds"]):
                continue
            mutated = False
            for raw_list, _trail in step_lists:
                for i, raw in enumerate(raw_list):
                    rs = _num(raw, _START_KEYS)
                    re_ = _num(raw, _END_KEYS)
                    if rs is None or re_ is None:
                        continue
                    if abs(rs - float(step["start_seconds"])) < 1e-3 and abs(re_ - float(step["end_seconds"])) < 1e-3:
                        _shift_step_end(raw_list, i, new_end)
                        mutated = True
                        break
                if mutated:
                    break
            if not mutated and isinstance(working, dict):
                # Segment-level speed: shrink the segment / ramp end.
                for key in ("segments", "events", "clips", "cuts", "speed_ramps", "ramps"):
                    for item in working.get(key) or []:
                        if not isinstance(item, dict):
                            continue
                        if _num(item, _START_KEYS) == step["start_seconds"] and _speed_of(item) == step["speed"]:
                            _set_num(item, _END_KEYS if any(k in item for k in _END_KEYS) else ("end_seconds",), new_end)
            elif not mutated and isinstance(working, list):
                for item in working:
                    if not isinstance(item, dict):
                        continue
                    if _num(item, _START_KEYS) == step["start_seconds"] and _speed_of(item) == step["speed"]:
                        _set_num(item, _END_KEYS if any(k in item for k in _END_KEYS) else ("end_seconds",), new_end)
        steps = extract_ramp_steps(working)

    reported: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for step in steps:
        speed = float(step["speed"])
        word = None
        if abs(speed - 1.0) > 1e-6:
            word = _first_overlapping_word(step, eligible, pad_seconds)
        entry = {
            **step,
            "pass": word is None or abs(speed - 1.0) < 1e-6,
        }
        if word is not None and abs(speed - 1.0) > 1e-6:
            pair = word_times(word)
            ws, we = pair if pair else (None, None)
            suggested = None if ws is None else round(float(ws) - float(pad_seconds), 4)
            detail = {
                "word": _word_label(word),
                "word_start_seconds": ws,
                "word_end_seconds": we,
                "bleeped": _is_bleeped(word),
                "confidence": _confidence(word),
                "suggested_end_seconds": suggested,
                "detail": (
                    f"{speed:g}x ramp covers {_word_label(word)!r} at {ws:.3f}s"
                    if ws is not None
                    else f"{speed:g}x ramp covers a word"
                ),
            }
            entry.update(detail)
            viol = {
                "step_index": step.get("step_index"),
                "speed": speed,
                "start_seconds": step["start_seconds"],
                "end_seconds": step["end_seconds"],
                "at_seconds": ws,
                **detail,
            }
            violations.append(viol)
        reported.append(entry)

    failed = bool(violations)
    return {
        "id": "speed_ramps",
        "ok": True,
        "pass": not failed,
        "mode": str(mode).lower(),
        "pad_seconds": float(pad_seconds),
        "confidence_min": float(confidence_min),
        "shift": bool(shift),
        "steps": reported,
        "violations": violations,
        "spec": working if shift else None,
        "value": len(violations),
        "limit": 0,
        "detail": (
            "; ".join(v.get("detail") or "" for v in violations)
            if violations
            else None
        ),
    }
