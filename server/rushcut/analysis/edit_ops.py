"""File-level ffmpeg primitives. No NLE required.

These are the headless building blocks an editor-agnostic agent can chain:
cut/trim, concat, reframe, audio mix, and loudness normalize. Timeline
composition (tracks, fades, overlays) lives on the FFmpeg backend.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media


def _ok_path(path: str, label: str = "path") -> dict[str, Any] | None:
    if not path:
        return {"ok": False, "error": f"{label} is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    return None


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def cut_media(
    path: str,
    start_seconds: float,
    end_seconds: float | None = None,
    output_path: str | None = None,
    length_seconds: float | None = None,
) -> dict[str, Any]:
    """Trim a media file to [start, end) and write a new file."""
    err = _ok_path(path)
    if err:
        return err
    start = max(0.0, float(start_seconds))
    if length_seconds is not None:
        length = float(length_seconds)
        end = start + length
    elif end_seconds is not None:
        end = float(end_seconds)
        length = end - start
    else:
        return {"ok": False, "error": "end_seconds or length_seconds is required"}
    if length <= 0:
        return {"ok": False, "error": "cut length must be positive"}
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.cut{ext or '.mp4'}"
    _ensure_parent(output_path)
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-ss",
                f"{start:.6f}",
                "-i",
                path,
                "-t",
                f"{length:.6f}",
                "-avoid_negative_ts",
                "make_zero",
                output_path,
            ],
            timeout=180,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "start_seconds": start,
        "end_seconds": start + length,
        "length_seconds": length,
    }


def concat_media(paths: list[str], output_path: str) -> dict[str, Any]:
    """Concatenate media files in order (re-encodes for mixed codecs)."""
    if not paths:
        return {"ok": False, "error": "paths is required"}
    files: list[str] = []
    for p in paths:
        err = _ok_path(p)
        if err:
            return err
        files.append(os.path.abspath(p))
    if not output_path:
        return {"ok": False, "error": "output_path is required"}
    _ensure_parent(output_path)
    list_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for p in files:
            escaped = p.replace("\\", "/").replace("'", r"'\''")
            list_file.write(f"file '{escaped}'\n")
        list_file.close()
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file.name,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                output_path,
            ],
            timeout=300,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            os.remove(list_file.name)
        except OSError:
            pass
    return {"ok": True, "output_path": os.path.abspath(output_path), "count": len(files)}


def reframe_media(
    path: str,
    width: int,
    height: int,
    output_path: str | None = None,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
    scale: float = 1.0,
) -> dict[str, Any]:
    """Cover-crop (and optional punch-in) to a target frame size."""
    err = _ok_path(path)
    if err:
        return err
    width = int(width)
    height = int(height)
    if width < 2 or height < 2:
        return {"ok": False, "error": "width and height must be >= 2"}
    zoom = max(1.0, float(scale))
    px = max(-1.0, min(1.0, float(pan_x)))
    py = max(-1.0, min(1.0, float(pan_y)))
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.{width}x{height}{ext or '.mp4'}"
    _ensure_parent(output_path)
    # Cover the zoomed canvas, then crop WxH. pan -1..1 shifts the crop window.
    vf = (
        f"scale={width * zoom}:{height * zoom}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-{width})/2*(1+{px}):(ih-{height})/2*(1+{py})"
    )
    probe = probe_media(path)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        path,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
    ]
    if probe.get("has_audio"):
        cmd.extend(["-c:a", "aac"])
    else:
        cmd.extend(["-an"])
    cmd.append(output_path)
    try:
        run(cmd, timeout=180)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "width": width,
        "height": height,
        "scale": zoom,
        "pan_x": px,
        "pan_y": py,
    }


def mix_audio(paths: list[str], output_path: str, normalize_mix: bool = False) -> dict[str, Any]:
    """Mix audio streams from one or more files into a single audio (or A/V) file."""
    if not paths:
        return {"ok": False, "error": "paths is required"}
    files: list[str] = []
    for p in paths:
        err = _ok_path(p)
        if err:
            return err
        files.append(os.path.abspath(p))
    if not output_path:
        return {"ok": False, "error": "output_path is required"}
    _ensure_parent(output_path)
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    for p in files:
        cmd.extend(["-i", p])
    n = len(files)
    labels = "".join(f"[{i}:a]" for i in range(n))
    mix = f"{labels}amix=inputs={n}:duration=longest:normalize={'1' if normalize_mix else '0'}[a]"
    cmd.extend(["-filter_complex", mix, "-map", "[a]", "-c:a", "aac", output_path])
    try:
        run(cmd, timeout=180)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "output_path": os.path.abspath(output_path), "count": n}


def normalize_audio(
    path: str,
    output_path: str | None = None,
    integrated_lufs: float = -16.0,
) -> dict[str, Any]:
    """Loudness-normalize a file with ffmpeg loudnorm."""
    err = _ok_path(path)
    if err:
        return err
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.norm{ext or '.mp4'}"
    _ensure_parent(output_path)
    probe = probe_media(path)
    if not probe.get("has_audio"):
        return {"ok": False, "error": "no audio stream to normalize", "path": path}
    af = f"loudnorm=I={float(integrated_lufs)}:TP=-1.5:LRA=11"
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", path, "-af", af]
    if probe.get("has_video"):
        cmd.extend(["-c:v", "copy"])
    cmd.extend(["-c:a", "aac", output_path])
    try:
        run(cmd, timeout=180)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "integrated_lufs": float(integrated_lufs),
    }
