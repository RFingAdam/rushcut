"""Rank candidate thumbnail frames from a video. CPU only, no NLE.

Each candidate frame is scored on signals that matter for a small phone
thumbnail: sharpness, contrast, subject size, a clear area for text, and a
face when OpenCV is installed. Blurred or motion-smeared frames, and frames
where a caller-supplied overlay region (notification, Steam overlay) is
showing, are rejected before ranking. Sampling and tie-breaks are fixed, so
the same file and params give the same picks every run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media
from .region import _parse_box

# Analysis frames are downscaled to this width (height keeps aspect).
_ANALYSIS_WIDTH = 320
# A frame is "blurred" when its sharpness is below this fraction of the
# clip's median sharpness, or below the absolute floor.
_BLUR_REL = 0.45
_BLUR_ABS = 8.0
# A frame is "motion-smeared" when its difference to the next sample is
# above this multiple of the clip's median difference and its sharpness
# is also below the median.
_MOTION_REL = 3.0
_WEIGHTS = {
    "sharpness": 0.35,
    "contrast": 0.2,
    "subject": 0.2,
    "text_area": 0.15,
    "face": 0.1,
}
_INSTALL_HINT = "pip install 'rushcut-mcp[analysis]' (numpy)"


def _try_numpy() -> Any:
    try:
        import numpy as np  # type: ignore

        return np
    except ImportError:
        return None


def _laplacian_var(np: Any, img: Any) -> float:
    lap = (
        -4.0 * img[1:-1, 1:-1]
        + img[:-2, 1:-1]
        + img[2:, 1:-1]
        + img[1:-1, :-2]
        + img[1:-1, 2:]
    )
    return float(lap.var())


def _gradient(np: Any, img: Any) -> Any:
    gx = np.abs(np.diff(img, axis=1))[:-1, :]
    gy = np.abs(np.diff(img, axis=0))[:, :-1]
    return gx + gy


def _subject_fraction(np: Any, grad: Any) -> float:
    """Area of the box holding the central 80% of edge energy, as a frame fraction."""
    total = float(grad.sum())
    if total <= 0:
        return 0.0
    h, w = grad.shape
    cols = np.cumsum(grad.sum(axis=0)) / total
    rows = np.cumsum(grad.sum(axis=1)) / total
    x0 = int(np.searchsorted(cols, 0.1))
    x1 = int(np.searchsorted(cols, 0.9))
    y0 = int(np.searchsorted(rows, 0.1))
    y1 = int(np.searchsorted(rows, 0.9))
    return max(0.0, (x1 - x0 + 1) * (y1 - y0 + 1) / float(w * h))


def _text_area(np: Any, grad: Any) -> tuple[float, str]:
    """How clear the calmest third of the frame is. 1.0 = flat area, 0 = busy."""
    h, w = grad.shape
    mean_all = float(grad.mean())
    if mean_all <= 0:
        return 1.0, "top"
    bands = {
        "top": grad[: h // 3, :],
        "bottom": grad[2 * h // 3 :, :],
        "left": grad[:, : w // 3],
        "right": grad[:, 2 * w // 3 :],
    }
    best_name = "top"
    best_val = float("inf")
    for name in ("top", "bottom", "left", "right"):
        val = float(bands[name].mean())
        if val < best_val:
            best_val, best_name = val, name
    return max(0.0, min(1.0, 1.0 - best_val / mean_all)), best_name


def _face_score(frame: Any, width: int, height: int) -> tuple[float, str]:
    try:
        import cv2  # type: ignore
    except ImportError:
        return 0.0, "unavailable"
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if not os.path.isfile(cascade_path):
            return 0.0, "unavailable"
        classifier = cv2.CascadeClassifier(cascade_path)
        faces = classifier.detectMultiScale(frame, scaleFactor=1.1, minNeighbors=4, minSize=(16, 16))
    except (OSError, ValueError, RuntimeError):
        return 0.0, "unavailable"
    if not len(faces):
        return 0.0, "haar"
    biggest = max(int(f[2]) * int(f[3]) for f in faces)
    # A face covering ~8% of the frame or more reads well on a phone.
    return min(1.0, biggest / (0.08 * width * height)), "haar"


def _read_frames(path: str, start: float, end: float, fps: float, width: int, height: int) -> tuple[bytes, int, int]:
    aw = min(_ANALYSIS_WIDTH, width) // 2 * 2
    ah = max(2, int(round(height * aw / float(width))) // 2 * 2)
    # round=up makes sample k the frame shown at exactly start + k/fps, so the
    # reported times match what the PNG export (-ss t) grabs.
    vf = f"fps={fps:.6f}:round=up,scale={aw}:{ah}:flags=area,format=gray"
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", path, "-t", f"{end - start:.6f}", "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    duration = max(1.0, end - start)
    proc = run(cmd, timeout=max(120.0, duration * 2.0), text=False)
    return proc.stdout or b"", aw, ah


def _overlay_flags(np: Any, frames: list[Any], boxes: list[dict[str, int]], threshold: float) -> list[list[int]]:
    """Per frame, the skip regions whose pixels differ from that region's usual look.

    The usual look is the per-pixel median over all sampled frames, so an
    overlay that shows for less than half the clip stands out against it.
    """
    flags: list[list[int]] = [[] for _ in frames]
    for idx, box in enumerate(boxes):
        y0, y1 = box["y"], box["y"] + box["h"]
        x0, x1 = box["x"], box["x"] + box["w"]
        stack = np.stack([f[y0:y1, x0:x1].astype(np.float32) for f in frames])
        baseline = np.median(stack, axis=0)
        diffs = np.abs(stack - baseline).mean(axis=(1, 2))
        for i, d in enumerate(diffs):
            if float(d) > threshold:
                flags[i].append(idx)
    return flags


def _extract_png(path: str, t: float, out_path: Path) -> None:
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{t:.6f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-an",
        "-map_metadata",
        "-1",
        "-flags",
        "+bitexact",
        str(out_path),
    ]
    run(cmd, timeout=120, text=False)


def pick_thumbnail_frames(
    path: str,
    n: int = 6,
    *,
    sample_fps: float = 2.0,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    min_gap_seconds: float = 2.0,
    skip_regions: list[Any] | None = None,
    output_dir: str | None = None,
    write_png: bool = True,
    overlay_threshold: float = 20.0,
) -> dict[str, Any]:
    """Return the ``n`` best thumbnail frames, ranked, with PNGs written to ``output_dir``.

    ``skip_regions`` is a list of boxes (``{x,y,w,h}`` or ``[x,y,w,h]``, pixels
    or 0-1 fractions) where an overlay or notification may appear. A frame
    where any of those regions looks different from its usual (median) look
    by more than ``overlay_threshold`` grey levels on average is skipped.
    """
    np = _try_numpy()
    if np is None:
        return {"ok": False, "error": "numpy is required for pick_thumbnail_frames", "install": _INSTALL_HINT}
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        n_i = int(n)
        fps = float(sample_fps)
        gap = float(min_gap_seconds)
        overlay_thr = float(overlay_threshold)
    except (TypeError, ValueError):
        return {"ok": False, "error": "n, sample_fps and min_gap_seconds must be numbers"}
    if n_i < 1 or n_i > 50:
        return {"ok": False, "error": "n must be in [1, 50]"}
    if fps <= 0 or fps > 10:
        return {"ok": False, "error": "sample_fps must be in (0, 10]"}
    if gap < 0:
        return {"ok": False, "error": "min_gap_seconds must be >= 0"}

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_video"):
        return {"ok": False, "error": "no video stream", "path": os.path.abspath(path)}
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration = float(probe.get("duration_seconds") or 0.0)
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "could not read the video size"}

    start = max(0.0, float(start_seconds or 0.0))
    end = duration if end_seconds is None else min(duration, float(end_seconds))
    if end <= start:
        return {"ok": False, "error": "end_seconds must be greater than start_seconds"}

    try:
        raw, aw, ah = _read_frames(path, start, end, fps, width, height)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    boxes: list[dict[str, int]] = []
    for box in skip_regions or []:
        parsed = _parse_box(box, width, height)
        if not parsed.get("ok"):
            return {"ok": False, "error": f"skip_regions: {parsed.get('error')}"}
        sx = aw / float(width)
        sy = ah / float(height)
        bx = max(0, min(aw - 2, int(round(int(parsed["x"]) * sx))))
        by = max(0, min(ah - 2, int(round(int(parsed["y"]) * sy))))
        boxes.append(
            {
                "x": bx,
                "y": by,
                "w": max(2, min(aw - bx, int(round(int(parsed["w"]) * sx)))),
                "h": max(2, min(ah - by, int(round(int(parsed["h"]) * sy)))),
            }
        )

    frame_size = aw * ah
    n_frames = len(raw) // frame_size if frame_size else 0
    if n_frames == 0:
        return {"ok": False, "error": "no frames decoded", "path": os.path.abspath(path)}

    frames = [
        np.frombuffer(raw[i * frame_size : (i + 1) * frame_size], dtype=np.uint8).reshape((ah, aw))
        for i in range(n_frames)
    ]
    overlay = _overlay_flags(np, frames, boxes, overlay_thr) if boxes else [[] for _ in frames]
    face_status = "unavailable"
    rows: list[dict[str, Any]] = []
    for i, frame in enumerate(frames):
        img = frame.astype(np.float32)
        grad = _gradient(np, img)
        sharp = _laplacian_var(np, img)
        if i + 1 < n_frames:
            motion = float(np.abs(img - frames[i + 1].astype(np.float32)).mean())
        elif i > 0:
            motion = float(np.abs(img - frames[i - 1].astype(np.float32)).mean())
        else:
            motion = 0.0
        text_score, text_side = _text_area(np, grad)
        face, face_status = _face_score(frame, aw, ah)
        rows.append(
            {
                "index": i,
                "t": round(start + i / fps, 3),
                "sharpness": sharp,
                "contrast": min(1.0, float(img.std()) / 64.0),
                "subject": min(1.0, _subject_fraction(np, grad) / 0.5),
                "text_area": text_score,
                "text_side": text_side,
                "face": face,
                "motion": motion,
                "overlay": overlay[i],
            }
        )

    sharp_values = sorted(r["sharpness"] for r in rows)
    median_sharp = sharp_values[len(sharp_values) // 2]
    motion_values = sorted(r["motion"] for r in rows)
    median_motion = motion_values[len(motion_values) // 2]
    max_sharp = sharp_values[-1] or 1.0

    rejected = {"blurred": 0, "motion_smeared": 0, "overlay": 0}
    candidates: list[dict[str, Any]] = []
    for r in rows:
        if r["sharpness"] < _BLUR_ABS or r["sharpness"] < _BLUR_REL * median_sharp:
            rejected["blurred"] += 1
            continue
        if (
            median_motion > 0
            and r["motion"] > _MOTION_REL * median_motion
            and r["sharpness"] < median_sharp
        ):
            rejected["motion_smeared"] += 1
            continue
        if r["overlay"]:
            rejected["overlay"] += 1
            continue
        signals = {
            "sharpness": min(1.0, r["sharpness"] / max_sharp),
            "contrast": r["contrast"],
            "subject": r["subject"],
            "text_area": r["text_area"],
            "face": r["face"],
        }
        score = sum(_WEIGHTS[k] * v for k, v in signals.items())
        candidates.append({**r, "signals": {k: round(v, 4) for k, v in signals.items()}, "score": round(score, 4)})

    # Rank by score, earliest frame breaks ties, then keep picks apart in time.
    candidates.sort(key=lambda c: (-c["score"], c["t"]))
    picks: list[dict[str, Any]] = []
    for c in candidates:
        if all(abs(c["t"] - p["t"]) >= gap for p in picks):
            picks.append(c)
        if len(picks) >= n_i:
            break

    out_dir: Path | None = None
    if write_png and picks:
        if output_dir:
            out_dir = Path(output_dir)
        else:
            src = Path(path)
            out_dir = src.parent / f"{src.stem}_thumbs"
        out_dir.mkdir(parents=True, exist_ok=True)

    frames_out: list[dict[str, Any]] = []
    for rank, p in enumerate(picks, 1):
        item: dict[str, Any] = {
            "rank": rank,
            "t": p["t"],
            "score": p["score"],
            "signals": p["signals"],
            "text_side": p["text_side"],
        }
        if out_dir is not None:
            png = out_dir / f"thumb_{rank:02d}_{p['t']:09.3f}s.png"
            try:
                _extract_png(path, float(p["t"]), png)
            except FfmpegError as exc:
                return {"ok": False, "error": f"PNG export failed at {p['t']}s: {exc}"}
            item["png"] = str(png.resolve())
        frames_out.append(item)

    return {
        "ok": True,
        "path": os.path.abspath(path),
        "n": n_i,
        "sample_fps": fps,
        "frames_scored": n_frames,
        "rejected": rejected,
        "face_detector": face_status,
        "weights": dict(_WEIGHTS),
        "output_dir": str(out_dir.resolve()) if out_dir is not None else None,
        "frames": frames_out,
    }
