"""Named render presets. Template names are matched against VEGAS's list."""
from __future__ import annotations

from typing import Any

RENDER_PRESETS: dict[str, dict[str, Any]] = {
    "vertical": {
        "name": "vertical",
        "width": 1080,
        "height": 1920,
        "label": "Vertical 1080x1920",
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": [
            "1080x1920",
            "1080",
            "1920",
            "vertical",
            "tiktok",
            "9:16",
            "9x16",
            "portrait",
        ],
    },
    "1080p": {
        "name": "1080p",
        "width": 1920,
        "height": 1080,
        "label": "1080p landscape",
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": ["1920x1080", "1080", "1920"],
    },
    "4k": {
        "name": "4k",
        "width": 3840,
        "height": 2160,
        "label": "4K landscape",
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": ["2160", "3840", "4k", "4K", "UHD"],
    },
}

# MAGIX AVC/AAC MP4 templates sometimes omit "MP4" from FileTypeName.
_MP4_ALIASES = ("mp4", "avc", "aac", "hevc", "x264", "x265", "mpeg-4", "mpeg4")


def _renderer_blob(t: dict[str, Any]) -> str:
    return f"{t.get('renderer') or ''} {t.get('renderer_name') or ''} {t.get('template') or ''}".lower()


def _renderer_matches(blob: str, renderer_f: str) -> bool:
    if not renderer_f:
        return True
    if renderer_f in blob:
        return True
    if renderer_f == "mp4" and any(tok in blob for tok in _MP4_ALIASES):
        return True
    return False


def _score_templates(
    templates: list[dict[str, Any]],
    spec: dict[str, Any],
    *,
    require_renderer: bool,
) -> list[tuple[int, dict[str, Any]]]:
    width, height = spec["width"], spec["height"]
    prefers = [s.lower() for s in spec.get("prefer_template_substrings") or [] if s]
    renderer_f = (spec.get("renderer_contains") or "").lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for t in templates:
        tw = int(t.get("width") or 0)
        th = int(t.get("height") or 0)
        blob = _renderer_blob(t)
        name = (t.get("template") or "").lower()
        if require_renderer and not _renderer_matches(blob, renderer_f):
            continue
        score = 0
        if tw == width and th == height:
            score += 50
        elif tw and th and height and abs(tw / th - width / height) < 0.05:
            score += 15
        for token in prefers:
            if token in name:
                score += 8
            if token in blob:
                score += 3
        if "magix" in blob and "avc" in blob:
            score += 4
        if score > 0:
            scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def match_preset(preset: str, templates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the best VEGAS renderer/template for a named preset."""
    spec = RENDER_PRESETS.get(preset.lower())
    if spec is None:
        return {"ok": False, "error": f"unknown preset {preset!r}; use vertical, 1080p, or 4k"}
    width, height = spec["width"], spec["height"]

    scored = _score_templates(templates, spec, require_renderer=True)
    if not scored:
        # Renderer name didn't contain "MP4" (or the list was unfiltered).
        scored = _score_templates(templates, spec, require_renderer=False)
    if not scored:
        return {
            "ok": False,
            "error": (
                f"no VEGAS template matched preset {preset!r} "
                f"({width}x{height}). Run list_render_templates and pass "
                "renderer_name + template_name to render_start."
            ),
            "preset": spec,
            "listed_count": len(templates),
        }
    best = scored[0][1]
    return {
        "ok": True,
        "preset": spec,
        "renderer_name": best.get("renderer") or best.get("renderer_name"),
        "template_name": best.get("template"),
        "matched": best,
        "alternates": [t for _, t in scored[1:6]],
        "listed_count": len(templates),
    }
