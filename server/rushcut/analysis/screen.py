"""On-screen text and fixed graphics over time. Local, CPU-only, no NLE.

``read_screen_regions`` OCRs named regions (tesseract via pytesseract, the
optional ``rushcut-mcp[ocr]`` extra) and emits only changes.
``match_template`` finds fixed graphics (banners, icons, pop-ups) with
zero-mean normalized cross-correlation: OpenCV when it is importable, numpy
otherwise.

Both read grayscale frames from ffmpeg at ``fps`` (sample k is the frame at
``start + k / fps``), skip work on regions whose pixels did not change since
the last evaluation, and cache results keyed by a file fingerprint plus the
call's parameters.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .ffmpeg_util import FfmpegError, decode_timeout_seconds, which_ffmpeg
from .probe import probe_media
from .region import _parse_box

OCR_INSTALL = "pip install 'rushcut-mcp[ocr]'"
ANALYSIS_INSTALL = "pip install 'rushcut-mcp[analysis]'"
NUMERIC_WHITELIST = "0123456789,.$"
DEFAULT_MIN_CONFIDENCE = 0.95
DEFAULT_CHANGE_THRESHOLD = 0.002
DEFAULT_TEMPLATE_THRESHOLD = 0.8
CACHE_VERSION = 1

# A pixel counts as changed when its luma moved by more than this.
_PIXEL_DELTA = 24
# Below this luma standard deviation a region is blank: no OCR needed.
_BLANK_STD = 4.0
# Windows flatter than this cannot hold a template match.
_FLAT_WINDOW_STD = 2.0
_OCR_MIN_HEIGHT = 96
_OCR_PAD = 10
_MAX_REJECTED = 200
_AUTO_SCALE_WIDTH = 960


# ---------------------------------------------------------------- helpers


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError:
        return None
    return np


def _load_ocr() -> tuple[Any, str | None, dict[str, Any] | None]:
    """Return (pytesseract module, tesseract version, error dict)."""
    try:
        import pytesseract
    except ImportError:
        return None, None, {
            "ok": False,
            "error": "OCR is not installed: pytesseract is missing",
            "install": OCR_INSTALL,
        }
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # TesseractNotFoundError and OS errors
        return None, None, {
            "ok": False,
            "error": f"tesseract binary not found or not runnable: {exc}",
            "install": OCR_INSTALL,
            "hint": "Install the tesseract-ocr package for your OS and put tesseract on PATH.",
        }
    return pytesseract, version, None


def _round_t(t: float) -> float:
    return round(float(t), 3)


def _rect_dict(parsed: dict[str, Any]) -> dict[str, int]:
    return {"x": int(parsed["x"]), "y": int(parsed["y"]), "w": int(parsed["w"]), "h": int(parsed["h"])}


def _changed_fraction(np: Any, a: Any, b: Any) -> float:
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return float((diff > _PIXEL_DELTA).mean())


# ------------------------------------------------------------------ cache


def file_fingerprint(path: str) -> str:
    """sha256 over size, mtime and the first/last MiB (whole file when small)."""
    st = os.stat(path)
    h = hashlib.sha256()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    chunk = 1024 * 1024
    with open(path, "rb") as fh:
        if st.st_size <= 2 * chunk:
            h.update(fh.read())
        else:
            h.update(fh.read(chunk))
            fh.seek(-chunk, os.SEEK_END)
            h.update(fh.read(chunk))
    return h.hexdigest()


def cache_dir() -> Path:
    root = os.environ.get("RUSHCUT_CACHE_DIR")
    base = Path(root) if root else Path(tempfile.gettempdir()) / "rushcut-cache"
    return base / "screen"


def _cache_key(path: str, kind: str, params: dict[str, Any]) -> str:
    blob = json.dumps(
        {"v": CACHE_VERSION, "kind": kind, "file": file_fingerprint(path), "params": params},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    target = cache_dir() / f"{key}.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _cache_put(key: str, result: dict[str, Any]) -> str | None:
    folder = cache_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(folder), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        target = folder / f"{key}.json"
        os.replace(tmp, target)
        return str(target)
    except OSError:
        return None


def _from_cache(key: str, started: float) -> dict[str, Any] | None:
    hit = _cache_get(key)
    if hit is None or not hit.get("ok"):
        return None
    hit["cache"] = {
        "hit": True,
        "key": key,
        "path": str(cache_dir() / f"{key}.json"),
        "original_runtime_seconds": hit.get("runtime_seconds"),
        "original_frames_read": hit.get("frames_read"),
    }
    hit["runtime_seconds"] = round(time.monotonic() - started, 4)
    hit["frames_read"] = 0
    return hit


def _store(key: str | None, result: dict[str, Any]) -> dict[str, Any]:
    if key is None:
        result["cache"] = {"hit": False, "key": None, "path": None}
        return result
    stored = _cache_put(key, result)
    result["cache"] = {"hit": False, "key": key, "path": stored}
    return result


# ----------------------------------------------------------------- frames


def _iter_frames(
    path: str,
    *,
    start: float,
    end: float,
    fps: float,
    crop: tuple[int, int, int, int],
    out_size: tuple[int, int] | None = None,
    timeout: float | None = None,
) -> Iterator[tuple[float, bytes]]:
    """Yield (t, gray bytes) for sample k at exactly start + k / fps."""
    cx, cy, cw, ch = crop
    filters = [f"fps={fps:.6f}:round=up", f"crop={cw}:{ch}:{cx}:{cy}"]
    ow, oh = cw, ch
    if out_size is not None and out_size != (cw, ch):
        ow, oh = out_size
        filters.append(f"scale={ow}:{oh}:flags=area")
    filters.append("format=gray")
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += [
        "-i", path,
        "-t", f"{end - start:.6f}",
        "-an", "-sn",
        "-vf", ",".join(filters),
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    frame_size = ow * oh
    deadline = time.monotonic() + float(timeout or decode_timeout_seconds(end - start))
    with tempfile.TemporaryFile() as errfile:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
        except FileNotFoundError as exc:
            raise FfmpegError(f"Executable not found: {cmd[0]}") from exc
        assert proc.stdout is not None
        k = 0
        try:
            while True:
                if time.monotonic() > deadline:
                    raise FfmpegError(f"Timed out reading frames from {path}")
                buf = proc.stdout.read(frame_size)
                if not buf or len(buf) < frame_size:
                    break
                yield start + k / fps, buf
                k += 1
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if proc.returncode not in (0, None) and k == 0:
            errfile.seek(0)
            err = errfile.read().decode("utf-8", "replace").strip()
            raise FfmpegError(f"ffmpeg exited {proc.returncode}: {err[:800]}", stderr=err)


def _open_media(
    path: str, start_seconds: float | None, end_seconds: float | None, fps: Any
) -> dict[str, Any]:
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        fps_f = float(fps)
    except (TypeError, ValueError):
        return {"ok": False, "error": "fps must be a number"}
    if not (0 < fps_f <= 30):
        return {"ok": False, "error": "fps must be in (0, 30]"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_video"):
        return {"ok": False, "error": "no video stream", "path": os.path.abspath(path)}
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "could not read video dimensions"}
    duration = float(probe.get("duration_seconds") or 0.0)
    start = max(0.0, float(start_seconds or 0.0))
    end = duration if end_seconds is None else min(duration, float(end_seconds))
    if end <= start:
        return {"ok": False, "error": "end_seconds must be greater than start_seconds"}
    return {
        "ok": True,
        "width": width,
        "height": height,
        "duration": duration,
        "start": start,
        "end": end,
        "fps": fps_f,
    }


def _union(rects: list[dict[str, int]]) -> tuple[int, int, int, int]:
    x0 = min(r["x"] for r in rects)
    y0 = min(r["y"] for r in rects)
    x1 = max(r["x"] + r["w"] for r in rects)
    y1 = max(r["y"] + r["h"] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


# ---------------------------------------------------------------- profile


def _profile_screen(profile: Any) -> tuple[dict[str, Any], Path | None]:
    """Return (screen block as a dict, base dir for relative template paths)."""
    if profile is None:
        return {}, None
    base: Path | None = None
    if isinstance(profile, (str, os.PathLike)):
        from ..profile import load_profile

        base = Path(profile).resolve().parent
        profile = load_profile(profile)
    if isinstance(profile, dict):
        screen = profile.get("screen")
    else:
        screen = getattr(profile, "screen", None)
        if screen is not None and hasattr(screen, "model_dump"):
            screen = screen.model_dump()
    return (dict(screen) if isinstance(screen, dict) else {}), base


# -------------------------------------------------------------------- OCR


def _region_specs(regions: Any, width: int, height: int) -> list[dict[str, Any]] | dict[str, Any]:
    if not isinstance(regions, list) or not regions:
        return {"ok": False, "error": "regions must be a non-empty list of {name, rect}"}
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(regions):
        if not isinstance(item, dict):
            return {"ok": False, "error": f"regions[{i}] must be an object with name and rect"}
        name = str(item.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": f"regions[{i}].name is required"}
        if name in seen:
            return {"ok": False, "error": f"duplicate region name: {name}"}
        seen.add(name)
        parsed = _parse_box(item.get("rect", item.get("box")), width, height)
        if not parsed.get("ok"):
            return {"ok": False, "error": f"regions[{i}].rect: {parsed.get('error')}"}
        numeric = bool(item.get("numeric", False))
        whitelist = item.get("whitelist")
        if whitelist is None and numeric:
            whitelist = NUMERIC_WHITELIST
        min_conf = item.get("min_confidence")
        if min_conf is not None:
            try:
                min_conf = float(min_conf)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"regions[{i}].min_confidence must be a number"}
        psm = item.get("psm")
        if psm is None:
            psm = 7
        try:
            psm = int(psm)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"regions[{i}].psm must be an integer"}
        specs.append(
            {
                "name": name,
                "rect": _rect_dict(parsed),
                "numeric": numeric,
                "whitelist": str(whitelist) if whitelist else None,
                "min_confidence": min_conf,
                "psm": psm,
            }
        )
    return specs


_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_numeric(text: str) -> int | float | None:
    """Parse '$1,250' / '3,400.50' / '17' into a number; None if it is not one."""
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "").strip()
    if not cleaned or not _NUMBER.match(cleaned):
        return None
    if "." in cleaned:
        return float(cleaned)
    return int(cleaned)


def _ocr_crop(np: Any, tess: Any, crop: Any, spec: dict[str, Any]) -> tuple[str, float]:
    from PIL import Image, ImageOps

    img = Image.fromarray(crop)
    # tesseract prefers dark text on a light background.
    if float(np.median(crop)) < 128:
        img = ImageOps.invert(img)
    h = img.height
    if h < _OCR_MIN_HEIGHT:
        factor = min(4, math.ceil(_OCR_MIN_HEIGHT / max(1, h)))
        img = img.resize((img.width * factor, h * factor), Image.Resampling.LANCZOS)
    img = ImageOps.expand(img, _OCR_PAD, fill=255)
    config = f"--psm {spec['psm']}"
    if spec.get("whitelist"):
        config += f" -c tessedit_char_whitelist={spec['whitelist']}"
    data = tess.image_to_data(img, config=config, output_type=tess.Output.DICT)
    lines: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data.get("text") or []):
        word = str(word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError, KeyError, IndexError):
            conf = -1.0
        if not word or conf < 0:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        lines.setdefault(key, []).append(word)
        confs.append(conf)
    if not confs:
        return "", 0.0
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    return text, round(min(confs) / 100.0, 4)


def read_screen_regions(
    path: str,
    regions: list[dict[str, Any]] | None = None,
    *,
    fps: float = 2.0,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
    profile: Any = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """OCR named regions over time and return only the changes.

    Each region is ``{name, rect, whitelist?, numeric?, min_confidence?, psm?}``;
    ``rect`` is ``{x,y,w,h}`` or ``[x,y,w,h]`` in pixels or 0-1 fractions.
    Events are ``{t, region, text, confidence}`` (plus ``value`` in numeric
    mode). Reads under the confidence floor are not emitted; they are listed
    under ``rejected``.
    """
    started = time.monotonic()
    np = _numpy()
    if np is None:
        return {"ok": False, "error": "numpy is required for read_screen_regions", "install": OCR_INSTALL}
    tess, tess_version, err = _load_ocr()
    if err is not None:
        return err
    try:
        floor = float(min_confidence)
        change_thr = float(change_threshold)
    except (TypeError, ValueError):
        return {"ok": False, "error": "min_confidence and change_threshold must be numbers"}
    if not (0.0 <= floor <= 1.0):
        return {"ok": False, "error": "min_confidence must be in [0, 1]"}

    try:
        screen, _ = _profile_screen(profile)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"profile: {exc}"}
    if regions is None:
        regions = screen.get("regions") or None
    if not regions:
        return {"ok": False, "error": "regions is required (or a profile with screen.regions)"}

    media = _open_media(path, start_seconds, end_seconds, fps)
    if not media.get("ok"):
        return media
    width, height = media["width"], media["height"]
    start, end, fps_f = media["start"], media["end"], media["fps"]
    specs = _region_specs(regions, width, height)
    if isinstance(specs, dict):
        return specs

    params = {
        "fps": fps_f,
        "start": round(start, 6),
        "end": round(end, 6),
        "regions": specs,
        "min_confidence": floor,
        "change_threshold": change_thr,
        "engine": f"tesseract {tess_version}",
    }
    key = _cache_key(path, "read_screen_regions", params) if use_cache else None
    if key is not None:
        hit = _from_cache(key, started)
        if hit is not None:
            return hit

    ux, uy, uw, uh = _union([s["rect"] for s in specs])
    state: dict[str, dict[str, Any]] = {s["name"]: {"ref": None, "emitted": None} for s in specs}
    events: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    stats = {"ocr_calls": 0, "unchanged_skips": 0, "blank_skips": 0, "rejected": 0}
    frames_read = 0
    try:
        for t, buf in _iter_frames(
            path, start=start, end=end, fps=fps_f, crop=(ux, uy, uw, uh)
        ):
            frames_read += 1
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((uh, uw))
            for spec in specs:
                r = spec["rect"]
                crop = frame[r["y"] - uy : r["y"] - uy + r["h"], r["x"] - ux : r["x"] - ux + r["w"]]
                st = state[spec["name"]]
                if st["ref"] is not None and _changed_fraction(np, crop, st["ref"]) <= change_thr:
                    stats["unchanged_skips"] += 1
                    continue
                st["ref"] = crop.copy()
                if float(crop.std()) < _BLANK_STD:
                    stats["blank_skips"] += 1
                    text, conf = "", 1.0
                else:
                    stats["ocr_calls"] += 1
                    text, conf = _ocr_crop(np, tess, crop, spec)
                region_floor = spec["min_confidence"] if spec["min_confidence"] is not None else floor
                value: int | float | None = None
                reason = None
                if conf < region_floor:
                    reason = "low_confidence"
                elif spec["numeric"] and text:
                    value = parse_numeric(text)
                    if value is None:
                        reason = "not_numeric"
                if reason is not None:
                    stats["rejected"] += 1
                    if len(rejected) < _MAX_REJECTED:
                        rejected.append(
                            {
                                "t": _round_t(t),
                                "region": spec["name"],
                                "text": text,
                                "confidence": conf,
                                "reason": reason,
                            }
                        )
                    continue
                if text == st["emitted"]:
                    continue
                st["emitted"] = text
                event: dict[str, Any] = {
                    "t": _round_t(t),
                    "region": spec["name"],
                    "text": text,
                    "confidence": conf,
                }
                if spec["numeric"]:
                    event["value"] = value
                events.append(event)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "fps": fps_f,
        "start_seconds": start,
        "end_seconds": end,
        "regions": specs,
        "min_confidence": floor,
        "change_threshold": change_thr,
        "engine": {"ocr": "tesseract", "version": tess_version},
        "events": events,
        "rejected": rejected,
        "stats": stats,
        "frames_read": frames_read,
        "runtime_seconds": round(time.monotonic() - started, 4),
    }
    return _store(key, result)


# --------------------------------------------------------------- templates


def _ncc_numpy(np: Any, image: Any, tmpl_fft: Any, tmpl_norm: float, th: int, tw: int) -> Any:
    """Zero-mean NCC map (TM_CCOEFF_NORMED) over valid positions, numpy only."""
    img = image.astype(np.float64)
    H, W = img.shape
    corr = np.fft.irfft2(np.fft.rfft2(img) * tmpl_fft, s=(H, W))
    num = corr[: H - th + 1, : W - tw + 1]
    s1 = _window_sum(np, img, th, tw)
    s2 = _window_sum(np, img * img, th, tw)
    n = float(th * tw)
    var = np.maximum(s2 - s1 * s1 / n, 0.0)
    den = np.sqrt(var) * tmpl_norm
    out = np.zeros_like(num)
    ok = var > (_FLAT_WINDOW_STD**2) * n
    out[ok] = num[ok] / den[ok]
    return np.clip(out, -1.0, 1.0)


def _window_sum(np: Any, arr: Any, th: int, tw: int) -> Any:
    ii = np.zeros((arr.shape[0] + 1, arr.shape[1] + 1), dtype=np.float64)
    ii[1:, 1:] = arr.cumsum(0).cumsum(1)
    return ii[th:, tw:] - ii[:-th, tw:] - ii[th:, :-tw] + ii[:-th, :-tw]


def _ncc_opencv(np: Any, cv2: Any, image: Any, tmpl: Any) -> Any:
    res = cv2.matchTemplate(image, tmpl, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
    th, tw = tmpl.shape
    img = image.astype(np.float64)
    n = float(th * tw)
    s1 = _window_sum(np, img, th, tw)
    s2 = _window_sum(np, img * img, th, tw)
    var = np.maximum(s2 - s1 * s1 / n, 0.0)
    res[var <= (_FLAT_WINDOW_STD**2) * n] = 0.0
    return np.clip(res, -1.0, 1.0)


def _template_list(
    templates: Any, base: Path | None
) -> list[dict[str, Any]] | dict[str, Any]:
    if not isinstance(templates, list) or not templates:
        return {"ok": False, "error": "templates must be a non-empty list of image paths or {name, path}"}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(templates):
        if isinstance(item, (str, os.PathLike)):
            item = {"path": str(item)}
        if not isinstance(item, dict) or not item.get("path"):
            return {"ok": False, "error": f"templates[{i}] must be a path or {{name, path}}"}
        tpath = Path(str(item["path"]))
        if not tpath.is_absolute() and base is not None and not tpath.is_file():
            tpath = base / tpath
        if not tpath.is_file():
            return {"ok": False, "error": f"templates[{i}]: file not found: {item['path']}"}
        name = str(item.get("name") or tpath.stem).strip()
        if name in seen:
            return {"ok": False, "error": f"duplicate template name: {name}"}
        seen.add(name)
        thr = item.get("threshold")
        try:
            thr_f = float(thr) if thr is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "error": f"templates[{i}].threshold must be a number"}
        out.append({"name": name, "path": str(tpath), "threshold": thr_f, "region": item.get("region")})
    return out


def _search_areas(
    regions: Any, width: int, height: int
) -> list[dict[str, Any]] | dict[str, Any]:
    if regions is None:
        return [{"name": None, "rect": {"x": 0, "y": 0, "w": width, "h": height}}]
    if not isinstance(regions, list) or not regions:
        return {"ok": False, "error": "regions must be a non-empty list of rects or {name, rect}"}
    areas: list[dict[str, Any]] = []
    for i, item in enumerate(regions):
        name = None
        rect = item
        if isinstance(item, dict) and ("rect" in item or "box" in item):
            name = str(item.get("name") or "").strip() or None
            rect = item.get("rect", item.get("box"))
        parsed = _parse_box(rect, width, height)
        if not parsed.get("ok"):
            return {"ok": False, "error": f"regions[{i}]: {parsed.get('error')}"}
        areas.append({"name": name, "rect": _rect_dict(parsed)})
    return areas


def _build_spans(
    samples: list[tuple[float, bool, float, dict[str, int] | None, str | None]],
    step: float,
    template: str,
    min_duration: float,
    merge_gap: float,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for t, present, score, rect, area in samples:
        if present:
            if cur is None:
                cur = {"start": t, "end": t + step, "score": score, "rect": rect, "region": area}
            else:
                cur["end"] = t + step
                if score > cur["score"]:
                    cur.update(score=score, rect=rect, region=area)
        elif cur is not None:
            spans.append(cur)
            cur = None
    if cur is not None:
        spans.append(cur)
    merged: list[dict[str, Any]] = []
    for span in spans:
        if merged and span["start"] - merged[-1]["end"] <= merge_gap + 1e-9:
            prev = merged[-1]
            prev["end"] = span["end"]
            if span["score"] > prev["score"]:
                prev.update(score=span["score"], rect=span["rect"], region=span["region"])
        else:
            merged.append(dict(span))
    out: list[dict[str, Any]] = []
    for span in merged:
        if span["end"] - span["start"] + 1e-9 < min_duration:
            continue
        entry = {
            "start": _round_t(span["start"]),
            "end": _round_t(span["end"]),
            "template": template,
            "score": round(float(span["score"]), 4),
            "rect": span["rect"],
        }
        if span["region"]:
            entry["region"] = span["region"]
        out.append(entry)
    return out


def match_template(
    path: str,
    templates: list[Any] | None = None,
    regions: list[Any] | None = None,
    *,
    fps: float = 2.0,
    threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
    min_duration: float = 0.5,
    merge_gap: float = 0.5,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    scale: float | None = None,
    change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
    engine: str = "auto",
    profile: Any = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Find fixed graphics over time; return ``[{start, end, template, score, rect}]`` spans.

    ``templates`` are image paths or ``{name, path, threshold?, region?}``;
    ``region`` names one of ``regions`` (or the profile's ``screen.regions``)
    or is a rect. ``regions`` restricts the search (default: the full frame).
    ``scale`` downsamples frames and templates (default: auto, frames wider
    than 960 px are halved or more). ``end`` is the first sample time after
    the last match, so spans are ``[start, end)`` at the sampling step.
    """
    started = time.monotonic()
    np = _numpy()
    if np is None:
        return {"ok": False, "error": "numpy is required for match_template", "install": ANALYSIS_INSTALL}
    eng = str(engine or "auto").lower()
    if eng not in {"auto", "numpy", "opencv"}:
        return {"ok": False, "error": "engine must be auto, numpy, or opencv"}
    cv2 = None
    if eng in {"auto", "opencv"}:
        try:
            import cv2 as _cv2

            cv2 = _cv2
        except ImportError:
            if eng == "opencv":
                return {"ok": False, "error": "opencv is not installed", "install": "pip install opencv-python-headless"}
    eng_used = "opencv" if cv2 is not None else "numpy"
    try:
        thr = float(threshold)
        min_dur = max(0.0, float(min_duration))
        gap = max(0.0, float(merge_gap))
        change_thr = float(change_threshold)
    except (TypeError, ValueError):
        return {"ok": False, "error": "threshold, min_duration, merge_gap and change_threshold must be numbers"}

    try:
        screen, base = _profile_screen(profile)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"profile: {exc}"}
    if templates is None:
        templates = screen.get("templates") or None
    if not templates:
        return {"ok": False, "error": "templates is required (or a profile with screen.templates)"}
    tlist = _template_list(templates, base)
    if isinstance(tlist, dict):
        return tlist

    media = _open_media(path, start_seconds, end_seconds, fps)
    if not media.get("ok"):
        return media
    width, height = media["width"], media["height"]
    start, end, fps_f = media["start"], media["end"], media["fps"]

    areas = _search_areas(regions, width, height)
    if isinstance(areas, dict):
        return areas
    named: dict[str, dict[str, int]] = {}
    for reg in screen.get("regions") or []:
        if isinstance(reg, dict) and reg.get("name"):
            parsed = _parse_box(reg.get("rect"), width, height)
            if parsed.get("ok"):
                named[str(reg["name"])] = _rect_dict(parsed)
    for area in areas:
        if area["name"]:
            named[area["name"]] = area["rect"]

    # Each template gets its own search areas (its region, else the shared list).
    plan: list[dict[str, Any]] = []
    for tpl in tlist:
        reg = tpl.get("region")
        if reg is None:
            t_areas = areas
        elif isinstance(reg, str):
            if reg not in named:
                return {"ok": False, "error": f"template {tpl['name']}: unknown region {reg!r}"}
            t_areas = [{"name": reg, "rect": named[reg]}]
        else:
            parsed = _parse_box(reg, width, height)
            if not parsed.get("ok"):
                return {"ok": False, "error": f"template {tpl['name']}: region {parsed.get('error')}"}
            t_areas = [{"name": None, "rect": _rect_dict(parsed)}]
        plan.append({**tpl, "areas": t_areas})

    ux, uy, uw, uh = _union([a["rect"] for p in plan for a in p["areas"]])
    if scale is None:
        s = min(1.0, _AUTO_SCALE_WIDTH / float(width))
    else:
        try:
            s = float(scale)
        except (TypeError, ValueError):
            return {"ok": False, "error": "scale must be a number"}
        if not (0 < s <= 1):
            return {"ok": False, "error": "scale must be in (0, 1]"}
    ow, oh = max(2, int(round(uw * s))), max(2, int(round(uh * s)))
    sx, sy = ow / float(uw), oh / float(uh)

    from PIL import Image

    for p in plan:
        try:
            with Image.open(p["path"]) as im:
                gray = im.convert("L")
                src_w, src_h = gray.size
                if s != 1.0:
                    gray = gray.resize(
                        (max(2, int(round(src_w * sx))), max(2, int(round(src_h * sy)))),
                        Image.Resampling.LANCZOS,
                    )
                arr = np.asarray(gray, dtype=np.uint8).copy()
        except OSError as exc:
            return {"ok": False, "error": f"template {p['name']}: cannot read image: {exc}"}
        f = arr.astype(np.float64)
        f0 = f - f.mean()
        norm = float(np.sqrt((f0 * f0).sum()))
        if norm < 1e-6 or float(f.std()) < _FLAT_WINDOW_STD:
            return {"ok": False, "error": f"template {p['name']}: image has no contrast to match"}
        p["arr"], p["f0"], p["norm"] = arr, f0, norm
        p["src_size"] = (src_w, src_h)
        th, tw = arr.shape
        scaled_areas = []
        for a in p["areas"]:
            r = a["rect"]
            ax = int(round((r["x"] - ux) * sx))
            ay = int(round((r["y"] - uy) * sy))
            aw = min(ow - ax, max(1, int(round(r["w"] * sx))))
            ah = min(oh - ay, max(1, int(round(r["h"] * sy))))
            if aw < tw or ah < th:
                return {
                    "ok": False,
                    "error": f"template {p['name']} ({src_w}x{src_h}) is larger than its search region",
                }
            tmpl_fft = None
            if cv2 is None:
                pad = np.zeros((ah, aw), dtype=np.float64)
                pad[:th, :tw] = f0
                tmpl_fft = np.conj(np.fft.rfft2(pad))
            scaled_areas.append({"name": a["name"], "box": (ax, ay, aw, ah), "fft": tmpl_fft, "ref": None, "last": None})
        p["scaled"] = scaled_areas

    params = {
        "fps": fps_f,
        "start": round(start, 6),
        "end": round(end, 6),
        "threshold": thr,
        "min_duration": min_dur,
        "merge_gap": gap,
        "scale": s,
        "change_threshold": change_thr,
        "engine": eng_used,
        "templates": [
            {
                "name": p["name"],
                "file": file_fingerprint(p["path"]),
                "threshold": p["threshold"],
                "areas": [a["rect"] for a in p["areas"]],
            }
            for p in plan
        ],
    }
    key = _cache_key(path, "match_template", params) if use_cache else None
    if key is not None:
        hit = _from_cache(key, started)
        if hit is not None:
            return hit

    samples: dict[str, list[tuple[float, bool, float, dict[str, int] | None, str | None]]] = {
        p["name"]: [] for p in plan
    }
    stats = {"match_calls": 0, "unchanged_skips": 0}
    frames_read = 0
    try:
        for t, buf in _iter_frames(
            path, start=start, end=end, fps=fps_f, crop=(ux, uy, uw, uh), out_size=(ow, oh)
        ):
            frames_read += 1
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((oh, ow))
            for p in plan:
                th, tw = p["arr"].shape
                best: tuple[float, dict[str, int] | None, str | None] = (-1.0, None, None)
                for a in p["scaled"]:
                    ax, ay, aw, ah = a["box"]
                    crop = frame[ay : ay + ah, ax : ax + aw]
                    if a["ref"] is not None and _changed_fraction(np, crop, a["ref"]) <= change_thr:
                        stats["unchanged_skips"] += 1
                        score, rect = a["last"]
                    else:
                        stats["match_calls"] += 1
                        a["ref"] = crop.copy()
                        if cv2 is not None:
                            res = _ncc_opencv(np, cv2, crop, p["arr"])
                        else:
                            res = _ncc_numpy(np, crop, a["fft"], p["norm"], th, tw)
                        iy, ix = np.unravel_index(int(np.argmax(res)), res.shape)
                        score = float(res[iy, ix])
                        src_w, src_h = p["src_size"]
                        rect = {
                            "x": int(round(ux + (ax + ix) / sx)),
                            "y": int(round(uy + (ay + iy) / sy)),
                            "w": int(src_w),
                            "h": int(src_h),
                        }
                        a["last"] = (score, rect)
                    if score > best[0]:
                        best = (score, rect, a["name"])
                t_thr = p["threshold"] if p["threshold"] is not None else thr
                samples[p["name"]].append((t, best[0] >= t_thr, best[0], best[1], best[2]))
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    step = 1.0 / fps_f
    spans: list[dict[str, Any]] = []
    for p in plan:
        spans.extend(_build_spans(samples[p["name"]], step, p["name"], min_dur, gap))
    spans.sort(key=lambda sp: (sp["start"], sp["template"]))

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "fps": fps_f,
        "start_seconds": start,
        "end_seconds": end,
        "threshold": thr,
        "min_duration": min_dur,
        "merge_gap": gap,
        "scale": round(s, 6),
        "engine": eng_used,
        "templates": [
            {
                "name": p["name"],
                "path": p["path"],
                "threshold": p["threshold"] if p["threshold"] is not None else thr,
                "regions": [a["rect"] for a in p["areas"]],
            }
            for p in plan
        ],
        "spans": spans,
        "stats": stats,
        "frames_read": frames_read,
        "runtime_seconds": round(time.monotonic() - started, 4),
    }
    return _store(key, result)
