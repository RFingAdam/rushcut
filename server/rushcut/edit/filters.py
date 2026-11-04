"""FFmpeg filter-graph builders for timeline effects."""
from __future__ import annotations

import os
from typing import Any

from .curves import collect_punches, ffmpeg_zoom_expr, interpolate_rect


def escape_path(path: str) -> str:
    text = os.path.abspath(path).replace("\\", "/")
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def escape_text(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", r"\'")
        .replace("%", "%%")
    )


def atempo_chain(factor: float) -> str:
    remaining = float(factor)
    parts: list[str] = []
    if remaining <= 0:
        remaining = 1.0
    while remaining > 2.0001:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.4999:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


def speed_output_length(source_len: float, steps: list[dict[str, Any]]) -> float:
    edges = {0.0, float(source_len)}
    parsed: list[tuple[float, float, float]] = []
    for step in steps:
        start = float(step.get("start", step.get("start_seconds", step.get("t0", 0.0))) or 0.0)
        end = float(step.get("end", step.get("end_seconds", step.get("t1", source_len))) or source_len)
        factor = float(step.get("factor", step.get("speed", step.get("rate", 1.0))) or 1.0)
        factor = min(8.0, max(1.0, factor))
        start = max(0.0, min(source_len, start))
        end = max(0.0, min(source_len, end))
        if end <= start:
            continue
        parsed.append((start, end, factor))
        edges.add(start)
        edges.add(end)
    ordered = sorted(edges)
    total = 0.0
    for a, b in zip(ordered, ordered[1:]):
        if b <= a:
            continue
        factor = 1.0
        for start, end, rate in parsed:
            if start <= a + 1e-9 and end >= b - 1e-9:
                factor = rate
        total += (b - a) / factor
    return total


def normalize_steps(effect: dict[str, Any], source_len: float) -> list[dict[str, float]]:
    steps = []
    for step in effect.get("steps") or effect.get("ramps") or []:
        if not isinstance(step, dict):
            continue
        start = float(step.get("start", step.get("start_seconds", step.get("t0", 0.0))) or 0.0)
        end = float(step.get("end", step.get("end_seconds", step.get("t1", source_len))) or source_len)
        factor = float(step.get("factor", step.get("speed", step.get("rate", 1.0))) or 1.0)
        steps.append(
            {
                "start": max(0.0, min(source_len, start)),
                "end": max(0.0, min(source_len, end)),
                "factor": min(8.0, max(1.0, factor)),
            }
        )
    if not steps:
        steps = [{"start": 0.0, "end": source_len, "factor": 1.0}]
    return steps


def zoom_filter(effect: dict[str, Any], src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    punches = collect_punches(effect, src_w, src_h)
    z, cx, cy = ffmpeg_zoom_expr(punches)
    # Crop a 1/z window around (cx, cy) then scale back to the output frame.
    return (
        f"crop=w='iw/({z})':h='ih/({z})':"
        f"x='({cx})*iw-iw/({z})/2':y='({cy})*ih-ih/({z})/2',"
        f"scale={out_w}:{out_h}:flags=lanczos"
    )


def layout_filter(
    effect: dict[str, Any],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> str:
    layout = str(effect.get("layout") or effect.get("mode") or "center")
    keys = effect.get("crop_path") or effect.get("keys")
    main_raw = effect.get("main") or effect.get("rect") or effect.get("crop")
    if main_raw is None and keys:
        main_raw = interpolate_rect(list(keys), float(effect.get("t") or 0.0), ease=str(effect.get("ease") or "linear"))
    main = _rect(main_raw, src_w, src_h)
    extras = list(effect.get("strips") or effect.get("extra") or [])
    if layout == "square":
        side = min(out_w, out_h)
        return _cover_crop(main, src_w, src_h, side, side) + f",scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2"
    if layout == "center":
        return _cover_crop(main, src_w, src_h, out_w, out_h)
    if layout == "blur_fill":
        mx, my, mw, mh = main
        bg_blur = int(effect.get("blur") or effect.get("bg_blur") or 20)
        darken = float(effect.get("darken") or effect.get("bg_darken") or 0.12)
        return (
            f"split[bg][fg];"
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},gblur=sigma={max(2.0, bg_blur / 3.0):.2f},eq=brightness=-{darken:.4f}[blur];"
            f"[fg]crop={mw}:{mh}:{mx}:{my},scale={out_w}:-2:force_original_aspect_ratio=decrease[main];"
            f"[blur][main]overlay=(W-w)/2:(H-h)/2"
            + _extra_strip_chain(extras, src_w, src_h, out_w, out_h)
        )
    if layout == "split":
        return _split_layout(effect, main, src_w, src_h, out_w, out_h)
    return _cover_crop(main, src_w, src_h, out_w, out_h)


def _cover_crop(rect: tuple[int, int, int, int], src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}"
        )
    return f"crop={w}:{h}:{x}:{y},scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"


