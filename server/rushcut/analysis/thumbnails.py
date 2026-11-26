"""Rank candidate thumbnail frames from a video. CPU only, no NLE.

Each candidate frame is scored on signals that matter for a small phone
thumbnail: sharpness, contrast, subject size, a clear area for text, and a
face when OpenCV is installed. Blurred or motion-smeared frames, and frames
where a caller-supplied overlay region (notification, Steam overlay) is
showing, are rejected before ranking. Sampling and tie-breaks are fixed, so
the same file and params give the same picks every run.
"""
from __future__ import annotations

import functools
import os
import queue
import re
import subprocess
import threading
from collections import deque
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..output_policy import check_output_path
from .ffmpeg_util import FfmpegError, run, spawn, which_ffmpeg
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
# Sample k is the first frame at or after start + k/fps. Both the sampler and
# the PNG seek are shifted back by this much so a frame sitting exactly on a
# grid point lands in its own sample whatever the float rounding.
_GRID_EPS = 1e-4
# The PNG seek lands this far before the scored frame's exact time, so
# ffmpeg's "first frame at or after" picks that frame (any fps below 2000).
_SEEK_BACK = 5e-4
# Each skip region is kept per sample at no more than this many pixels
# (strided), so overlay checks stay bounded on long recordings.
_OVERLAY_MAX_PIXELS = 4096
# Give up when ffmpeg delivers no frame for this long.
_STALL_SECONDS = 120.0
_SHOWINFO_TB = re.compile(r"Parsed_showinfo.*config in time_base:\s*(\d+)/(\d+)")
_SHOWINFO_PTS = re.compile(r"Parsed_showinfo.*\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:(\S+)")


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


