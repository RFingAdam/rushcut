"""Layout-parity gate: compare a candidate re-render to an approved cut.

`rushcut check --parity APPROVED CANDIDATE` samples candidate frames, maps
each one onto the approved timeline, and scores named regions with luma
SSIM (7x7) and mean absolute difference. Pixels inside an edit-region are
masked only during that region's time span.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media
from ..output_policy import check_output_path

DEFAULT_SAMPLES = 24
MIN_SAMPLES = 20
DEFAULT_SSIM_MIN = 0.95
DEFAULT_MAD_MAX = 3.0
PRESENCE_STD_MIN = 10.0
SSIM_WINDOW = 7
_L = 255.0
_C1 = (0.01 * _L) ** 2
_C2 = (0.03 * _L) ** 2


def _require_numpy():
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "check --parity requires numpy (pip install 'rushcut-mcp[analysis]')"
        ) from exc
    return np


def parse_region(text: str) -> dict[str, Any]:
    raw = str(text).strip()
    if "=" in raw:
        name, rest = raw.split("=", 1)
    elif ":" in raw and not raw[0].isdigit():
        name, rest = raw.split(":", 1)
    else:
        raise ValueError(f"region must be name=x0,y0,x1,y1, got {text!r}")
    name = name.strip()
    parts = [p.strip() for p in rest.replace("x", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"region {name!r} must be name=x0,y0,x1,y1")
    x0, y0, x1, y1 = (float(p) for p in parts)
    if not name:
        raise ValueError("region name is required")
    return {"name": name, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def parse_edit_region(text: str) -> dict[str, Any]:
    parts = [p.strip() for p in str(text).replace("x", ",").split(",") if p.strip()]
    if len(parts) != 6:
        raise ValueError("edit-region must be x0,y0,x1,y1,t0,t1")
    x0, y0, x1, y1, t0, t1 = (float(p) for p in parts)
    if t1 < t0:
        raise ValueError("edit-region t1 must be >= t0")
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t0": t0, "t1": t1}


def parse_time_map_segment(text: str) -> dict[str, float]:
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError("time-map segment must be cand_t0,cand_t1,offset")
    t0, t1, offset = (float(p) for p in parts)
    if t1 <= t0:
        raise ValueError("time-map segment must have cand_t1 > cand_t0")
    return {"cand_t0": t0, "cand_t1": t1, "offset": offset}


def _as_regions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict) and "name" in value:
        return [dict(value)]
    if isinstance(value, dict):
        out = []
        for name, spec in value.items():
            if isinstance(spec, dict):
                item = {"name": name, **spec}
            elif isinstance(spec, (list, tuple)) and len(spec) == 4:
                item = {"name": name, "x0": spec[0], "y0": spec[1], "x1": spec[2], "y1": spec[3]}
            else:
                raise ValueError(f"region {name!r} must be x0,y0,x1,y1")
            out.append(item)
        return out
    if isinstance(value, str):
        return [parse_region(value)]
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(parse_region(item))
        elif isinstance(item, dict):
            out.append(dict(item))
        elif isinstance(item, (list, tuple)) and len(item) == 5:
            out.append(
                {"name": item[0], "x0": item[1], "y0": item[2], "x1": item[3], "y1": item[4]}
            )
        else:
            raise ValueError("region must be name=x0,y0,x1,y1 or a mapping")
    return out


def _as_edit_regions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [parse_edit_region(value)]
    if isinstance(value, dict):
        return [dict(value)]
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(parse_edit_region(item))
        elif isinstance(item, dict):
            out.append(dict(item))
        elif isinstance(item, (list, tuple)) and len(item) == 6:
            out.append(
                {
                    "x0": item[0],
                    "y0": item[1],
                    "x1": item[2],
                    "y1": item[3],
                    "t0": item[4],
                    "t1": item[5],
                }
            )
        else:
            raise ValueError("edit-region must be x0,y0,x1,y1,t0,t1")
    return out


def _parse_time_map_item(item: Any) -> list[dict[str, float]]:
    if isinstance(item, dict):
        return [
            {
                "cand_t0": float(item["cand_t0"]),
                "cand_t1": float(item["cand_t1"]),
                "offset": float(item.get("offset", 0.0)),
            }
        ]
    if isinstance(item, (list, tuple)):
        if len(item) == 3 and all(isinstance(x, (int, float)) for x in item):
            return [{"cand_t0": float(item[0]), "cand_t1": float(item[1]), "offset": float(item[2])}]
        out: list[dict[str, float]] = []
        for part in item:
            out.extend(_parse_time_map_item(part))
        return out
    return [parse_time_map_segment(str(item))]


def _as_time_map(value: Any) -> list[dict[str, float]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return _parse_time_map_item(value)
    out: list[dict[str, float]] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_parse_time_map_item(item))
        return out
    return _parse_time_map_item(value)


def default_regions(width: int, height: int) -> list[dict[str, Any]]:
    h1 = height // 3
    h2 = (2 * height) // 3
    return [
        {"name": "top", "x0": 0, "y0": 0, "x1": width, "y1": h1},
        {"name": "mid", "x0": 0, "y0": h1, "x1": width, "y1": h2},
        {"name": "bottom", "x0": 0, "y0": h2, "x1": width, "y1": height},
        {"name": "full", "x0": 0, "y0": 0, "x1": width, "y1": height},
    ]


def sample_times(duration: float, n: int, fps: float) -> list[float]:
    if n < 1:
        raise ValueError("samples must be >= 1")
    last = max(0.0, float(duration) - 0.5 / max(float(fps), 1.0))
    if n == 1:
        return [min(float(duration) / 2.0, last)]
    return [min(i * float(duration) / n, last) for i in range(n)]


def validate_time_map(
    segments: list[dict[str, float]],
    *,
    candidate_duration: float,
    approved_duration: float,
    fps: float = 30.0,
) -> None:
    if not segments:
        return
    frame = 1.0 / max(float(fps), 1.0)
    slop = max(0.05, 2.0 * frame)
    prev_end = -1.0
    for i, seg in enumerate(segments):
        t0 = float(seg["cand_t0"])
        t1 = float(seg["cand_t1"])
        offset = float(seg["offset"])
        if t1 <= t0:
            raise ValueError(f"time-map segment {i} is not increasing")
        if t0 + slop < prev_end:
            raise ValueError("time-map segments must be monotonic and non-overlapping")
        if t0 < -slop or t1 > candidate_duration + slop:
            raise ValueError("time-map segment is outside the candidate duration")
        a0 = t0 + offset
        a1 = t1 + offset
        if a0 < -slop or a1 > approved_duration + slop:
            raise ValueError("time-map segment is outside the approved duration")
        prev_end = t0 if prev_end < 0 else t1
        prev_end = t1


def map_candidate_time(
    t: float,
    segments: list[dict[str, float]],
    *,
    approved_duration: float,
) -> float | None:
    if not segments:
        if t < 0:
            return None
        return t
    last = None
    for seg in segments:
        t0 = float(seg["cand_t0"])
        t1 = float(seg["cand_t1"])
        if t0 <= t < t1 or (last is None and t < t0):
            last = seg
            if t0 <= t < t1:
                approved = t + float(seg["offset"])
                if approved < -1e-6 or approved > approved_duration + 1e-3:
                    return None
                return approved
        if t >= t0:
            last = seg
    if last is not None and t == float(last["cand_t1"]):
        approved = t + float(last["offset"])
        if 0 <= approved <= approved_duration + 1e-3:
            return approved
    return None


def _clip_rect(spec: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    x0 = round(float(spec["x0"]))
    y0 = round(float(spec["y0"]))
    x1 = round(float(spec["x1"]))
    y1 = round(float(spec["y1"]))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(0, min(x0, width))
    x1 = max(0, min(x1, width))
    y0 = max(0, min(y0, height))
    y1 = max(0, min(y1, height))
    return x0, y0, x1, y1


def _edit_active(edit: dict[str, Any], t: float) -> bool:
    t0 = float(edit["t0"])
    t1 = float(edit["t1"])
    return t0 <= t < t1


def _uniform_filter(arr: Any, size: int) -> Any:
    np = _require_numpy()
    try:
        from scipy.ndimage import uniform_filter  # type: ignore

        return uniform_filter(arr, size=size, mode="nearest")
    except ImportError:
        pass
    pad = size // 2
    padded = np.pad(arr, pad, mode="edge")
    integral = padded.cumsum(0).cumsum(1)
    integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant")
    h, w = arr.shape
    out = (
        integral[size : size + h, size : size + w]
        - integral[0:h, size : size + w]
        - integral[size : size + h, 0:w]
        + integral[0:h, 0:w]
    )
    return out / float(size * size)


def ssim_luma(a: Any, b: Any, mask: Any | None = None, window: int = SSIM_WINDOW) -> float | None:
    np = _require_numpy()
    if a.size == 0 or b.size == 0:
        return None
    aa = a.astype(np.float64, copy=False)
    bb = b.astype(np.float64, copy=False)
    if aa.shape != bb.shape:
        raise ValueError("SSIM inputs must have the same shape")
    if mask is not None and not mask.any():
        return None
    if min(aa.shape) < window:
        if mask is not None:
            aa = aa[mask]
            bb = bb[mask]
            if aa.size == 0:
                return None
        mu_a = float(aa.mean())
        mu_b = float(bb.mean())
        sig_a2 = float(aa.var())
        sig_b2 = float(bb.var())
        sig_ab = float(((aa - mu_a) * (bb - mu_b)).mean())
        num = (2.0 * mu_a * mu_b + _C1) * (2.0 * sig_ab + _C2)
        den = (mu_a * mu_a + mu_b * mu_b + _C1) * (sig_a2 + sig_b2 + _C2)
        return float(num / den) if den else 1.0
    mu_a = _uniform_filter(aa, window)
    mu_b = _uniform_filter(bb, window)
    mu_a2 = mu_a * mu_a
    mu_b2 = mu_b * mu_b
    mu_ab = mu_a * mu_b
    sig_a2 = np.maximum(_uniform_filter(aa * aa, window) - mu_a2, 0.0)
    sig_b2 = np.maximum(_uniform_filter(bb * bb, window) - mu_b2, 0.0)
    sig_ab = _uniform_filter(aa * bb, window) - mu_ab
    ssim_map = ((2.0 * mu_ab + _C1) * (2.0 * sig_ab + _C2)) / (
        (mu_a2 + mu_b2 + _C1) * (sig_a2 + sig_b2 + _C2)
    )
    pad = window // 2
    valid = ssim_map[pad:-pad, pad:-pad]
    if mask is None:
        return float(valid.mean())
    inner = mask[pad:-pad, pad:-pad]
    if not inner.any():
        return None
    return float(valid[inner].mean())


def mean_abs_diff(a: Any, b: Any, mask: Any | None = None) -> float | None:
    np = _require_numpy()
    if a.size == 0 or b.size == 0:
        return None
    diff = np.abs(a.astype(np.float64, copy=False) - b.astype(np.float64, copy=False))
    if mask is None:
        return float(diff.mean())
    if not mask.any():
        return None
    return float(diff[mask].mean())


def _region_mask(
    height: int,
    width: int,
    region: dict[str, Any],
    edit_regions: list[dict[str, Any]],
    t: float,
) -> tuple[slice, slice, Any]:
    np = _require_numpy()
    x0, y0, x1, y1 = _clip_rect(region, width, height)
    mask = np.ones((max(0, y1 - y0), max(0, x1 - x0)), dtype=bool)
    for edit in edit_regions:
        if not _edit_active(edit, t):
            continue
        ex0, ey0, ex1, ey1 = _clip_rect(edit, width, height)
        ix0 = max(0, ex0 - x0)
        iy0 = max(0, ey0 - y0)
        ix1 = min(x1 - x0, ex1 - x0)
        iy1 = min(y1 - y0, ey1 - y0)
        if ix1 > ix0 and iy1 > iy0:
            mask[iy0:iy1, ix0:ix1] = False
    return slice(y0, y1), slice(x0, x1), mask


def _score_region(
    approved: Any,
    candidate: Any,
    region: dict[str, Any],
    edit_regions: list[dict[str, Any]],
    t: float,
) -> dict[str, Any]:
    np = _require_numpy()
    height, width = candidate.shape
    ys, xs, mask = _region_mask(height, width, region, edit_regions, t)
    a = approved[ys, xs]
    b = candidate[ys, xs]
    ssim = ssim_luma(a, b, mask)
    mad = mean_abs_diff(a, b, mask)
    a_pix = a[mask] if mask is not None else a
    b_pix = b[mask] if mask is not None else b
    a_std = float(a_pix.std()) if a_pix.size else 0.0
    b_std = float(b_pix.std()) if b_pix.size else 0.0
    return {
        "ssim": None if ssim is None else round(float(ssim), 6),
        "mad": None if mad is None else round(float(mad), 4),
        "approved_std": round(a_std, 4),
        "candidate_std": round(b_std, 4),
        "pixels": int(np.count_nonzero(mask)) if mask is not None else int(a.size),
    }


def _nframes(duration: float, fps: float) -> int:
    return max(1, round(float(duration) * float(fps)))


def _time_to_frame(t: float, fps: float, nframes: int) -> int:
    return max(0, min(round(float(t) * float(fps)), nframes - 1))


def _decode_gray_frames(
    path: str, indices: list[int], width: int, height: int
) -> dict[int, Any]:
    np = _require_numpy()
    unique = sorted({int(i) for i in indices if i >= 0})
    if not unique:
        return {}
    frame_size = int(width) * int(height)
    if frame_size <= 0:
        raise ValueError("invalid frame size")

    def _reshape(raw: bytes, count: int) -> list[Any]:
        frames = []
        for i in range(count):
            chunk = raw[i * frame_size : (i + 1) * frame_size]
            if len(chunk) < frame_size:
                break
            frames.append(np.frombuffer(chunk, dtype=np.uint8).reshape((height, width)).copy())
        return frames

    expr = "+".join(f"eq(n\\,{i})" for i in unique)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-an",
        "-i",
        path,
        "-vf",
        f"select='{expr}'",
        "-vsync",
        "0",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        proc = run(cmd, timeout=90, text=False)
        raw = proc.stdout or b""
        frames = _reshape(raw, len(unique))
        if len(frames) == len(unique):
            return {idx: frame for idx, frame in zip(unique, frames)}
    except FfmpegError:
        frames = []
    out: dict[int, Any] = {}
    for idx in unique:
        one = [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-an",
            "-i",
            path,
            "-vf",
            f"select=eq(n\\,{idx})",
            "-vframes",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        proc = run(one, timeout=30, text=False)
        got = _reshape(proc.stdout or b"", 1)
        if got:
            out[idx] = got[0]
    missing = [i for i in unique if i not in out]
    if missing:
        raise FfmpegError(f"failed to decode frames {missing[:8]} from {path}")
    return out


def _presence_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _write_json(result: dict[str, Any], dest: str | os.PathLike[str]) -> str:
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return str(path.resolve())


def format_parity_report(result: dict[str, Any]) -> str:
    if result.get("error") and not result.get("regions"):
        return f"rushcut check --parity: error: {result.get('error')}\n"
    status = "PASS" if result.get("ok") else "FAIL"
    lines = [
        f"rushcut check --parity: {status}",
        (
            f"samples: {result.get('sampled') or result.get('samples')}  "
            f"ssim_min: {result.get('ssim_min')}  mad_max: {result.get('mad_max')}"
        ),
        "",
        f"{'region':<16} {'worst_ssim':>10} {'worst_mad':>10} {'pass':>6}",
    ]
    for name, info in (result.get("regions") or {}).items():
        ssim = info.get("worst_ssim")
        mad = info.get("worst_mad")
        mark = "ok" if info.get("pass") else "FAIL"
        ssim_s = "—" if ssim is None else f"{ssim:.4f}"
        mad_s = "—" if mad is None else f"{mad:.3f}"
        lines.append(f"{name:<16} {ssim_s:>10} {mad_s:>10} {mark:>6}")
    failures = list(result.get("failures") or [])
    if failures:
        lines.append("")
        lines.append("failures:")
        for item in failures:
            if item.get("reason") == "no_counterpart":
                lines.append(f"  t={item.get('t'):.3f}  no_counterpart")
                continue
            extra = item.get("reason") or ""
            ssim = item.get("ssim")
            mad = item.get("mad")
            metric = []
            if ssim is not None:
                metric.append(f"ssim={ssim:.4f}")
            if mad is not None:
                metric.append(f"mad={mad:.3f}")
            why = f"  {extra}" if extra and extra not in {"ssim", "mad"} else ""
            lines.append(
                f"  t={float(item.get('t') or 0):.3f}  region={item.get('region')}  "
                + " ".join(metric)
                + why
            )
    if result.get("loop") is not None:
        loop = result["loop"]
        mark = "ok" if loop.get("pass") or loop.get("ok") else "FAIL"
        lines.append(f"loop: {mark} {loop.get('detail') or ''}".rstrip())
    if result.get("loudness") is not None:
        loud = result["loudness"]
        mark = "ok" if loud.get("ok") else "FAIL"
        lines.append(
            f"loudness: {mark} LUFS {loud.get('integrated_lufs')} peak {loud.get('true_peak_dbtp')}"
        )
    lines.append(f"ok: {bool(result.get('ok'))}")
    return "\n".join(lines) + "\n"


def check_parity(
    approved: str,
    candidate: str,
    *,
    edit_regions: Any = None,
    regions: Any = None,
    time_map: Any = None,
    samples: int = DEFAULT_SAMPLES,
    ssim_min: float = DEFAULT_SSIM_MIN,
    mad_max: float = DEFAULT_MAD_MAX,
    presence_region: Any = None,
    allow_new: float = 0.0,
    seek_tolerance: int = 0,
    with_loop: bool = False,
    with_loudness: bool = False,
    json_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Compare a candidate re-render to an approved cut. JSON is written to json_path."""
    _require_numpy()
    try:
        n_samples = int(samples)
    except (TypeError, ValueError):
        return {"ok": False, "id": "parity", "error": "samples must be an integer"}
    if n_samples < MIN_SAMPLES:
        return {
            "ok": False,
            "id": "parity",
            "error": f"samples must be >= {MIN_SAMPLES} (default {DEFAULT_SAMPLES})",
        }
    if not approved:
        return {"ok": False, "id": "parity", "error": "approved source is required"}
    if not candidate:
        return {"ok": False, "id": "parity", "error": "candidate is required"}
    if not os.path.isfile(approved):
        return {"ok": False, "id": "parity", "error": f"file not found: {approved}"}
    if not os.path.isfile(candidate):
        return {"ok": False, "id": "parity", "error": f"file not found: {candidate}"}
    dest = str(Path(json_path).expanduser().resolve()) if json_path else None
    refused = check_output_path(dest)
    if refused:
        return {**refused, "id": "parity"}
    try:
        edits = _as_edit_regions(edit_regions)
        named = _as_regions(regions)
        segments = _as_time_map(time_map)
        presence = _presence_names(presence_region)
        tol = max(0, int(seek_tolerance or 0))
        allow = max(0.0, float(allow_new or 0.0))
        smin = float(ssim_min)
        mmax = float(mad_max)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "id": "parity", "error": str(exc)}

    appr = probe_media(approved)
    cand = probe_media(candidate)
    if not appr.get("ok"):
        return {"ok": False, "id": "parity", "error": appr.get("error") or "approved probe failed"}
    if not cand.get("ok"):
        return {"ok": False, "id": "parity", "error": cand.get("error") or "candidate probe failed"}
    if not appr.get("has_video") or not cand.get("has_video"):
        return {"ok": False, "id": "parity", "error": "both inputs must have a video stream"}
    width = int(cand.get("width") or 0)
    height = int(cand.get("height") or 0)
    if width <= 0 or height <= 0:
        return {"ok": False, "id": "parity", "error": "candidate has no video size"}
    if int(appr.get("width") or 0) != width or int(appr.get("height") or 0) != height:
        return {
            "ok": False,
            "id": "parity",
            "error": (
                f"resolution mismatch: approved {appr.get('width')}x{appr.get('height')} "
                f"vs candidate {width}x{height}"
            ),
        }
    cand_fps = float(cand.get("fps") or 30.0)
    appr_fps = float(appr.get("fps") or cand_fps)
    cand_dur = float(cand.get("duration_seconds") or 0.0)
    appr_dur = float(appr.get("duration_seconds") or 0.0)
    if cand_dur <= 0 or appr_dur <= 0:
        return {"ok": False, "id": "parity", "error": "both inputs must have a duration"}
    if not named:
        named = default_regions(width, height)
    names = [str(r.get("name") or "") for r in named]
    if any(not n for n in names):
        return {"ok": False, "id": "parity", "error": "every region needs a name"}
    if len(set(names)) != len(names):
        return {"ok": False, "id": "parity", "error": "region names must be unique"}
    missing_presence = [n for n in presence if n not in names]
    if missing_presence:
        return {
            "ok": False,
            "id": "parity",
            "error": f"presence-region not defined: {', '.join(missing_presence)}",
        }
    if not segments:
        segments = [{"cand_t0": 0.0, "cand_t1": cand_dur, "offset": 0.0}]
        identity_map = True
    else:
        identity_map = False
        try:
            validate_time_map(
                segments,
                candidate_duration=cand_dur,
                approved_duration=appr_dur,
                fps=cand_fps,
            )
        except ValueError as exc:
            return {"ok": False, "id": "parity", "error": str(exc)}

    times = sample_times(cand_dur, n_samples, cand_fps)
    cand_n = _nframes(cand_dur, cand_fps)
    appr_n = _nframes(appr_dur, appr_fps)
    last_covered = 0.0
    for seg in segments:
        last_covered = max(last_covered, float(seg["cand_t1"]))
    if identity_map:
        last_covered = min(cand_dur, appr_dur)

    planned: list[dict[str, Any]] = []
    cand_needed: list[int] = []
    appr_needed: list[int] = []
    for t in times:
        mapped = map_candidate_time(t, segments, approved_duration=appr_dur)
        cf = _time_to_frame(t, cand_fps, cand_n)
        cand_needed.append(cf)
        item: dict[str, Any] = {"t": t, "cand_frame": cf, "approved_t": mapped}
        if mapped is None:
            overhang = t - last_covered
            item["no_counterpart"] = True
            item["allowed"] = overhang <= allow + (0.5 / cand_fps)
            planned.append(item)
            continue
        item["no_counterpart"] = False
        base = _time_to_frame(mapped, appr_fps, appr_n)
        offsets = list(range(-tol, tol + 1)) if tol else [0]
        choices = []
        for k in offsets:
            af = base + k
            if 0 <= af < appr_n:
                choices.append(af)
                appr_needed.append(af)
        if not choices:
            item["no_counterpart"] = True
            item["allowed"] = (t - last_covered) <= allow + (0.5 / cand_fps)
            planned.append(item)
            continue
        item["approved_choices"] = choices
        item["approved_frame_nominal"] = base
        planned.append(item)

    try:
        cand_frames = _decode_gray_frames(candidate, cand_needed, width, height)
        appr_frames = _decode_gray_frames(approved, appr_needed, width, height)
    except (FfmpegError, ValueError) as exc:
        return {"ok": False, "id": "parity", "error": str(exc)}

    region_worst: dict[str, dict[str, Any]] = {
        r["name"]: {
            "x0": _clip_rect(r, width, height)[0],
            "y0": _clip_rect(r, width, height)[1],
            "x1": _clip_rect(r, width, height)[2],
            "y1": _clip_rect(r, width, height)[3],
            "worst_ssim": None,
            "worst_mad": None,
            "worst_ssim_t": None,
            "worst_mad_t": None,
            "pass": True,
        }
        for r in named
    }
    failures: list[dict[str, Any]] = []
    no_counterpart: list[dict[str, Any]] = []
    per_sample: list[dict[str, Any]] = []

    for item in planned:
        t = float(item["t"])
        row: dict[str, Any] = {
            "t": round(t, 6),
            "cand_frame": item["cand_frame"],
            "no_counterpart": bool(item.get("no_counterpart")),
        }
        if item.get("no_counterpart"):
            allowed = bool(item.get("allowed"))
            row["approved_t"] = None
            row["reason"] = "no_counterpart"
            row["allowed"] = allowed
            no_counterpart.append({"t": round(t, 6), "reason": "no_counterpart", "allowed": allowed})
            if not allowed:
                failures.append({"t": round(t, 6), "region": None, "reason": "no_counterpart"})
            per_sample.append(row)
            continue
        cand_frame = cand_frames[item["cand_frame"]]
        choices = list(item.get("approved_choices") or [])
        scored_choices: list[tuple[float, int, dict[str, dict[str, Any]]]] = []
        for af in choices:
            appr_frame = appr_frames[af]
            scores = {}
            ssim_vals = []
            for region in named:
                scores[region["name"]] = _score_region(appr_frame, cand_frame, region, edits, t)
                if scores[region["name"]]["ssim"] is not None:
                    ssim_vals.append(float(scores[region["name"]]["ssim"]))
            rank = sum(ssim_vals) / len(ssim_vals) if ssim_vals else -1.0
            scored_choices.append((rank, af, scores))
        scored_choices.sort(key=lambda x: x[0], reverse=True)
        _rank, chosen, region_scores = scored_choices[0]
        approved_t = chosen / appr_fps
        row["approved_t"] = round(approved_t, 6)
        row["approved_frame"] = chosen
        row["seek_offset_frames"] = int(chosen - item.get("approved_frame_nominal", chosen))
        row["regions"] = {}
        for region in named:
            name = region["name"]
            score = region_scores[name]
            ssim = score["ssim"]
            mad = score["mad"]
            presence_fail = False
            if (
                name in presence
                and score["approved_std"] > PRESENCE_STD_MIN
                and score["candidate_std"] <= PRESENCE_STD_MIN
            ):
                presence_fail = True
            masked_skip = ssim is None and mad is None
            ssim_ok = masked_skip or (ssim is not None and ssim >= smin)
            mad_ok = masked_skip or (mad is not None and mad <= mmax)
            passed = bool(ssim_ok and mad_ok and not presence_fail)
            row["regions"][name] = {
                **score,
                "pass": passed,
                "presence_fail": presence_fail,
            }
            worst = region_worst[name]
            if ssim is not None and (worst["worst_ssim"] is None or ssim < worst["worst_ssim"]):
                worst["worst_ssim"] = ssim
                worst["worst_ssim_t"] = round(t, 6)
            if mad is not None and (worst["worst_mad"] is None or mad > worst["worst_mad"]):
                worst["worst_mad"] = mad
                worst["worst_mad_t"] = round(t, 6)
            if not passed:
                worst["pass"] = False
                reason = "presence" if presence_fail else ("ssim" if not ssim_ok else "mad")
                failures.append(
                    {
                        "t": round(t, 6),
                        "approved_t": round(approved_t, 6),
                        "region": name,
                        "ssim": ssim,
                        "mad": mad,
                        "reason": reason,
                    }
                )
        per_sample.append(row)

    ok = not failures
    extras: dict[str, Any] = {}
    if with_loop:
        from .loop import check_loop

        extras["loop"] = check_loop(candidate)
        if not extras["loop"].get("ok") or not extras["loop"].get("pass"):
            ok = False
    if with_loudness:
        from .media import check_media

        extras["loudness"] = check_media(candidate, transcribe=False)
        if not extras["loudness"].get("ok"):
            ok = False

    result: dict[str, Any] = {
        "ok": ok,
        "pass": ok,
        "id": "parity",
        "approved": os.path.abspath(approved),
        "candidate": os.path.abspath(candidate),
        "width": width,
        "height": height,
        "fps": cand_fps,
        "candidate_duration": cand_dur,
        "approved_duration": appr_dur,
        "samples": n_samples,
        "sampled": len(per_sample),
        "ssim_min": smin,
        "mad_max": mmax,
        "seek_tolerance": tol,
        "allow_new": allow,
        "time_map": segments,
        "edit_regions": edits,
        "presence_region": presence,
        "regions": region_worst,
        "failures": failures,
        "no_counterpart": no_counterpart,
        "per_sample": per_sample,
        **extras,
    }
    if dest:
        result["json_path"] = dest
        _write_json(result, dest)
    return result
