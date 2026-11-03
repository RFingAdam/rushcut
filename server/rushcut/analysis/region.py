"""Per-second overlay or face presence inside a region box. CPU-only fallback."""
from __future__ import annotations

import math
import os
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media
from .session import lookup_session_time

_MEAN_PRESENT = 18.0
_VAR_PRESENT = 60.0
_EDGE_PRESENT = 6.0


def _parse_box(box: Any, width: int, height: int) -> dict[str, Any] | dict[str, str]:
    if box is None:
        return {"ok": False, "error": "box is required"}
    if isinstance(box, dict):
        x = box.get("x", box.get("left", 0))
        y = box.get("y", box.get("top", 0))
        w = box.get("w", box.get("width", box.get("width_px")))
        h = box.get("h", box.get("height", box.get("height_px")))
    elif isinstance(box, (list, tuple)) and len(box) == 4:
        x, y, w, h = box
    else:
        return {"ok": False, "error": "box must be {x,y,w,h} or [x,y,w,h]"}
    try:
        x_f, y_f, w_f, h_f = float(x), float(y), float(w), float(h)
    except (TypeError, ValueError):
        return {"ok": False, "error": "box values must be numbers"}
    if 0 < w_f <= 1 and 0 < h_f <= 1 and 0 <= x_f <= 1 and 0 <= y_f <= 1:
        x_px = int(round(x_f * width))
        y_px = int(round(y_f * height))
        w_px = int(round(w_f * width))
        h_px = int(round(h_f * height))
        normalized = True
    else:
        x_px, y_px, w_px, h_px = int(round(x_f)), int(round(y_f)), int(round(w_f)), int(round(h_f))
        normalized = False
    w_px = max(2, w_px)
    h_px = max(2, h_px)
    if x_px < 0 or y_px < 0 or x_px + w_px > width or y_px + h_px > height:
        x_px = max(0, min(x_px, width - 2))
        y_px = max(0, min(y_px, height - 2))
        w_px = max(2, min(w_px, width - x_px))
        h_px = max(2, min(h_px, height - y_px))
    return {
        "ok": True,
        "x": x_px,
        "y": y_px,
        "w": w_px,
        "h": h_px,
        "normalized": normalized,
    }


def _stats(frame: bytes) -> tuple[float, float, float]:
    n = len(frame)
    if n <= 0:
        return 0.0, 0.0, 0.0
    total = 0
    for b in frame:
        total += b
    mean = total / n
    var_acc = 0.0
    for b in frame:
        d = b - mean
        var_acc += d * d
    variance = var_acc / n
    edge_acc = 0
    for i in range(n - 1):
        edge_acc += abs(frame[i] - frame[i + 1])
    edge = edge_acc / max(1, n - 1)
    return mean, variance, edge


def _face_present(frame: bytes, width: int, height: int) -> tuple[bool, str]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return False, "unavailable"
    try:
        arr = np.frombuffer(frame, dtype=np.uint8).reshape((height, width))
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if not os.path.isfile(cascade_path):
            return False, "unavailable"
        classifier = cv2.CascadeClassifier(cascade_path)
        faces = classifier.detectMultiScale(arr, scaleFactor=1.1, minNeighbors=3, minSize=(16, 16))
        return bool(len(faces)), "haar"
    except (OSError, ValueError, RuntimeError):
        return False, "unavailable"


def _present(mean: float, variance: float, edge: float, face: bool, mode: str) -> tuple[bool, float, str]:
    activity_score = max(mean / 255.0, math.sqrt(variance) / 128.0, edge / 64.0)
    if mode == "face":
        return bool(face), 1.0 if face else activity_score, "face" if face else "activity"
    if mode == "activity":
        present = mean >= _MEAN_PRESENT or variance >= _VAR_PRESENT or edge >= _EDGE_PRESENT
        return present, round(activity_score, 4), "activity"
    # auto: face if found, else activity
    if face:
        return True, 1.0, "face"
    present = mean >= _MEAN_PRESENT or variance >= _VAR_PRESENT or edge >= _EDGE_PRESENT
    return present, round(activity_score, 4), "activity"


def detect_region_presence(
    path: str,
    box: Any,
    *,
    mode: str = "auto",
    sample_fps: float = 1.0,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    offset_map: list[dict[str, Any]] | None = None,
    session_seconds: float | None = None,
    session_end: float | None = None,
) -> dict[str, Any]:
    """Return per-second presence for a region: face detector or variance/edge activity."""
    if offset_map is not None and session_seconds is not None:
        hit = lookup_session_time(offset_map, session_seconds)
        if not hit.get("ok"):
            return hit
        path = str(hit["path"])
        start_seconds = float(hit["file_seconds"])
        if session_end is not None:
            end_hit = lookup_session_time(offset_map, session_end)
            if end_hit.get("ok") and end_hit.get("path") == path:
                end_seconds = float(end_hit["file_seconds"])
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    mode_l = str(mode or "auto").lower()
    if mode_l not in {"auto", "face", "activity"}:
        return {"ok": False, "error": "mode must be auto, face, or activity"}
    try:
        fps = float(sample_fps)
    except (TypeError, ValueError):
        return {"ok": False, "error": "sample_fps must be a number"}
    if fps <= 0 or fps > 30:
        return {"ok": False, "error": "sample_fps must be in (0, 30]"}

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_video"):
        return {"ok": False, "error": "no video stream", "path": os.path.abspath(path)}
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    parsed = _parse_box(box, width, height)
    if not parsed.get("ok"):
        return parsed
    bx, by, bw, bh = int(parsed["x"]), int(parsed["y"]), int(parsed["w"]), int(parsed["h"])

    start = max(0.0, float(start_seconds or 0.0))
    duration = float(probe.get("duration_seconds") or 0)
    if end_seconds is None:
        end = duration
    else:
        end = min(duration, float(end_seconds))
    if end <= start:
        return {"ok": False, "error": "end_seconds must be greater than start_seconds"}

    vf = f"fps={fps:.6f},crop={bw}:{bh}:{bx}:{by},format=gray"
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", path, "-t", f"{end - start:.6f}", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    try:
        proc = run(cmd, timeout=180, text=False)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    raw = proc.stdout or b""
    frame_size = bw * bh
    if frame_size <= 0:
        return {"ok": False, "error": "invalid crop size"}
    n_frames = len(raw) // frame_size
    face_status = "skipped"
    samples: list[dict[str, Any]] = []
    for i in range(n_frames):
        frame = raw[i * frame_size : (i + 1) * frame_size]
        mean, variance, edge = _stats(frame)
        t = start + i / fps
        face = False
        if mode_l in {"auto", "face"}:
            face, face_status = _face_present(frame, bw, bh)
            if face_status == "unavailable" and mode_l == "face":
                mode_l = "activity"
        present, score, method = _present(mean, variance, edge, face, mode_l)
        samples.append(
            {
                "t": round(t, 3),
                "present": present,
                "score": round(float(score), 4),
                "method": method,
                "mean": round(mean, 2),
                "variance": round(variance, 2),
                "edge": round(edge, 2),
            }
        )

    return {
        "ok": True,
        "path": os.path.abspath(path),
        "box": {"x": bx, "y": by, "w": bw, "h": bh},
        "mode": mode_l,
        "sample_fps": fps,
        "face_detector": face_status,
        "cpu_fallback": face_status != "haar",
        "samples": samples,
        "present_seconds": [s["t"] for s in samples if s["present"]],
        "absent_seconds": [s["t"] for s in samples if not s["present"]],
        "duration_seconds": duration,
    }