@functools.lru_cache(maxsize=1)
def _face_classifier() -> Any | None:
    """The Haar face cascade, loaded once per process. ``None`` when unavailable."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if not os.path.isfile(cascade_path):
            return None
        classifier = cv2.CascadeClassifier(cascade_path)
    except (OSError, ValueError, RuntimeError, AttributeError):
        return None
    if classifier.empty():
        return None
    return classifier


def _face_score(frame: Any, width: int, height: int) -> tuple[float, str]:
    classifier = _face_classifier()
    if classifier is None:
        return 0.0, "unavailable"
    try:
        faces = classifier.detectMultiScale(frame, scaleFactor=1.1, minNeighbors=4, minSize=(16, 16))
    except (OSError, ValueError, RuntimeError):
        return 0.0, "unavailable"
    if not len(faces):
        return 0.0, "haar"
    biggest = max(int(f[2]) * int(f[3]) for f in faces)
    # A face covering ~8% of the frame or more reads well on a phone.
    return min(1.0, biggest / (0.08 * width * height)), "haar"


def _analysis_size(width: int, height: int) -> tuple[int, int]:
    aw = min(_ANALYSIS_WIDTH, width) // 2 * 2
    ah = max(2, int(round(height * aw / float(width))) // 2 * 2)
    return aw, ah


def _iter_frames(
    np: Any, path: str, start: float, end: float, fps: float, aw: int, ah: int
) -> Iterator[tuple[float, Any]]:
    """Yield ``(t, gray_frame)`` one sample at a time, straight off the ffmpeg pipe.

    Sample k is the first decoded frame at or after ``start + k/fps``, and
    ``t`` is that frame's own timestamp (from ``showinfo``), so the PNG export
    can seek back to exactly the frame that was scored. Only one frame is held
    here at a time.
    """
    sel = (
        f"isnan(prev_selected_t)+gt(floor((t+{_GRID_EPS})*{fps:.6f}),"
        f"floor((prev_selected_t+{_GRID_EPS})*{fps:.6f}))"
    )
    vf = f"select='{sel}',showinfo,scale={aw}:{ah}:flags=area,format=gray"
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-nostats", "-loglevel", "info", "-y"]
    seek = f"{start:.6f}"
    if start:
        cmd += ["-ss", seek]
    # passthrough: one output frame per selected frame, no CFR duplicates.
    cmd += ["-i", path, "-t", f"{end - start:.6f}", "-an", "-vf", vf, "-vsync", "0"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    base = float(seek) if start else 0.0
    frame_size = aw * ah

    proc = spawn(cmd, text=False)
    times: queue.Queue[float] = queue.Queue()
    frames: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
    tail: deque[str] = deque(maxlen=20)

    def _read_stderr() -> None:
        tb: Fraction | None = None
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", "replace").rstrip()
            m = _SHOWINFO_PTS.search(line)
            if m:
                t_rel = float(int(m.group(1)) * tb) if tb is not None else float(m.group(2))
                times.put(t_rel)
                continue
            m = _SHOWINFO_TB.search(line)
            if m and int(m.group(2)):
                tb = Fraction(int(m.group(1)), int(m.group(2)))
                continue
            if "Parsed_showinfo" not in line:
                tail.append(line)

    stop = threading.Event()

    def _put(item: bytes | None) -> bool:
        while not stop.is_set():
            try:
                frames.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _read_stdout() -> None:
        assert proc.stdout is not None
        try:
            while True:
                buf = proc.stdout.read(frame_size)
                if not buf or len(buf) < frame_size or not _put(buf):
                    break
        finally:
            _put(None)

    readers = [threading.Thread(target=_read_stderr, daemon=True), threading.Thread(target=_read_stdout, daemon=True)]
    for th in readers:
        th.start()
    try:
        while True:
            try:
                buf = frames.get(timeout=_STALL_SECONDS)
            except queue.Empty:
                raise FfmpegError(f"ffmpeg made no progress for {_STALL_SECONDS:.0f}s while sampling frames") from None
            if buf is None:
                break
            try:
                t_rel = times.get(timeout=10.0)
            except queue.Empty:
                raise FfmpegError("ffmpeg did not report a frame timestamp") from None
            yield base + t_rel, np.frombuffer(buf, dtype=np.uint8).reshape((ah, aw))
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise FfmpegError("ffmpeg did not exit after its last frame") from None
        readers[0].join(timeout=5)
        if proc.returncode != 0:
            err = "\n".join(tail).strip()
            raise FfmpegError(f"ffmpeg exited {proc.returncode}: {err[-800:]}", stderr=err)
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _region_sample(frame: Any, box: dict[str, int]) -> Any:
    """The skip region's pixels, strided down to at most ``_OVERLAY_MAX_PIXELS``."""
    w, h = box["w"], box["h"]
    step = 1
    while (-(-w // step)) * (-(-h // step)) > _OVERLAY_MAX_PIXELS:
        step += 1
    crop = frame[box["y"] : box["y"] + h : step, box["x"] : box["x"] + w : step]
    return crop.copy()


def _overlay_flags(np: Any, samples: list[list[Any]], threshold: float) -> list[list[int]]:
    """Per frame, the skip regions whose pixels differ from that region's usual look.

    ``samples[r][i]`` is region ``r`` in frame ``i``. The usual look is the
    per-pixel median over all sampled frames, so an overlay that shows for
    less than half the clip stands out against it.
    """
    n_frames = len(samples[0]) if samples else 0
    flags: list[list[int]] = [[] for _ in range(n_frames)]
    for idx, crops in enumerate(samples):
        baseline = np.median(np.stack(crops), axis=0).astype(np.float32)
        for i, crop in enumerate(crops):
            if float(np.abs(crop.astype(np.float32) - baseline).mean()) > threshold:
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
        f"{max(0.0, t - _SEEK_BACK):.6f}",
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

    out_dir: Path | None = None
    if write_png:
        if output_dir:
            out_dir = Path(output_dir)
        else:
            src = Path(path)
            out_dir = src.parent / f"{src.stem}_thumbs"
        refused = check_output_path(str(out_dir / "thumb.png"))
        if refused:
            return refused

    aw, ah = _analysis_size(width, height)
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

    # Score each sample as it comes off the pipe; only the previous frame
    # (for the motion signal), per-frame numbers and small skip-region
    # crops are kept, never the decoded stream.
    face_status = "unavailable"
    rows: list[dict[str, Any]] = []
    region_samples: list[list[Any]] = [[] for _ in boxes]
    prev_img: Any = None
    try:
        for i, (t_exact, frame) in enumerate(_iter_frames(np, path, start, end, fps, aw, ah)):
            img = frame.astype(np.float32)
            if prev_img is not None:
                rows[-1]["motion"] = float(np.abs(prev_img - img).mean())
            grad = _gradient(np, img)
            text_score, text_side = _text_area(np, grad)
            face, face_status = _face_score(frame, aw, ah)
            for r_idx, box in enumerate(boxes):
                region_samples[r_idx].append(_region_sample(frame, box))
            rows.append(
                {
                    "index": i,
                    "t": round(t_exact, 3),
                    "t_exact": t_exact,
                    "sharpness": _laplacian_var(np, img),
                    "contrast": min(1.0, float(img.std()) / 64.0),
                    "subject": min(1.0, _subject_fraction(np, grad) / 0.5),
                    "text_area": text_score,
                    "text_side": text_side,
                    "face": face,
                    "motion": 0.0,
                    "overlay": [],
                }
            )
            prev_img = img
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    n_frames = len(rows)
    if n_frames == 0:
        return {"ok": False, "error": "no frames decoded", "path": os.path.abspath(path)}
    if n_frames > 1:
        # The last sample has no next frame; reuse its difference to the one before.
        rows[-1]["motion"] = rows[-2]["motion"]
    if boxes:
        for row, flagged in zip(rows, _overlay_flags(np, region_samples, overlay_thr)):
            row["overlay"] = flagged
        region_samples = []

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

    if not picks:
        out_dir = None
    if out_dir is not None:
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
                _extract_png(path, float(p["t_exact"]), png)
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
