"""Zoom curves, crop-path interpolation, and easing."""
from __future__ import annotations

import math
from typing import Any


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def smoothstep(t: float, start: float, end: float) -> float:
    """0 at start, 1 at end, cubic Hermite (smoothstep)."""
    if end <= start:
        return 1.0 if t >= end else 0.0
    u = clamp((t - start) / (end - start), 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def ease_weight(t: float, t_in: float, t_out: float, ease: float) -> float:
    """0 outside [t_in, t_out], 1 in the hold, smoothstep on the edges."""
    ease = max(0.0, float(ease))
    if t_out <= t_in:
        return 0.0
    span = t_out - t_in
    edge = min(ease, span / 2.0)
    if t < t_in or t > t_out:
        return 0.0
    if edge <= 1e-9:
        return 1.0
    if t < t_in + edge:
        return smoothstep(t, t_in, t_in + edge)
    if t > t_out - edge:
        return 1.0 - smoothstep(t, t_out - edge, t_out)
    return 1.0


def _as_center(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return float(value.get("x", 0.5)), float(value.get("y", 0.5))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return None


def _as_rect(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if all(k in value for k in ("x", "y", "w", "h")):
            return float(value["x"]), float(value["y"]), float(value["w"]), float(value["h"])
        if all(k in value for k in ("x0", "y0", "x1", "y1")):
            x0, y0, x1, y1 = (float(value[k]) for k in ("x0", "y0", "x1", "y1"))
            return x0, y0, x1 - x0, y1 - y0
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return float(value[0]), float(value[1]), float(value[2]), float(value[3])
    return None


def _punch_from_dict(raw: dict[str, Any], src_w: float, src_h: float) -> dict[str, float]:
    scale = float(raw.get("scale") or 1.0)
    center = _as_center(raw.get("center"))
    rect = _as_rect(raw.get("rect"))
    if rect and src_w > 0 and src_h > 0:
        x, y, w, h = rect
        if w > 0 and h > 0:
            scale = max(scale, src_w / w, src_h / h)
            center = ( (x + w / 2.0) / src_w, (y + h / 2.0) / src_h )
    if center is None:
        center = (0.5, 0.5)
    t_in = float(raw.get("t_in", raw.get("start", raw.get("start_seconds", 0.0))) or 0.0)
    t_out = float(raw.get("t_out", raw.get("end", raw.get("end_seconds", t_in))) or t_in)
    ease = float(raw.get("ease", raw.get("ease_seconds", 0.15)) or 0.0)
    return {
        "scale": max(1.0, scale),
        "cx": clamp(center[0], 0.0, 1.0),
        "cy": clamp(center[1], 0.0, 1.0),
        "t_in": t_in,
        "t_out": t_out,
        "ease": max(0.0, ease),
    }


def collect_punches(effect: dict[str, Any], src_w: float = 1920.0, src_h: float = 1080.0) -> list[dict[str, float]]:
    punches = list(effect.get("punches") or [])
    if not punches:
        if any(k in effect for k in ("scale", "center", "rect", "t_in", "t_out")):
            punches = [effect]
    return [_punch_from_dict(p, src_w, src_h) for p in punches if isinstance(p, dict)]


def zoom_at(
    t: float,
    punches: list[dict[str, float]],
) -> tuple[float, float, float]:
    """Merged zoom curve: max of overlapping punches, no 1.0 dip between them.

    Centre is a (scale-1)-weighted blend so the window slides between punches.
    """
    scale = 1.0
    cx_acc = 0.0
    cy_acc = 0.0
    weight = 0.0
    for punch in punches:
        w = ease_weight(t, punch["t_in"], punch["t_out"], punch["ease"])
        if w <= 0:
            continue
        s = 1.0 + (punch["scale"] - 1.0) * w
        scale = max(scale, s)
        mass = max(0.0, s - 1.0)
        cx_acc += punch["cx"] * mass
        cy_acc += punch["cy"] * mass
        weight += mass
    if weight > 1e-9:
        return scale, cx_acc / weight, cy_acc / weight
    return scale, 0.5, 0.5


def zoom_samples(
    punches: list[dict[str, float]],
    duration: float,
    fps: float,
) -> list[tuple[float, float, float, float]]:
    frames = max(1, int(math.ceil(max(0.0, duration) * fps)))
    out: list[tuple[float, float, float, float]] = []
    for i in range(frames):
        t = i / fps
        scale, cx, cy = zoom_at(t, punches)
        out.append((t, scale, cx, cy))
    return out


def _key_time(key: Any) -> float:
    if not isinstance(key, dict):
        return 0.0
    return float(key.get("t", key.get("time", key.get("at", key.get("at_seconds", 0.0)))) or 0.0)


def _key_rect(key: Any) -> tuple[float, float, float, float]:
    if not isinstance(key, dict):
        return 0.0, 0.0, 0.0, 0.0
    rect = _as_rect(key.get("rect") or key)
    if rect is None:
        return 0.0, 0.0, 0.0, 0.0
    return rect


def interpolate_rect(
    keys: list[Any],
    t: float,
    *,
    ease: str = "linear",
) -> tuple[float, float, float, float]:
    """Linear or eased interpolation of a keyframed crop path."""
    if not keys:
        return 0.0, 0.0, 0.0, 0.0
    ordered = sorted((k for k in keys if isinstance(k, dict)), key=_key_time)
    if not ordered:
        return 0.0, 0.0, 0.0, 0.0
    if t <= _key_time(ordered[0]):
        return _key_rect(ordered[0])
    if t >= _key_time(ordered[-1]):
        return _key_rect(ordered[-1])
    for left, right in zip(ordered, ordered[1:]):
        t0 = _key_time(left)
        t1 = _key_time(right)
        if t0 <= t <= t1:
            u = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            if str(ease).lower() in {"ease", "smooth", "smoothstep"}:
                u = u * u * (3.0 - 2.0 * u)
            a = _key_rect(left)
            b = _key_rect(right)
            return tuple(a[i] + (b[i] - a[i]) * u for i in range(4))  # type: ignore[return-value]
    return _key_rect(ordered[-1])


def ffmpeg_zoom_expr(punches: list[dict[str, float]]) -> tuple[str, str, str]:
    """ffmpeg expressions for scale / cx / cy using max() of punch envelopes."""
    if not punches:
        return "1", "0.5", "0.5"
    scale_terms = ["1"]
    cx_num = []
    cy_num = []
    mass = []
    for punch in punches:
        t_in = punch["t_in"]
        t_out = punch["t_out"]
        ease = max(punch["ease"], 1e-4)
        edge = min(ease, max(1e-4, (t_out - t_in) / 2.0))
        # weight 0..1
        fade_in = (
            f"(min(max((t-{t_in:.6f})/{edge:.6f},0),1)"
            f"*min(max((t-{t_in:.6f})/{edge:.6f},0),1)"
            f"*(3-2*min(max((t-{t_in:.6f})/{edge:.6f},0),1)))"
        )
        fade_out = (
            f"(min(max((t-{t_out - edge:.6f})/{edge:.6f},0),1)"
            f"*min(max((t-{t_out - edge:.6f})/{edge:.6f},0),1)"
            f"*(3-2*min(max((t-{t_out - edge:.6f})/{edge:.6f},0),1)))"
        )
        w = (
            f"if(lt(t,{t_in:.6f})+gt(t,{t_out:.6f}),0,"
            f"if(lt(t,{t_in + edge:.6f}),{fade_in},"
            f"if(gt(t,{t_out - edge:.6f}),1-{fade_out},1)))"
        )
        s = f"(1+({punch['scale']:.6f}-1)*({w}))"
        scale_terms.append(s)
        m = f"max({s}-1,0)"
        mass.append(m)
        cx_num.append(f"({punch['cx']:.6f}*{m})")
        cy_num.append(f"({punch['cy']:.6f}*{m})")
    scale = "max(" + ",".join(scale_terms) + ")" if len(scale_terms) == 2 else _max_tree(scale_terms)
    mass_sum = "+".join(mass)
    cx = f"if(gt({mass_sum},0.000001),({'+'.join(cx_num)})/({mass_sum}),0.5)"
    cy = f"if(gt({mass_sum},0.000001),({'+'.join(cy_num)})/({mass_sum}),0.5)"
    return scale, cx, cy


def _max_tree(terms: list[str]) -> str:
    if len(terms) == 1:
        return terms[0]
    if len(terms) == 2:
        return f"max({terms[0]},{terms[1]})"
    mid = len(terms) // 2
    return f"max({_max_tree(terms[:mid])},{_max_tree(terms[mid:])})"
