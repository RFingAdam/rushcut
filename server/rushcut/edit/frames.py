"""Thumbnail compositor and post-render frame checks."""
from __future__ import annotations

import json
import math
import os
from typing import Any

from ..analysis.probe import probe_media
from ..output_policy import check_output_path
from .mediaio import extract_frame, laplacian_var, mean_luma
from .text import bundled_font_path


def pick_frame(
    path: str,
    start: float = 0.0,
    end: float | None = None,
    *,
    avoid_blink: bool = False,
    avoid_motion: bool = False,
    step: float | None = None,
) -> dict[str, Any]:
    """Return the sharpest frame (Laplacian variance) in [start, end)."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    duration = float(probe.get("duration_seconds") or 0)
    start = max(0.0, float(start))
    end = duration if end is None else min(duration, float(end))
    if end <= start:
        return {"ok": False, "error": "end must be greater than start", "field": "end"}
    fps = float(probe.get("fps") or 24.0) or 24.0
    stride = float(step) if step else max(1.0 / fps, (end - start) / 24.0)
    best_t = start
    best_score = -1.0
    prev_rgb = None
    scores: list[dict[str, Any]] = []
    t = start
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    # Sample, don't load every frame of a long range.
    while t < end - 1e-6:
        grabbed = extract_frame(path, t)
        if not grabbed.get("ok"):
            t += stride
            continue
        rgb = grabbed["rgb"]
        w = int(grabbed.get("width") or width)
        h = int(grabbed.get("height") or height)
        score = laplacian_var(rgb, w, h)
        if avoid_blink and mean_luma(rgb, w, h) < 12:
            score *= 0.15
        if avoid_motion and prev_rgb is not None and len(prev_rgb) == len(rgb):
            diff = 0
            step_px = max(1, (w * h) // 4000)
            for i in range(0, len(rgb), 3 * step_px):
                diff += abs(rgb[i] - prev_rgb[i])
            motion = diff / max(1, (len(rgb) // (3 * step_px)))
            if motion > 18:
                score *= 0.4
        scores.append({"t": t, "score": score})
        if score > best_score:
            best_score = score
            best_t = t
        prev_rgb = rgb
        t += stride
    if best_score < 0:
        return {"ok": False, "error": "no frames decoded"}
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "t": float(best_t),
        "time": float(best_t),
        "score": float(best_score),
        "start": start,
        "end": end,
        "candidates": scores,
    }


def _load_image(path: str):
    from PIL import Image

    return Image.open(path).convert("RGBA")


def _draw_text_block(canvas, text: str, box: tuple[int, int, int, int], fill, stroke=(0, 0, 0, 255)) -> None:
    from PIL import ImageDraw, ImageFont

    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(canvas)
    max_w = max(8, x1 - x0)
    size = max(24, int((y1 - y0) * 0.55))
    font_path = bundled_font_path()
    font = ImageFont.truetype(font_path, size)
    while size > 16:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=6)
        if bbox[2] - bbox[0] <= max_w:
            break
        size -= 4
        font = ImageFont.truetype(font_path, size)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=6)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x0 + max(0, (max_w - tw) // 2)
    y = y0 + max(0, (y1 - y0 - th) // 2)
    draw.text((x + 3, y + 4), text, font=font, fill=(0, 0, 0, 200), stroke_width=6, stroke_fill=(0, 0, 0, 200))
    draw.text((x, y), text, font=font, fill=fill, stroke_width=6, stroke_fill=stroke)


def compose_thumbnail(
    frames: list[str] | str,
    output_path: str,
    *,
    layout: str = "single",
    texts: list[str] | None = None,
    size: str | tuple[int, int] = "1280x720",
    arrows: bool = False,
    boxes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Single-frame or left/right split thumbnail with big stroked text."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return {"ok": False, "error": "pillow is required for compose_thumbnail"}
    refused = check_output_path(output_path)
    if refused:
        return refused
    if isinstance(size, str):
        if "x" in size:
            w_s, h_s = size.lower().split("x", 1)
            out_w, out_h = int(w_s), int(h_s)
        elif size in {"vertical", "9:16"}:
            out_w, out_h = 1080, 1920
        else:
            out_w, out_h = 1280, 720
    else:
        out_w, out_h = int(size[0]), int(size[1])
    paths = [frames] if isinstance(frames, str) else list(frames)
    if not paths:
        return {"ok": False, "error": "frames is required", "field": "frames"}
    canvas = Image.new("RGB", (out_w, out_h), (8, 8, 10))
    mode = str(layout or "single").lower()
    if mode in {"split", "side", "compare"} and len(paths) >= 2:
        left = _load_image(paths[0]).convert("RGB")
        right = _load_image(paths[1]).convert("RGB")
        half = out_w // 2
        canvas.paste(left.resize((half, out_h)), (0, 0))
        canvas.paste(right.resize((out_w - half, out_h)), (half, 0))
        draw = ImageDraw.Draw(canvas)
        draw.line([(half, 0), (half, out_h)], fill=(255, 255, 255), width=6)
        if arrows:
            mid_y = out_h // 2
            draw.polygon([(half - 80, mid_y), (half - 20, mid_y - 36), (half - 20, mid_y + 36)], fill=(255, 214, 10))
            draw.polygon([(half + 80, mid_y), (half + 20, mid_y - 36), (half + 20, mid_y + 36)], fill=(255, 214, 10))
        labels = list(texts or [])
        if labels:
            rgba = canvas.convert("RGBA")
            _draw_text_block(rgba, labels[0], (20, 20, half - 20, int(out_h * 0.28)), (255, 255, 255, 255))
            if len(labels) > 1:
                _draw_text_block(rgba, labels[1], (half + 20, 20, out_w - 20, int(out_h * 0.28)), (255, 214, 10, 255))
            canvas = rgba.convert("RGB")
    else:
        img = _load_image(paths[0]).convert("RGB").resize((out_w, out_h))
        canvas.paste(img, (0, 0))
        if texts:
            rgba = canvas.convert("RGBA")
            _draw_text_block(rgba, texts[0], (40, 40, out_w - 40, int(out_h * 0.28)), (255, 255, 255, 255))
            canvas = rgba.convert("RGB")
    if boxes:
        draw = ImageDraw.Draw(canvas)
        for box in boxes:
            x, y, w, h = (int(box.get(k, 0)) for k in ("x", "y", "w", "h"))
            color = str(box.get("color") or "#FFD60A")
            draw.rectangle((x, y, x + w, y + h), outline=color, width=int(box.get("width") or 8))
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    canvas.save(output_path, "PNG")
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "width": out_w,
        "height": out_h,
        "layout": mode,
    }


def _normalize_times(times: list[Any] | None, duration: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in times or []:
        if isinstance(raw, dict):
            t = float(raw.get("t", raw.get("time", raw.get("seconds", 0.0))) or 0.0)
            label = str(raw.get("label") or raw.get("name") or "")
        else:
            t = float(raw)
            label = ""
        t = max(0.0, min(duration, t))
        items.append({"t": t, "label": label})
    return items


def frame_check(
    path: str,
    times: list[Any] | None = None,
    output_dir: str | None = None,
    *,
    include_ends: bool = True,
    cols: int = 4,
    profile: Any = None,
) -> dict[str, Any]:
    """Grab labelled frames + a tiled contact sheet + JSON."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    duration = float(probe.get("duration_seconds") or 0)
    dest = output_dir
    if dest is None and profile is not None:
        dest = getattr(profile, "frame_check_dir", None) or getattr(
            getattr(profile, "review", None), "frame_check_dir", None
        )
    if dest is None:
        dest = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", "frame_check")
    refused = check_output_path(os.path.join(dest, "sheet.png"))
    if refused:
        return refused
    os.makedirs(dest, exist_ok=True)
    stamps = _normalize_times(times, duration)
    if include_ends:
        last_t = max(0.0, duration - 1.0 / max(float(probe.get("fps") or 24.0), 1.0))
        stamps = [{"t": 0.0, "label": "first"}] + stamps + [{"t": last_t, "label": "last"}]
    seen = set()
    unique: list[dict[str, Any]] = []
    for item in stamps:
        key = round(item["t"], 3)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {"ok": False, "error": "pillow is required for frame_check"}
    thumbs: list[dict[str, Any]] = []
    images = []
    font = ImageFont.truetype(bundled_font_path(), 20)
    for i, item in enumerate(unique):
        png = os.path.join(dest, f"frame_{i:02d}_{item['t']:.3f}.png".replace(".", "p", 1))
        grabbed = extract_frame(path, item["t"], dest=png)
        if not grabbed.get("ok") and item.get("label") == "last":
            grabbed = extract_frame(path, max(0.0, item["t"] - 0.05), dest=png)
        if not grabbed.get("ok"):
            continue
        img = Image.open(png).convert("RGB")
        images.append(img)
        thumbs.append(
            {
                "t": item["t"],
                "label": item["label"],
                "path": os.path.abspath(png),
            }
        )
    if not images:
        return {"ok": False, "error": "no frames extracted"}
    tw, th = 320, 180
    resized = [im.resize((tw, th)) for im in images]
    columns = max(1, int(cols))
    rows = int(math.ceil(len(resized) / columns))
    sheet = Image.new("RGB", (columns * tw, rows * (th + 28)), (16, 16, 18))
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(resized):
        r, c = divmod(i, columns)
        x, y = c * tw, r * (th + 28)
        sheet.paste(im, (x, y))
        label = thumbs[i]["label"]
        caption = f"{thumbs[i]['t']:.2f}s" + (f"  {label}" if label else "")
        draw.text((x + 6, y + th + 4), caption, font=font, fill=(230, 230, 230))
    sheet_path = os.path.join(dest, "sheet.png")
    sheet.save(sheet_path, "PNG")
    report = {
        "ok": True,
        "path": os.path.abspath(path),
        "output_dir": os.path.abspath(dest),
        "sheet": os.path.abspath(sheet_path),
        "frames": thumbs,
        "duration_seconds": duration,
    }
    json_path = os.path.join(dest, "frame_check.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
    report["json_path"] = os.path.abspath(json_path)
    return report


def compose_layout(
    path: str,
    output_path: str,
    layout: str = "blur_fill",
    **params: Any,
) -> dict[str, Any]:
    """File-level layout compositor (same graphs as the timeline effect)."""
    from .apply import apply_edit

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    duration = float(probe.get("duration_seconds") or 0.04)
    effect = {"type": "compose_layout", "layout": layout, **params}
    edit = {
        "segments": [{"source": path, "in": 0.0, "out": duration, "effects": [effect]}],
        "output": {
            "width": int(params.get("width") or 1080),
            "height": int(params.get("height") or 1920),
            "fps": float(params.get("fps") or probe.get("fps") or 30),
        },
        "join_fade_ms": 0,
    }
    return apply_edit(edit, output_path)
