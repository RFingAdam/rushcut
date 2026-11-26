"""Named render presets.

Two families live here:

* ``vertical`` / ``1080p`` / ``4k`` -- size-only presets. Their names are
  matched against VEGAS's render template list (see :func:`match_preset`);
  the template decides the codec settings.
* ``vertical_30`` / ``hd_30`` / ``hd_60`` / ``uhd`` -- MP4 delivery presets
  with fixed encode settings. On VEGAS / Resolve they still pick the closest
  template by size; the FFmpeg backend and ``apply_timeline`` encode the final
  file with exactly the arguments from :func:`delivery_ffmpeg_args`, and
  ``verify_output(preset=...)`` checks the result against them.

Delivery presets, as ffmpeg output arguments (between the inputs and the
destination path)::

    vertical_30  1080x1920, 30 fps
      -vf scale=1080:1920:force_original_aspect_ratio=decrease,
          pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30
      -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p
      -c:a aac -b:a 192k -ar 48000 -ac 2
      -movflags +faststart

    hd_30        1920x1080, 30 fps  (same args, 1920:1080, fps=30)
    hd_60        1920x1080, 60 fps  (same args, 1920:1080, fps=60)
    uhd          3840x2160, 30 fps  (same args, 3840:2160, fps=30)

The ``-vf`` chain is a no-op when the program is already at the preset size
and rate (the FFmpeg backend composes it that way); it only letterboxes and
conforms when the args are applied to some other file. Frame rate is
conformed with the ``fps`` filter rather than ``-r`` (see docs/EDIT_JSON.md).
A program at the preset's NTSC rate (29.97 / 59.94) keeps it: pass
``source_fps`` and the filter becomes ``fps=30000/1001`` / ``fps=60000/1001``
instead of dropping or duplicating frames to reach 30 / 60.
``verify_output`` expects: h264 video, yuv420p, the preset's size and
frame rate, AAC audio at 48 kHz, and ``moov`` ahead of ``mdat`` (faststart).

``master=True`` on ``render_with_preset`` (or ``master: true`` on a preset)
runs two-pass ``master_audio`` on the delivered file. It keeps the 48 kHz
source rate and writes AAC 192k with the video stream-copied; the file is
then remuxed with ``+faststart`` (audio is re-encoded to the preset's
settings only if it no longer matches).
"""
from __future__ import annotations

from typing import Any

# Encode settings shared by every delivery preset.
DELIVERY_ENCODE: dict[str, Any] = {
    "encoder": "libx264",
    "x264_preset": "medium",
    "crf": 19,
    "pix_fmt": "yuv420p",
    "video_codec": "h264",
    "audio_encoder": "aac",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "sample_rate": 48000,
    "channels": 2,
    "faststart": True,
}

# name -> (width, height, fps, label, template substrings for VEGAS matching)
_DELIVERY_SIZES: dict[str, tuple[int, int, int, str, list[str]]] = {
    "vertical_30": (
        1080,
        1920,
        30,
        "Vertical 1080x1920 30p (x264 CRF 19)",
        ["1080x1920", "vertical", "9:16", "9x16", "portrait", "tiktok", "30p", "29.97", "30 fps"],
    ),
    "hd_30": (
        1920,
        1080,
        30,
        "HD 1920x1080 30p (x264 CRF 19)",
        ["1920x1080", "1080p", "30p", "29.97", "30 fps"],
    ),
    "hd_60": (
        1920,
        1080,
        60,
        "HD 1920x1080 60p (x264 CRF 19)",
        ["1920x1080", "1080p", "60p", "59.94", "60 fps"],
    ),
    "uhd": (
        3840,
        2160,
        30,
        "UHD 3840x2160 30p (x264 CRF 19)",
        ["3840x2160", "2160", "uhd", "4k", "30p", "29.97", "30 fps"],
    ),
}

DELIVERY_PRESETS: dict[str, dict[str, Any]] = {
    name: {
        "name": name,
        "width": w,
        "height": h,
        "fps": fps,
        "label": label,
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": prefers,
        "master": False,
        "delivery": True,
        **DELIVERY_ENCODE,
    }
    for name, (w, h, fps, label, prefers) in _DELIVERY_SIZES.items()
}

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
        "master": False,
    },
    "1080p": {
        "name": "1080p",
        "width": 1920,
        "height": 1080,
        "label": "1080p landscape",
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": ["1920x1080", "1080", "1920"],
        "master": False,
    },
    "4k": {
        "name": "4k",
        "width": 3840,
        "height": 2160,
        "label": "4K landscape",
        "renderer_contains": "MP4",
        "template_contains": "",
        "prefer_template_substrings": ["2160", "3840", "4k", "4K", "UHD"],
        "master": False,
    },
}

