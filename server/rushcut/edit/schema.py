"""edit.json load + validation. Errors name the bad field."""
from __future__ import annotations

import json
import os
from typing import Any

SCHEMA_NAME = "rushcut.edit/v1"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.json")
EFFECT_TYPES = {
    "zoom",
    "speed_ramp",
    "lens_zoom",
    "compose_layout",
    "blur_regions",
    "overlay",
}
OUTPUT_PRESETS = {
    "vertical": (1080, 1920, 30.0),
    "1080p": (1920, 1080, 30.0),
    "4k": (3840, 2160, 30.0),
}


class EditError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def schema_document() -> dict[str, Any]:
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _num(value: Any, field: str, *, minimum: float | None = None, exclusive_min: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditError(field, "must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise EditError(field, f"must be >= {minimum}")
    if exclusive_min is not None and number <= exclusive_min:
        raise EditError(field, f"must be > {exclusive_min}")
    return number


def _opt_num(value: Any, field: str, **kwargs: Any) -> float | None:
    if value is None:
        return None
    return _num(value, field, **kwargs)


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditError(field, "must be a non-empty string")
    return value


def _effect(effect: Any, field: str) -> dict[str, Any]:
    if not isinstance(effect, dict):
        raise EditError(field, "must be an object")
    kind = effect.get("type") or effect.get("name") or effect.get("id")
    if not isinstance(kind, str) or not kind.strip():
        raise EditError(f"{field}.type", "must be a non-empty string")
    if kind not in EFFECT_TYPES:
        raise EditError(f"{field}.type", f"unknown effect {kind!r}")
    item = dict(effect)
    item["type"] = kind
    if kind == "speed_ramp":
        _validate_speed_ramp(item, field)
    elif kind == "zoom":
        _validate_zoom(item, field)
    elif kind == "blur_regions":
        regions = item.get("regions")
        if regions is not None and not isinstance(regions, list):
            raise EditError(f"{field}.regions", "must be a list")
    elif kind == "overlay":
        if not item.get("path") and not item.get("source"):
            raise EditError(f"{field}.path", "is required")
    elif kind == "compose_layout":
        layout = str(item.get("layout") or item.get("mode") or "center")
        if layout not in {"blur_fill", "split", "center", "square"}:
            raise EditError(f"{field}.layout", f"unknown layout {layout!r}")
        item["layout"] = layout
    return item


def _validate_zoom(effect: dict[str, Any], field: str) -> None:
    if effect.get("scale") is not None:
        scale = _num(effect["scale"], f"{field}.scale", exclusive_min=0)
        if scale < 1.0:
            raise EditError(f"{field}.scale", "must be >= 1")
    if effect.get("punches") is not None and not isinstance(effect.get("punches"), list):
        raise EditError(f"{field}.punches", "must be a list")
    for i, punch in enumerate(effect.get("punches") or []):
        if not isinstance(punch, dict):
            raise EditError(f"{field}.punches[{i}]", "must be an object")
        if punch.get("scale") is not None:
            _num(punch["scale"], f"{field}.punches[{i}].scale", exclusive_min=0)


def _validate_speed_ramp(effect: dict[str, Any], field: str) -> None:
    steps = effect.get("steps") or effect.get("ramps")
    if steps is None:
        raise EditError(f"{field}.steps", "is required")
    if not isinstance(steps, list) or not steps:
        raise EditError(f"{field}.steps", "must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise EditError(f"{field}.steps[{i}]", "must be an object")
        start = step.get("start", step.get("start_seconds", step.get("t0")))
        end = step.get("end", step.get("end_seconds", step.get("t1")))
        factor = step.get("factor", step.get("speed", step.get("rate")))
        _num(start, f"{field}.steps[{i}].start", minimum=0)
        _num(end, f"{field}.steps[{i}].end", exclusive_min=0)
        speed = _num(factor, f"{field}.steps[{i}].factor", exclusive_min=0)
        if speed < 1.0 or speed > 8.0:
            raise EditError(f"{field}.steps[{i}].factor", "must be in 1..8")
    audio = str(effect.get("audio") or effect.get("audio_mode") or "atempo")
    if audio not in {"atempo", "mute", "keep_original_length"}:
        raise EditError(f"{field}.audio", "must be atempo, mute, or keep_original_length")


def _segment(segment: Any, field: str) -> dict[str, Any]:
    if not isinstance(segment, dict):
        raise EditError(field, "must be an object")
    source = segment.get("source") or segment.get("path") or segment.get("media_path")
    src = _str(source, f"{field}.source")
    start = segment.get("in", segment.get("start", segment.get("start_seconds", 0.0)))
    end = segment.get("out", segment.get("end", segment.get("end_seconds")))
    in_s = _num(start, f"{field}.in", minimum=0)
    if end is None:
        raise EditError(f"{field}.out", "is required")
    out_s = _num(end, f"{field}.out", exclusive_min=0)
    if out_s <= in_s:
        raise EditError(f"{field}.out", "must be greater than in")
    audio_source = segment.get("audio_source")
    if audio_source is not None:
        audio_source = _str(audio_source, f"{field}.audio_source")
    audio_offset = _opt_num(segment.get("audio_offset"), f"{field}.audio_offset")
    effects = []
    raw_effects = segment.get("effects") or []
    if not isinstance(raw_effects, list):
        raise EditError(f"{field}.effects", "must be a list")
    for i, effect in enumerate(raw_effects):
        effects.append(_effect(effect, f"{field}.effects[{i}]"))
    item = dict(segment)
    item.update(
        {
            "source": src,
            "in": in_s,
            "out": out_s,
            "audio_source": audio_source,
            "audio_offset": 0.0 if audio_offset is None else audio_offset,
            "effects": effects,
        }
    )
    return item


def _output(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise EditError("output", "must be an object")
    preset = raw.get("preset")
    width = raw.get("width")
    height = raw.get("height")
    fps = raw.get("fps")
    if preset is not None:
        key = str(preset).lower()
        if key not in OUTPUT_PRESETS:
            raise EditError("output.preset", f"unknown preset {preset!r}")
        dw, dh, df = OUTPUT_PRESETS[key]
        width = int(width or dw)
        height = int(height or dh)
        fps = float(fps or df)
        preset = key
    else:
        width = int(width or 1920)
        height = int(height or 1080)
        fps = float(fps or 30.0)
        preset = None
    if width < 2:
        raise EditError("output.width", "must be >= 2")
    if height < 2:
        raise EditError("output.height", "must be >= 2")
    if fps <= 0:
        raise EditError("output.fps", "must be > 0")
    return {
        **raw,
        "preset": preset,
        "width": width,
        "height": height,
        "fps": fps,
    }


def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("segments"), list):
        return data
    edit = data.get("edit")
    if isinstance(edit, dict) and isinstance(edit.get("segments"), list):
        merged = dict(data)
        merged["segments"] = edit["segments"]
        if "output" not in merged and "output" in edit:
            merged["output"] = edit["output"]
        if "join_fade_ms" not in merged and "join_fade_ms" in edit:
            merged["join_fade_ms"] = edit["join_fade_ms"]
        if "effects" not in merged and "effects" in edit:
            merged["effects"] = edit["effects"]
        return merged
    timeline = data.get("timeline")
    if isinstance(timeline, dict) and isinstance(timeline.get("segments"), list):
        merged = dict(data)
        merged.update(timeline)
        return merged
    return data


def validate_edit(data: Any) -> dict[str, Any]:
    """Return a normalized edit document or raise EditError."""
    if not isinstance(data, dict):
        raise EditError("$", "edit.json must be an object")
    data = _unwrap(data)
    segments = data.get("segments")
    if segments is None:
        raise EditError("segments", "is required")
    if not isinstance(segments, list) or not segments:
        raise EditError("segments", "must be a non-empty list")
    normalized = [_segment(seg, f"segments[{i}]") for i, seg in enumerate(segments)]
    fade = data.get("join_fade_ms", 8.0)
    fade_ms = _num(fade, "join_fade_ms", minimum=0)
    if fade_ms > 50:
        raise EditError("join_fade_ms", "must be <= 50")
    global_effects = []
    raw_global = data.get("effects") or []
    if not isinstance(raw_global, list):
        raise EditError("effects", "must be a list")
    for i, effect in enumerate(raw_global):
        global_effects.append(_effect(effect, f"effects[{i}]"))
    out = dict(data)
    out["schema"] = data.get("schema") or SCHEMA_NAME
    out["segments"] = normalized
    out["output"] = _output(data.get("output"))
    out["join_fade_ms"] = fade_ms
    out["effects"] = global_effects
    return out


def load_edit(source: str | dict[str, Any]) -> dict[str, Any]:
    """Load an edit.json path, sidecar, or already-parsed dict."""
    if isinstance(source, dict):
        return validate_edit(source)
    if not isinstance(source, str) or not source.strip():
        raise EditError("edit_json", "must be a path or object")
    path = os.path.abspath(source)
    if not os.path.isfile(path):
        raise EditError("edit_json", f"file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        raise EditError("edit_json", f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EditError("edit_json", "JSON root must be an object")
    return validate_edit(payload)


def validate_edit_result(source: str | dict[str, Any]) -> dict[str, Any]:
    """ok/error wrapper used by tools."""
    try:
        edit = load_edit(source) if not isinstance(source, dict) else validate_edit(source)
    except EditError as exc:
        return {"ok": False, "error": str(exc), "field": exc.field}
    return {"ok": True, "edit": edit}


def looks_like_edit(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(
            value.get("segments")
            or (isinstance(value.get("edit"), dict) and value["edit"].get("segments"))
            or (isinstance(value.get("timeline"), dict) and value["timeline"].get("segments"))
        )
    if isinstance(value, str) and value.lower().endswith((".json", ".edit.json")):
        return os.path.isfile(value)
    return False
