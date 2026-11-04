"""Circular lens magnify + dark-ring tracker (frame-pipe, no full-clip buffer)."""
from __future__ import annotations

import json
import math
import os
import random
from typing import Any

from ..analysis.probe import probe_media
from ..output_policy import check_output_path
from .mediaio import encode_rgb_frames, iter_rgb_frames

MIN_INLIERS = 12


def _luma(rgb: bytes, width: int, x: int, y: int) -> int:
    i = (y * width + x) * 3
    return (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000


def _sample_ring_edges(
    rgb: bytes,
    width: int,
    height: int,
    cx: float,
    cy: float,
    r_min: float,
    r_max: float,
    rays: int = 48,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for i in range(rays):
        angle = (2.0 * math.pi * i) / rays
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        prev = None
        best = None
        best_score = 0.0
        steps = max(8, int(r_max - r_min))
        for step in range(steps):
            r = r_min + (r_max - r_min) * step / max(1, steps - 1)
            x = int(round(cx + cos_a * r))
            y = int(round(cy + sin_a * r))
            if x < 1 or y < 1 or x >= width - 1 or y >= height - 1:
                continue
            lum = _luma(rgb, width, x, y)
            if prev is not None:
                score = abs(lum - prev)
                if score > best_score:
                    best_score = score
                    best = (float(x), float(y))
            prev = lum
        if best is not None and best_score >= 8:
            points.append(best)
    return points


def _fit_circle(pts: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    if len(pts) < 3:
        return None
    (x1, y1), (x2, y2), (x3, y3) = pts[:3]
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-6:
        return None
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / d
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    if r < 2:
        return None
    return ux, uy, r


def _ransac_circle(
    points: list[tuple[float, float]],
    *,
    thresh: float = 2.5,
    iters: int = 80,
    rng: random.Random | None = None,
) -> tuple[float, float, float, int] | None:
    if len(points) < 3:
        return None
    rng = rng or random.Random(0)
    best = None
    best_n = 0
    for _ in range(iters):
        sample = rng.sample(points, 3)
        fit = _fit_circle(sample)
        if fit is None:
            continue
        cx, cy, r = fit
        inliers = [(x, y) for x, y in points if abs(math.hypot(x - cx, y - cy) - r) <= thresh]
        if len(inliers) > best_n:
            best_n = len(inliers)
            # Refine with all inliers via more RANSAC-ish average of 3-point fits.
            refined = fit
            if len(inliers) >= 6:
                acc = [0.0, 0.0, 0.0]
                count = 0
                for _j in range(12):
                    nxt = _fit_circle(rng.sample(inliers, 3))
                    if nxt is None:
                        continue
                    acc[0] += nxt[0]
                    acc[1] += nxt[1]
                    acc[2] += nxt[2]
                    count += 1
                if count:
                    refined = (acc[0] / count, acc[1] / count, acc[2] / count)
            best = (*refined, best_n)
    return best


def _moving_median(values: list[float | None], window: int = 5) -> list[float | None]:
    half = window // 2
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - half) : i + half + 1] if v is not None]
        if not chunk:
            out.append(None)
        else:
            chunk.sort()
            out.append(chunk[len(chunk) // 2])
    return out


def _savgol(values: list[float], window: int = 7, poly: int = 2) -> list[float]:
    try:
        from scipy.signal import savgol_filter  # type: ignore

        odd = window if window % 2 == 1 else window + 1
        odd = min(odd, len(values) if len(values) % 2 == 1 else max(3, len(values) - 1))
        if len(values) < 5:
            return list(values)
        return [float(v) for v in savgol_filter(values, odd, min(poly, odd - 1))]
    except Exception:
        # Fallback: 5-tap weighted moving average.
        if len(values) < 3:
            return list(values)
        out = []
        for i in range(len(values)):
            a = values[max(0, i - 2) : i + 3]
            out.append(sum(a) / len(a))
        return out


def _interpolate_nones(values: list[float | None]) -> list[float | None]:
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    if not known:
        return values
    out = list(values)
    for i, v in enumerate(out):
        if v is not None:
            continue
        prev = next(((j, pv) for j, pv in reversed(known) if j < i), None)
        nxt = next(((j, nv) for j, nv in known if j > i), None)
        if prev and nxt:
            j, pv = prev
            k, nv = nxt
            t = (i - j) / max(1, k - j)
            out[i] = pv + (nv - pv) * t
        elif prev:
            out[i] = prev[1]
        elif nxt:
            out[i] = nxt[1]
    return out


def track_circle(
    path: str,
    start: float = 0.0,
    duration: float | None = None,
    r_min: float = 20.0,
    r_max: float = 180.0,
    min_inliers: int = MIN_INLIERS,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Track a dark-ring circle. Returns per-frame [cx, cy, r, inliers] JSON."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 30.0) or 30.0
    guess_cx, guess_cy = width / 2.0, height / 2.0
    guess_r = (r_min + r_max) / 2.0
    frames: list[dict[str, Any]] = []
    raw_cx: list[float | None] = []
    raw_cy: list[float | None] = []
    raw_r: list[float | None] = []
    rng = random.Random(1)
    for index, rgb, w, h in iter_rgb_frames(path, start=start, duration=duration):
        points = _sample_ring_edges(rgb, w, h, guess_cx, guess_cy, r_min, r_max)
        if len(points) < 8:
            points = _sample_ring_edges(rgb, w, h, w / 2.0, h / 2.0, r_min, r_max, rays=64)
        fit = _ransac_circle(points, rng=rng)
        t = start + index / fps
        if fit is None or fit[3] < min_inliers:
            frames.append({"t": t, "cx": None, "cy": None, "r": None, "inliers": 0, "ok": False})
            raw_cx.append(None)
            raw_cy.append(None)
            raw_r.append(None)
            continue
        cx, cy, r, n = fit
        frames.append({"t": t, "cx": cx, "cy": cy, "r": r, "inliers": n, "ok": True})
        raw_cx.append(cx)
        raw_cy.append(cy)
        raw_r.append(r)
        guess_cx, guess_cy, guess_r = cx, cy, r
        r_min = max(8.0, guess_r * 0.6)
        r_max = guess_r * 1.5
        _ = height
    if not frames:
        return {"ok": False, "error": "no frames decoded", "field": "path"}
    hits = sum(1 for f in frames if f.get("ok"))
    if hits == 0:
        return {
            "ok": False,
            "error": f"track_circle failed: fewer than {min_inliers} inliers on every frame",
            "field": "inliers",
            "min_inliers": min_inliers,
            "frames": frames,
        }
    cx_i = _interpolate_nones(_moving_median(raw_cx))
    cy_i = _interpolate_nones(_moving_median(raw_cy))
    r_i = _interpolate_nones(_moving_median(raw_r))
    cx_s = _savgol([float(v) for v in cx_i if v is not None]) if any(v is not None for v in cx_i) else []
    cy_s = _savgol([float(v) for v in cy_i if v is not None]) if any(v is not None for v in cy_i) else []
    r_s = _savgol([float(v) for v in r_i if v is not None]) if any(v is not None for v in r_i) else []
    si = 0
    smoothed: list[list[float]] = []
    for i, frame in enumerate(frames):
        if cx_i[i] is None:
            continue
        frame["cx"] = cx_s[si] if si < len(cx_s) else cx_i[i]
        frame["cy"] = cy_s[si] if si < len(cy_s) else cy_i[i]
        frame["r"] = r_s[si] if si < len(r_s) else r_i[i]
        frame["ok"] = True
        smoothed.append([float(frame["cx"]), float(frame["cy"]), float(frame["r"]), float(frame["inliers"])])
        si += 1
        frames[i] = frame
    confidence = hits / max(1, len(frames))
    payload = {
        "ok": True,
        "path": os.path.abspath(path),
        "start": float(start),
        "duration": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "confidence": confidence,
        "min_inliers": min_inliers,
        "frames": frames,
        "circles": smoothed,
    }
    if output_path:
        refused = check_output_path(output_path)
        if refused:
            return refused
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        payload["output_path"] = os.path.abspath(output_path)
    return payload


def _load_track(track: str | dict[str, Any] | None) -> list[dict[str, Any]]:
    if track is None:
        return []
    if isinstance(track, dict):
        return list(track.get("frames") or [])
    if isinstance(track, str) and os.path.isfile(track):
        with open(track, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        return list((data or {}).get("frames") or [])
    return []


def _circle_at(frames: list[dict[str, Any]], index: int, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    if not frames:
        return fallback
    if 0 <= index < len(frames):
        item = frames[index]
        if item.get("cx") is not None:
            return float(item["cx"]), float(item["cy"]), float(item["r"])
    for item in frames:
        if item.get("cx") is not None:
            return float(item["cx"]), float(item["cy"]), float(item["r"])
    return fallback


def _sample_lanczos(rgb: bytes, width: int, height: int, x: float, y: float) -> tuple[int, int, int]:
    # 2-lobe Lanczos, 4x4 neighbourhood.
    ix, iy = int(math.floor(x)), int(math.floor(y))

    def lanczos(d: float) -> float:
        if d == 0:
            return 1.0
        if abs(d) >= 2:
            return 0.0
        pd = math.pi * d
        return (2 * math.sin(pd) * math.sin(pd / 2.0)) / (pd * pd)

    acc = [0.0, 0.0, 0.0]
    wsum = 0.0
    for ky in range(iy - 1, iy + 3):
        for kx in range(ix - 1, ix + 3):
            wx = lanczos(x - kx)
            wy = lanczos(y - ky)
            w = wx * wy
            if w == 0:
                continue
            sx = min(width - 1, max(0, kx))
            sy = min(height - 1, max(0, ky))
            i = (sy * width + sx) * 3
            acc[0] += rgb[i] * w
            acc[1] += rgb[i + 1] * w
            acc[2] += rgb[i + 2] * w
            wsum += w
    if wsum <= 1e-9:
        i = (min(height - 1, max(0, iy)) * width + min(width - 1, max(0, ix))) * 3
        return rgb[i], rgb[i + 1], rgb[i + 2]
    return (
        int(max(0, min(255, acc[0] / wsum))),
        int(max(0, min(255, acc[1] / wsum))),
        int(max(0, min(255, acc[2] / wsum))),
    )


def _unsharp(dst: bytearray, src: bytes, width: int, height: int, amount: float = 0.45) -> None:
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            i = (y * width + x) * 3
            for c in range(3):
                center = src[i + c]
                blur = (
                    src[i + c]
                    + src[i - 3 + c]
                    + src[i + 3 + c]
                    + src[i - width * 3 + c]
                    + src[i + width * 3 + c]
                ) / 5.0
                val = center + amount * (center - blur)
                dst[i + c] = int(max(0, min(255, val)))


def apply_lens_zoom(
    path: str,
    output_path: str,
    *,
    track: str | dict[str, Any] | None = None,
    scale: float = 1.6,
    inset_ratio: float = 0.08,
    feather_px: float = 4.0,
    ease: float = 0.25,
    focus_track: str | dict[str, Any] | None = None,
    start: float = 0.0,
    duration: float | None = None,
    unsharp: bool = True,
) -> dict[str, Any]:
    """Magnify only inside the tracked circle. Frame-pipe, no full-clip buffer."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    refused = check_output_path(output_path)
    if refused:
        return refused
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 30.0) or 30.0
    circles = _load_track(track)
    focus = _load_track(focus_track)
    if not circles:
        tracked = track_circle(path, start=start, duration=duration)
        if not tracked.get("ok"):
            return tracked
        circles = list(tracked.get("frames") or [])
    n_hint = int(round((duration if duration is not None else float(probe.get("duration_seconds") or 0)) * fps))
    n_hint = max(1, n_hint)

    def ease_scale(index: int) -> float:
        t = index / fps
        dur = (duration if duration is not None else n_hint / fps)
        edge = min(float(ease), dur / 2.0)
        if t < edge:
            u = t / max(edge, 1e-6)
            u = u * u * (3 - 2 * u)
            return 1.0 + (scale - 1.0) * u
        if t > dur - edge:
            u = (dur - t) / max(edge, 1e-6)
            u = u * u * (3 - 2 * u)
            return 1.0 + (scale - 1.0) * u
        return float(scale)

    def frames() -> Any:
        for index, rgb, w, h in iter_rgb_frames(path, start=start, duration=duration):
            cx, cy, r = _circle_at(circles, index, (w / 2.0, h / 2.0, min(w, h) / 4.0))
            if focus:
                fx, fy, _fr = _circle_at(focus, index, (cx, cy, r))
            else:
                fx, fy = cx, cy
            z = ease_scale(index)
            inner = max(2.0, r * (1.0 - float(inset_ratio)))
            feather = max(0.5, float(feather_px))
            out = bytearray(rgb)
            # Only walk the bounding box of the circle.
            x0 = max(0, int(cx - r - 2))
            x1 = min(w, int(cx + r + 3))
            y0 = max(0, int(cy - r - 2))
            y1 = min(h, int(cy + r + 3))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    dx = x - cx
                    dy = y - cy
                    dist = math.hypot(dx, dy)
                    if dist > inner + feather:
                        continue
                    src_x = fx + dx / z
                    src_y = fy + dy / z
                    rpx, g, b = _sample_lanczos(rgb, w, h, src_x, src_y)
                    if dist >= inner:
                        a = 1.0 - (dist - inner) / feather
                        a = max(0.0, min(1.0, a))
                        i = (y * w + x) * 3
                        rpx = int(rgb[i] * (1 - a) + rpx * a)
                        g = int(rgb[i + 1] * (1 - a) + g * a)
                        b = int(rgb[i + 2] * (1 - a) + b * a)
                    i = (y * w + x) * 3
                    out[i] = rpx
                    out[i + 1] = g
                    out[i + 2] = b
            if unsharp and z > 1.15:
                sharp = bytearray(out)
                _unsharp(sharp, bytes(out), w, h)
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        if math.hypot(x - cx, y - cy) <= inner:
                            i = (y * w + x) * 3
                            out[i : i + 3] = sharp[i : i + 3]
            yield bytes(out)

    encoded = encode_rgb_frames(
        frames(),
        output_path,
        width,
        height,
        fps,
        audio_path=path,
        audio_start=start,
        audio_duration=duration,
    )
    if not encoded.get("ok"):
        return encoded
    encoded["effect"] = "lens_zoom"
    encoded["scale"] = float(scale)
    return encoded