RENDER_PRESETS.update(DELIVERY_PRESETS)


def delivery_preset(name: str | None) -> dict[str, Any] | None:
    """The delivery preset spec for *name*, or ``None`` (size-only / unknown)."""
    if not name:
        return None
    return DELIVERY_PRESETS.get(str(name).lower())


def is_ntsc_of(fps: float | None, nominal: float, tolerance: float = 0.01) -> bool:
    """True when *fps* is the NTSC rate (x 1000/1001) of *nominal*, e.g. 29.97 for 30."""
    if not fps:
        return False
    return abs(float(fps) - nominal * 1000.0 / 1001.0) < tolerance


def delivery_rate(name: str, source_fps: float | None = None) -> tuple[float, str]:
    """``(fps, fps-filter rate)`` for a delivery preset.

    The preset's integer rate, unless *source_fps* is its NTSC rate: then
    ``30000/1001`` / ``60000/1001`` so a 29.97 / 59.94 program is left alone.
    """
    nominal = int(DELIVERY_PRESETS[str(name).lower()]["fps"])
    if is_ntsc_of(source_fps, nominal):
        return nominal * 1000.0 / 1001.0, f"{nominal * 1000}/1001"
    return float(nominal), str(nominal)


def delivery_video_filter(name: str, source_fps: float | None = None) -> str:
    """The ``-vf`` chain for a delivery preset (size + letterbox + fps conform)."""
    spec = DELIVERY_PRESETS[str(name).lower()]
    w, h = spec["width"], spec["height"]
    rate = delivery_rate(name, source_fps)[1]
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={rate}"
    )


def delivery_ffmpeg_args(
    name: str, *, video_filter: bool = True, source_fps: float | None = None
) -> list[str]:
    """Exact ffmpeg output args for a delivery preset.

    Goes between the inputs and the destination path. ``video_filter=False``
    drops the ``-vf`` pair for callers that build their own filter graph
    (they must still end it at the preset size and ``fps``). ``source_fps``
    is the program's rate; an NTSC program keeps its NTSC rate.
    """
    spec = DELIVERY_PRESETS.get(str(name).lower())
    if spec is None:
        raise KeyError(f"unknown delivery preset {name!r}; use {', '.join(DELIVERY_PRESETS)}")
    args: list[str] = []
    if video_filter:
        args.extend(["-vf", delivery_video_filter(name, source_fps)])
    args.extend(
        [
            "-c:v",
            spec["encoder"],
            "-preset",
            spec["x264_preset"],
            "-crf",
            str(spec["crf"]),
            "-pix_fmt",
            spec["pix_fmt"],
            "-c:a",
            spec["audio_encoder"],
            "-b:a",
            spec["audio_bitrate"],
            "-ar",
            str(spec["sample_rate"]),
            "-ac",
            str(spec["channels"]),
        ]
    )
    if spec.get("faststart"):
        args.extend(["-movflags", "+faststart"])
    return args


def preset_expectations(name: str | None) -> dict[str, Any] | None:
    """What ``verify_output(preset=name)`` checks, or ``None`` for unknown names.

    Size-only presets check width/height; delivery presets add fps, codecs,
    pix_fmt, sample rate and faststart.
    """
    if not name:
        return None
    spec = RENDER_PRESETS.get(str(name).lower())
    if spec is None:
        return None
    expect: dict[str, Any] = {"width": spec["width"], "height": spec["height"]}
    if spec.get("delivery"):
        expect.update(
            {
                "fps": float(spec["fps"]),
                "video_codec": spec["video_codec"],
                "pix_fmt": spec["pix_fmt"],
                "audio_codec": spec["audio_codec"],
                "sample_rate": int(spec["sample_rate"]),
                "faststart": bool(spec.get("faststart")),
            }
        )
    return expect


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
        return {
            "ok": False,
            "error": f"unknown preset {preset!r}; use {', '.join(RENDER_PRESETS)}",
        }
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
