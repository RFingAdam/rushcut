"""Text title cards and the bundled OFL font."""
from __future__ import annotations

import os
from typing import Any

from ..output_policy import check_output_path

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
BUNDLED_FONT = os.path.join(FONT_DIR, "LiberationSans-Regular.ttf")


def bundled_font_path() -> str:
    return BUNDLED_FONT


def _parse_color(value: Any, default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        nums = [int(v) for v in value]
        if len(nums) == 3:
            nums.append(255)
        return int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
    text = str(value).strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 6:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 255
    if len(text) == 8:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), int(text[6:8], 16)
    named = {
        "white": (255, 255, 255, 255),
        "black": (0, 0, 0, 255),
        "yellow": (255, 214, 10, 255),
        "red": (220, 38, 38, 255),
    }
    return named.get(text.lower(), default)


def _load_font(path: str | None, size: int):
    from PIL import ImageFont

    font_path = path or BUNDLED_FONT
    try:
        return ImageFont.truetype(font_path, size)
    except OSError:
        return ImageFont.truetype(BUNDLED_FONT, size)


def _text_size(draw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    return int(box[2] - box[0]), int(box[3] - box[1])


def make_text_card(
    lines: list[str] | str,
    output_path: str | None = None,
    *,
    style: dict[str, Any] | None = None,
    width: int = 1080,
    height: int | None = None,
) -> dict[str, Any]:
    """Render a 1-3 line title card PNG with stroke, shadow and a rounded box."""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError:
        return {"ok": False, "error": "pillow is required for make_text_card"}
    _ = ImageFont
    style = dict(style or {})
    if isinstance(lines, str):
        lines = [ln for ln in lines.split("\n") if ln]
    rows = [str(ln) for ln in (lines or []) if str(ln)]
    if not rows:
        return {"ok": False, "error": "lines is required", "field": "lines"}
    if len(rows) > 3:
        rows = rows[:3]
    font_path = style.get("font") or style.get("font_path") or BUNDLED_FONT
    size = int(style.get("size") or style.get("font_size") or 72)
    max_width = int(style.get("max_width") or int(width * 0.86))
    colors_raw = style.get("colors") or style.get("line_colors") or []
    default_colors = [(255, 255, 255, 255), (255, 214, 10, 255), (255, 255, 255, 255)]
    colors = []
    for i, row in enumerate(rows):
        if i < len(colors_raw):
            colors.append(_parse_color(colors_raw[i], default_colors[min(i, 2)]))
        else:
            colors.append(default_colors[min(i, 2)])
    stroke = int(style.get("stroke") or style.get("stroke_width") or 6)
    stroke_color = _parse_color(style.get("stroke_color") or "black", (0, 0, 0, 255))
    shadow = int(style.get("shadow") or style.get("shadow_px") or 8)
    box_color = _parse_color(style.get("box_color") or "#000000CC", (0, 0, 0, 204))
    radius = int(style.get("radius") or style.get("box_radius") or 28)
    padding = int(style.get("padding") or 28)
    # Auto-shrink until every line fits max_width.
    fitted = size
    dummy = Image.new("RGBA", (max(8, max_width), 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    while fitted > 12:
        font = _load_font(font_path, fitted)
        widths = [_text_size(draw, row, font)[0] for row in rows]
        if max(widths) + 2 * stroke <= max_width:
            break
        fitted -= 2
    font = _load_font(font_path, fitted)
    line_sizes = [_text_size(draw, row, font) for row in rows]
    gap = max(8, fitted // 6)
    text_w = max(s[0] for s in line_sizes)
    text_h = sum(s[1] for s in line_sizes) + gap * (len(rows) - 1)
    card_w = min(width, text_w + 2 * padding + 2 * stroke)
    card_h = text_h + 2 * padding + 2 * stroke
    if height is None:
        height = card_h + 2 * shadow
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    box_x = (width - card_w) // 2
    box_y = (height - card_h) // 2
    box = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    box_draw = ImageDraw.Draw(box)
    box_draw.rounded_rectangle(
        (box_x, box_y, box_x + card_w, box_y + card_h),
        radius=radius,
        fill=box_color,
    )
    if shadow:
        shadow_img = box.filter(ImageFilter.GaussianBlur(radius=max(1, shadow // 2)))
        canvas.alpha_composite(shadow_img, (0, shadow // 3))
    canvas.alpha_composite(box)
    draw = ImageDraw.Draw(canvas)
    y = box_y + padding
    for row, (lw, lh), color in zip(rows, line_sizes, colors):
        x = (width - lw) // 2
        if shadow:
            draw.text(
                (x + 2, y + 3),
                row,
                font=font,
                fill=(0, 0, 0, 180),
                stroke_width=stroke,
                stroke_fill=(0, 0, 0, 180),
            )
        draw.text(
            (x, y),
            row,
            font=font,
            fill=color,
            stroke_width=stroke,
            stroke_fill=stroke_color,
        )
        y += lh + gap
    if not output_path:
        output_path = os.path.join(os.getcwd(), "title_card.png")
    refused = check_output_path(output_path)
    if refused:
        return refused
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    canvas.save(output_path, "PNG")
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "width": width,
        "height": height,
        "font": os.path.abspath(font_path),
        "font_size": fitted,
        "lines": rows,
        "bundled_font": os.path.abspath(BUNDLED_FONT),
    }