def _split_layout(
    effect: dict[str, Any],
    main: tuple[int, int, int, int],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> str:
    mx, my, mw, mh = main
    inset = effect.get("inset") or {}
    if not isinstance(inset, dict):
        inset = {}
    ix, iy, iw, ih = _rect(inset.get("rect") or inset.get("crop") or inset, src_w, src_h)
    pos_x = int(inset.get("x", inset.get("out_x", out_w - min(out_w, 420) - 24)))
    pos_y = int(inset.get("y", inset.get("out_y", int(out_h * 0.62))))
    out_iw = int(inset.get("width", inset.get("out_w", min(420, out_w // 3))))
    out_ih = int(inset.get("height", inset.get("out_h", int(out_iw * 9 / 16))))
    border = int(inset.get("border", inset.get("border_width", 4)))
    color = str(inset.get("border_color") or inset.get("color") or "white").lstrip("#")
    if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color):
        color = f"0x{color}"
    pad = max(0, border)
    main_h = int(effect.get("main_height") or min(out_h, int(out_w * mh / max(mw, 1))))
    main_h = max(2, main_h - (main_h % 2))
    out_iw = max(2, out_iw - (out_iw % 2))
    out_ih = max(2, out_ih - (out_ih % 2))
    chain = (
        f"split=3[bg][fg][cam];"
        f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},gblur=sigma=6,eq=brightness=-0.15[blur];"
        f"[fg]crop={mw}:{mh}:{mx}:{my},scale={out_w}:{main_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{main_h}:(ow-iw)/2:0[main];"
        f"[blur][main]overlay=0:0[base];"
        f"[cam]crop={iw}:{ih}:{ix}:{iy},scale={out_iw}:{out_ih},"
        f"pad={out_iw + 2 * pad}:{out_ih + 2 * pad}:{pad}:{pad}:color={color}[ins];"
        f"[base][ins]overlay={pos_x}:{pos_y}"
    )
    return chain + _extra_strip_chain(effect.get("strips") or [], src_w, src_h, out_w, out_h)


def _extra_strip_chain(strips: list[Any], src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    if not strips:
        return ""
    # Applied after the previous overlay by splitting is awkward; keep a
    # sequential overlay suffix that assumes the last label is the current frame.
    parts = [""]
    current = "v"
    # Caller graphs already end on an unlabeled default stream when used as -vf.
    # Extra strips are appended as a second filter_complex pass in apply.py.
    _ = (src_w, src_h, out_w, out_h, current, parts)
    return ""


def extra_strip_overlay(
    path_label: str,
    strip: dict[str, Any],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> tuple[str, int, int]:
    x, y, w, h = _rect(strip.get("rect") or strip.get("crop") or strip, src_w, src_h)
    tw = int(strip.get("width") or strip.get("out_w") or out_w)
    th = int(strip.get("height") or strip.get("out_h") or max(2, int(h * tw / max(w, 1))))
    ty = int(strip.get("y") or strip.get("out_y") or 0)
    tx = int(strip.get("x") or strip.get("out_x") or (out_w - tw) // 2)
    _ = (path_label, out_h)
    return f"crop={w}:{h}:{x}:{y},scale={tw}:{th}", tx, ty


def blur_region_filter(
    region: dict[str, Any],
    src_w: int,
    src_h: int,
    t: float | None = None,
) -> dict[str, Any]:
    rect = region.get("rect") or region.get("crop")
    keys = region.get("keys") or region.get("path") or region.get("keyframes")
    if keys:
        rect = interpolate_rect(list(keys), float(t or 0.0), ease=str(region.get("ease") or "linear"))
    x, y, w, h = _rect(rect, src_w, src_h)
    mode = str(region.get("mode") or "blur").lower()
    strength = float(region.get("strength") or region.get("amount") or 12)
    feather = int(region.get("feather") or 0)
    start = float(region.get("start", region.get("t_in", 0.0)) or 0.0)
    end = region.get("end", region.get("t_out"))
    color = str(region.get("color") or "black")
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "mode": mode,
        "strength": strength,
        "feather": max(0, feather),
        "start": start,
        "end": None if end is None else float(end),
        "color": color,
    }


def region_enable(region: dict[str, Any]) -> str:
    start = float(region["start"])
    end = region["end"]
    if end is None:
        return f"gte(t,{start:.6f})"
    return f"between(t,{start:.6f},{float(end):.6f})"


def blur_vf(regions: list[dict[str, Any]], src_w: int, src_h: int) -> str:
    """Build a -vf chain that only touches listed rects."""
    if not regions:
        return "null"
    parts: list[str] = []
    for region in regions:
        spec = blur_region_filter(region, src_w, src_h)
        enable = region_enable(spec)
        x, y, w, h = spec["x"], spec["y"], spec["w"], spec["h"]
        feather = spec["feather"]
        cx, cy = x - feather, y - feather
        cw, ch = w + 2 * feather, h + 2 * feather
        cx = max(0, cx)
        cy = max(0, cy)
        cw = min(src_w - cx, cw)
        ch = min(src_h - cy, ch)
        if spec["mode"] in {"fill", "solid"}:
            color = spec["color"]
            parts.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill:enable='{enable}'")
            continue
        if spec["mode"] in {"pixelate", "pixel", "mosaic"}:
            block = max(2, int(spec["strength"]))
            parts.append(
                f"split[base][roi];"
                f"[roi]crop={cw}:{ch}:{cx}:{cy},scale=iw/{block}:ih/{block}:flags=neighbor,"
                f"scale={cw}:{ch}:flags=neighbor[fx];"
                f"[base][fx]overlay={cx}:{cy}:enable='{enable}'"
            )
            continue
        sigma = max(0.5, float(spec["strength"]) / 3.0)
        parts.append(
            f"split[base][roi];"
            f"[roi]crop={cw}:{ch}:{cx}:{cy},gblur=sigma={sigma:.3f}[fx];"
            f"[base][fx]overlay={cx}:{cy}:enable='{enable}'"
        )
    # Multiple split/overlay stages cannot sit in one -vf list unless chained
    # as a filter_complex. Use a single-region -vf or the complex builder.
    if len(parts) == 1 and "split" not in parts[0]:
        return parts[0]
    return parts[0] if len(parts) == 1 else "__complex__"


def blur_filter_complex(regions: list[dict[str, Any]], src_w: int, src_h: int) -> tuple[str, str]:
    """Return (filter_complex, last_video_label)."""
    current = "0:v"
    if not regions:
        return "[0:v]null[vout]", "vout"
    parts: list[str] = []
    for i, region in enumerate(regions):
        spec = blur_region_filter(region, src_w, src_h)
        enable = region_enable(spec)
        x, y, w, h = spec["x"], spec["y"], spec["w"], spec["h"]
        feather = spec["feather"]
        cx, cy = max(0, x - feather), max(0, y - feather)
        cw = min(src_w - cx, w + 2 * feather)
        ch = min(src_h - cy, h + 2 * feather)
        src = current if current.startswith("0:") else f"{current}"
        src_ref = f"[{src}]" if not src.startswith("[") else src
        if i == 0 and current == "0:v":
            src_ref = "[0:v]format=gbrp,"
        if spec["mode"] in {"fill", "solid"}:
            nxt = f"v{i}"
            color = spec["color"]
            parts.append(
                f"{src_ref}drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill:enable='{enable}'[{nxt}]"
            )
            current = nxt
            continue
        base, roi, fx, nxt = f"b{i}", f"r{i}", f"fx{i}", f"v{i}"
        if spec["mode"] in {"pixelate", "pixel", "mosaic"}:
            block = max(2, int(spec["strength"]))
            parts.append(
                f"{src_ref}split[{base}][{roi}];"
                f"[{roi}]crop={cw}:{ch}:{cx}:{cy},scale=iw/{block}:ih/{block}:flags=neighbor,"
                f"scale={cw}:{ch}:flags=neighbor[{fx}];"
                f"[{base}][{fx}]overlay={cx}:{cy}:format=gbrp:enable='{enable}'[{nxt}]"
            )
        else:
            sigma = max(0.5, float(spec["strength"]) / 3.0)
            parts.append(
                f"{src_ref}split[{base}][{roi}];"
                f"[{roi}]crop={cw}:{ch}:{cx}:{cy},gblur=sigma={sigma:.3f}[{fx}];"
                f"[{base}][{fx}]overlay={cx}:{cy}:format=gbrp:enable='{enable}'[{nxt}]"
            )
        current = nxt
    return ";".join(parts), current


def overlay_filter(
    effect: dict[str, Any],
    out_w: int,
    out_h: int,
) -> dict[str, Any]:
    path = str(effect.get("path") or effect.get("source") or "")
    scale = float(effect.get("scale") or 1.0)
    opacity = float(effect.get("opacity") or 1.0)
    fade = float(effect.get("fade") or effect.get("fade_seconds") or 0.0)
    start = float(effect.get("start", effect.get("t_in", 0.0)) or 0.0)
    end = effect.get("end", effect.get("t_out"))
    anchor = str(effect.get("anchor") or effect.get("safe") or "")
    x, y = _anchor_xy(anchor, out_w, out_h, effect)
    return {
        "path": path,
        "x": x,
        "y": y,
        "scale": scale,
        "opacity": opacity,
        "fade": fade,
        "start": start,
        "end": None if end is None else float(end),
        "sequence": bool(effect.get("sequence") or effect.get("png_sequence")),
        "fps": float(effect.get("fps") or 30.0),
    }


SAFE_ANCHORS_9X16 = {
    "top_left": (48, 160),
    "top_center": ("(W-w)/2", 160),
    "top_right": ("W-w-48", 160),
    "center": ("(W-w)/2", "(H-h)/2"),
    "bottom_left": (48, "H-h-360"),
    "bottom_center": ("(W-w)/2", "H-h-360"),
    "bottom_right": ("W-w-48", "H-h-360"),
    "safe_title": ("(W-w)/2", 220),
    "safe_lower": ("(W-w)/2", "H-h-420"),
    "logo": ("W-w-40", 48),
    "watermark": ("W-w-40", 48),
}


def _anchor_xy(anchor: str, out_w: int, out_h: int, effect: dict[str, Any]) -> tuple[Any, Any]:
    if "x" in effect or "y" in effect:
        return effect.get("x", 0), effect.get("y", 0)
    key = anchor.lower().replace("-", "_")
    if out_h > out_w and key in SAFE_ANCHORS_9X16:
        return SAFE_ANCHORS_9X16[key]
    landscape = {
        "top_left": (32, 32),
        "top_center": ("(W-w)/2", 32),
        "top_right": ("W-w-32", 32),
        "center": ("(W-w)/2", "(H-h)/2"),
        "bottom_left": (32, "H-h-32"),
        "bottom_center": ("(W-w)/2", "H-h-32"),
        "bottom_right": ("W-w-32", "H-h-32"),
        "logo": ("W-w-32", 32),
        "watermark": ("W-w-32", 32),
    }
    return landscape.get(key, (effect.get("x", 32), effect.get("y", 32)))


def _rect(value: Any, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    if value is None:
        return 0, 0, int(src_w), int(src_h)
    if isinstance(value, dict):
        if "x0" in value:
            x0, y0 = float(value["x0"]), float(value["y0"])
            x1, y1 = float(value.get("x1", src_w)), float(value.get("y1", src_h))
            x, y, w, h = x0, y0, x1 - x0, y1 - y0
        else:
            x = float(value.get("x", value.get("left", 0)) or 0)
            y = float(value.get("y", value.get("top", 0)) or 0)
            w = float(value.get("w", value.get("width", src_w)) or src_w)
            h = float(value.get("h", value.get("height", src_h)) or src_h)
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        x, y, w, h = (float(v) for v in value[:4])
    else:
        return 0, 0, int(src_w), int(src_h)
    if 0 < w <= 1 and 0 < h <= 1 and 0 <= x <= 1 and 0 <= y <= 1:
        x, y, w, h = x * src_w, y * src_h, w * src_w, h * src_h
    xi, yi = int(round(x)), int(round(y))
    wi, hi = max(2, int(round(w))), max(2, int(round(h)))
    xi = max(0, min(xi, src_w - 2))
    yi = max(0, min(yi, src_h - 2))
    wi = min(wi, src_w - xi)
    hi = min(hi, src_h - yi)
    return xi, yi, wi, hi
