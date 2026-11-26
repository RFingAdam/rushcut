"""A/V sync verification and cheap recording-folder audits. FFmpeg only.

Two checks, both read-only:

* :func:`verify_sync` measures how far a rendered or stream-copied video sits
  from a reference (``source``) in audio and in picture, and the resulting
  audio/video offset.
* :func:`tape_audit` scans a folder of recordings for video/audio end
  mismatch and timestamp gaps by probing packet timestamps near the ends of
  each file, without decoding.

Method notes (these come from real drift hunts):

* Audio is compared by sample. The whole needed range is decoded once and
  sliced by sample index. Seeking with ``-ss`` before decoding adds an error
  of a frame or more on some containers, which is the quantity being measured.
* Picture is compared by exact decoded frame signature when the video was
  stream-copied (decoded frames are then bit-identical). Frame times come
  from the decoder (``showinfo``), never from counting frames at ``n/fps``:
  stitching pieces with ``n/fps`` instead of real packet timestamps drifts
  about 1 ms per piece. When no exact match exists (re-encoded, static scene)
  it falls back to motion-energy cross-correlation.
* Sign convention: positive means the checked file is LATER than the
  reference.
"""
from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, which_ffmpeg

SR = 16000
_SIG_W, _SIG_H = 64, 36
_MEDIA_EXTS = (".mkv", ".mp4", ".mov", ".ts", ".m4v", ".webm", ".flv", ".avi")


def _np() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FfmpegError(
            "verify_sync needs numpy. Install with: pip install 'rushcut-mcp[analysis]'"
        ) from exc
    return np


def _nice(cmd: list[str]) -> list[str]:
    return ["nice", "-n", "15", *cmd] if os.name == "posix" and os.environ.get("RUSHCUT_NO_NICE") is None else cmd


def _run_bytes(cmd: list[str], timeout: float) -> tuple[bytes, str]:
    try:
        p = subprocess.run(_nice(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"Timed out after {timeout:.0f}s: {cmd[0]}") from exc
    return p.stdout, p.stderr.decode("utf-8", "replace")


def _ffprobe(path: str, args: list[str], timeout: float = 60) -> str:
    cmd = [which_ffmpeg("ffprobe"), "-v", "error", *args, path]
    out, _ = _run_bytes(cmd, timeout)
    return out.decode("utf-8", "replace")


def _floats(text: str) -> list[float]:
    vals = []
    for tok in text.replace(",", " ").split():
        try:
            vals.append(float(tok))
        except ValueError:
            continue  # "N/A"
    return vals


def _stream_info(path: str) -> dict[str, Any]:
    import json

    raw = _ffprobe(path, ["-show_entries", "format=duration,start_time:stream=index,codec_type,r_frame_rate,avg_frame_rate,start_time",
                          "-of", "json"])
    data = json.loads(raw or "{}")
    fmt = data.get("format", {})
    vids = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    auds = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    fps = 0.0
    if vids:
        num, _, den = (vids[0].get("avg_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        if fps <= 0:
            num, _, den = (vids[0].get("r_frame_rate") or "0/1").partition("/")
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = 0.0
    def _f(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return {
        "duration": _f(fmt.get("duration")),
        "start_time": 0.0,  # picture times are raw pts on both sides
        "video_start": (_f(vids[0].get("start_time")) or 0.0) if vids else 0.0,
        "audio_starts": [_f(a.get("start_time")) or 0.0 for a in auds],
        "fps": fps,
        "has_video": bool(vids),
        "audio_streams": len(auds),
    }


# --------------------------------------------------------------------- audio


def _decode_audio(path: str, upto: float | None, stream: int, timeout: float) -> Any:
    """Mono 16 kHz float samples from t=0 (``-t`` only, never ``-ss``)."""
    np = _np()
    cmd = [which_ffmpeg("ffmpeg"), "-v", "error", "-i", path, "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(SR)]
    if upto is not None:
        cmd += ["-t", f"{upto:.3f}"]
    cmd += ["-f", "f32le", "-"]
    out, err = _run_bytes(cmd, timeout)
    if not out:
        raise FfmpegError(f"No audio decoded from {path}: {err.strip()[:300]}")
    return np.frombuffer(out[: len(out) // 4 * 4], dtype=np.float32)


def _bandpass(x: Any, lo: float = 300.0, hi: float = 3400.0) -> Any:
    np = _np()
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SR)
    spec[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(spec, len(x))


def _xcorr_lag(cand: Any, ref: Any, max_lag: int) -> tuple[int, float, float]:
    """Lag in samples of ``cand`` relative to ``ref`` (positive = cand later).

    ``ref`` is the longer window, padded ``max_lag`` samples each side. Returns
    (lag, normalised peak correlation, peak / next-best-peak ratio).
    """
    np = _np()
    a = _bandpass(cand - cand.mean())
    n = len(a)
    b = _bandpass(ref - ref.mean())
    size = 1 << (len(b) + n).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    cc = np.fft.irfft(fb * np.conj(fa), size)  # cc[k] = sum b[i+k] a[i]
    ks = np.arange(0, 2 * max_lag + 1)
    vals = cc[ks]
    # Normalise each lag by the energy of the ref slice it overlaps.
    csum = np.concatenate(([0.0], np.cumsum(b.astype(np.float64) ** 2)))
    e_b = csum[ks + n] - csum[ks]
    norm = np.sqrt(np.maximum(e_b, 1e-12) * max((a.astype(np.float64) ** 2).sum(), 1e-12))
    score = np.abs(vals) / norm
    best = int(np.argmax(score))
    top = float(score[best])
    masked = score.copy()
    lo, hi = max(0, best - 8), min(len(masked), best + 9)
    masked[lo:hi] = 0
    second = float(masked.max()) if masked.size else 0.0
    lag = max_lag - best  # ref content found later in the window => cand is earlier
    return lag, top, (top / second if second > 1e-9 else 99.0)


def _sample_times(duration: float, window: float, points: list[float] | None) -> list[float]:
    if points:
        return [float(p) for p in points if 0 <= p <= max(duration - 1.0, 0)]
    if duration <= window + 1.0:
        return [max(0.0, (duration - window) / 2)]
    return sorted({round(t, 3) for t in (1.0, duration / 2 - window / 2, duration - window - 1.0) if t >= 0})


def _audio_check(
    video: str, source: str, source_start: float, duration: float, times: list[float],
    window: float, max_lag_ms: float, audio_stream: int, timeout: float,
    c_start: float = 0.0, s_start: float = 0.0,
) -> dict[str, Any]:
    np = _np()
    cand = _decode_audio(video, None, audio_stream, timeout)
    margin = int(max_lag_ms / 1000 * SR)
    need = max(source_start + max(times) - s_start, 0) + window + max_lag_ms / 1000 + 1.0
    ref = _decode_audio(source, need, audio_stream, timeout)
    points = []
    for t in times:
        c0 = int(round((t - c_start) * SR))
        c = cand[c0: c0 + int(window * SR)]
        r0 = int(round((source_start + t - s_start) * SR)) - margin
        r = ref[max(r0, 0): r0 + len(c) + 2 * margin]
        pad = max(0, -r0)
        if pad:
            r = np.concatenate((np.zeros(pad, np.float32), r))
        if len(c) < SR or len(r) < len(c) + 2 * margin:
            continue
        lag, corr, ratio = _xcorr_lag(c, r, margin)
        points.append({
            "t": t,
            "lag_ms": round(lag / SR * 1000, 3),
            "correlation": round(corr, 3),
            "peak_ratio": round(ratio, 2),
            "reliable": corr >= 0.3 and ratio >= 1.5,
        })
    rel = [p for p in points if p["reliable"]]
    return {"points": points, "lag_ms": round(float(np.median([p["lag_ms"] for p in rel])), 3) if rel else None,
            "drift_ms": round(max(p["lag_ms"] for p in rel) - min(p["lag_ms"] for p in rel), 3) if len(rel) > 1 else 0.0}


# --------------------------------------------------------------------- video


def _frames(path: str, ss: float, span: float, size: tuple[int, int], fps: float | None, timeout: float) -> tuple[Any, list[float]]:
    """Decoded gray frames (N, H*W) and their real pts (decoder reported)."""
    np = _np()
    w, h = size
    vf = (f"fps={fps}," if fps else "") + f"scale={w}:{h}:flags=area,format=gray,showinfo"
    cmd = [which_ffmpeg("ffmpeg"), "-v", "info", "-nostats", "-ss", f"{max(ss, 0):.4f}", "-t", f"{span:.4f}", "-copyts",
           "-i", path, "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    out, err = _run_bytes(cmd, timeout)
    pts = [float(m) for m in re.findall(r"pts_time:\s*(-?[0-9.]+)", err)]
    n = min(len(out) // (w * h), len(pts))
    arr = np.frombuffer(out[: n * w * h], dtype=np.uint8).reshape(n, w * h).astype(np.float32)
    return arr, pts[:n]


def _exact_video_match(
    video: str, source: str, v_start: float, s_start: float, source_start: float, t: float,
    tol: float, timeout: float,
) -> dict[str, Any]:
    np = _np()
    span = 1.0
    cf, cp = _frames(video, v_start + t, span, (_SIG_W, _SIG_H), None, timeout)
    sf, sp = _frames(source, s_start + source_start + t - 0.5, span + 1.0, (_SIG_W, _SIG_H), None, timeout)
    if len(cf) < 3 or len(sf) < 3:
        return {"t": t, "matched": False, "reason": "too few frames decoded"}
    lags, dists = [], []
    for i in range(len(cf)):
        d = np.abs(sf - cf[i]).mean(axis=1)
        j = int(np.argmin(d))
        order = np.sort(d)
        if d[j] <= tol and (len(order) < 2 or order[1] - order[0] > 0.2):
            lags.append((cp[i] - v_start) - ((sp[j] - s_start) - source_start))  # + = cand later
            dists.append(float(d[j]))
    if len(lags) < max(2, len(cf) // 4):
        return {"t": t, "matched": False, "reason": "no unique exact frame match (re-encoded or static picture)"}
    lag = float(np.median(lags))
    mean_diff = float(np.mean(dists))
    # Stream-copied frames decode bit-identically (diff 0). Re-encodes only match closely.
    return {"t": t, "matched": True, "method": "exact_frame" if mean_diff <= 0.05 else "near_frame",
            "lag_ms": round(lag * 1000, 3), "frames_matched": len(lags), "mean_pixel_diff": round(mean_diff, 3)}


def _motion(path: str, ss: float, span: float, fps: float, timeout: float) -> Any:
    np = _np()
    f, _ = _frames(path, ss, span, (160, 90), fps, timeout)
    if len(f) < 4:
        return np.zeros(0)
    return np.r_[0, np.abs(np.diff(f, axis=0)).mean(axis=1)]


def _motion_video_lag(video: str, source: str, v_start: float, s_start: float, source_start: float,
                      t: float, fps: float, timeout: float) -> dict[str, Any]:
    np = _np()
    fps = fps if fps > 0 else 30.0
    a = _motion(video, v_start + t, 8.0, fps, timeout)
    b = _motion(source, s_start + source_start + t, 8.0, fps, timeout)
    m = min(len(a), len(b))
    if m < 16:
        return {"t": t, "matched": False, "reason": "clip too short for motion correlation"}
    a, b = a[:m] - a[:m].mean(), b[:m] - b[:m].mean()
    best = (-9.0, 0)
    for k in range(-12, 13):
        x = a[max(0, k): m + min(0, k)]
        y = b[max(0, -k): m - max(0, k)]
        c = float((x * y).sum() / np.sqrt((x ** 2).sum() * (y ** 2).sum() + 1e-9))
        best = max(best, (c, k))
    c, k = best
    return {"t": t, "matched": c >= 0.5, "method": "motion_xcorr", "lag_ms": round(k / fps * 1000, 3),
            "correlation": round(c, 3), "frames": k}


def _video_check(video: str, source: str, vinfo: dict[str, Any], sinfo: dict[str, Any], source_start: float,
                 times: list[float], timeout: float) -> dict[str, Any]:
    pts = []
    for t in times:
        r = _exact_video_match(video, source, vinfo["start_time"], sinfo["start_time"], source_start, t, 1.5, timeout)
        if not r["matched"]:
            r = _motion_video_lag(video, source, vinfo["start_time"], sinfo["start_time"], source_start, t,
                                  vinfo["fps"], timeout)
        pts.append(r)
    np = _np()
    ok = [p for p in pts if p.get("matched")]
    return {"points": pts, "lag_ms": round(float(np.median([p["lag_ms"] for p in ok])), 3) if ok else None,
            "exact": bool(ok) and all(p.get("method") == "exact_frame" for p in ok)}


def verify_sync(
    video: str,
    source: str | None = None,
    source_start: float = 0.0,
    points: list[float] | None = None,
    window_seconds: float = 5.0,
    max_lag_ms: float = 250.0,
    audio_stream: int = 0,
    audio_tolerance_ms: float = 5.0,
    av_tolerance_ms: float = 20.0,
    check_video: bool = True,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """Report audio/video offset of ``video``, optionally against ``source``.

    ``source_start`` is where ``video`` begins on the source timeline in
    seconds (0 for a render of the whole source). ``points`` are times in
    ``video`` to sample; by default start, middle and end.

    Always reports container-level facts (stream duration/start mismatch). With
    a ``source`` it adds sample-accurate audio lag (cross-correlation against
    the source audio, sliced by sample from a full decode), picture lag (exact
    frame match for stream-copied video, motion correlation otherwise), the
    resulting ``av_offset_ms`` (picture lag minus audio lag; positive = picture
    late against sound) and ``drift_ms`` across the sample points.

    Decoding the source reads it from t=0 up to the last sample point; this is
    deliberate (no ``-ss`` error) but costs time on long sources.
    """
    if not os.path.isfile(video):
        return {"ok": False, "error": f"video not found: {video}"}
    try:
        vinfo = _stream_info(video)
        if not vinfo["has_video"] or not vinfo["audio_streams"]:
            return {"ok": False, "error": "file needs one video and one audio stream"}
        if not vinfo["duration"]:
            return {"ok": False, "error": "duration unknown (unfinalized file?); run repair_media first"}
        result: dict[str, Any] = {"ok": True, "file": video, "duration": vinfo["duration"], "fps": vinfo["fps"],
                                  "convention": "positive = checked file later than the reference", "findings": []}
        if source is None:
            result["note"] = "no source given: container checks only; pass source for audio/video offset"
            return result
        if not os.path.isfile(source):
            return {"ok": False, "error": f"source not found: {source}"}
        sinfo = _stream_info(source)
        times = _sample_times(vinfo["duration"], window_seconds, points)
        if not times:
            return {"ok": False, "error": "no usable sample points inside the video"}
        audio = _audio_check(video, source, source_start, vinfo["duration"], times, window_seconds,
                             max_lag_ms, audio_stream, timeout,
                             c_start=vinfo["audio_starts"][audio_stream] if audio_stream < len(vinfo["audio_starts"]) else 0.0,
                             s_start=sinfo["audio_starts"][audio_stream] if audio_stream < len(sinfo["audio_starts"]) else 0.0)
        result["audio"] = audio
        if audio["lag_ms"] is None:
            result["findings"].append("audio: no reliable correlation (wrong source_start, or different audio)")
        elif abs(audio["drift_ms"]) > audio_tolerance_ms:
            result["findings"].append(f"audio drifts {audio['drift_ms']} ms across the file (limit {audio_tolerance_ms})")
        if check_video and sinfo["has_video"]:
            vid = _video_check(video, source, vinfo, sinfo, source_start, times, timeout)
            result["picture"] = vid
            if vid["lag_ms"] is None:
                result["findings"].append("picture: no match against the source")
            if audio["lag_ms"] is not None and vid["lag_ms"] is not None:
                av = round(vid["lag_ms"] - audio["lag_ms"], 3)
                result["av_offset_ms"] = av
                if abs(av) > av_tolerance_ms:
                    result["findings"].append(
                        f"A/V offset {av} ms exceeds {av_tolerance_ms} ms (picture {'late' if av > 0 else 'early'} against sound)")
        result["ok"] = not result["findings"]
        return result
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- tape audit


def _tape_probe(path: str, tail_seconds: float, head_packets: int = 3) -> dict[str, Any]:
    info = {"file": os.path.basename(path)}
    try:
        dur = _stream_info(path)["duration"]
        if not dur:
            return {**info, "error": "duration unknown (unfinalized file?)"}
        info["duration"] = round(dur, 3)
        tail_from = f"{max(dur - tail_seconds, 0):.1f}%+{tail_seconds + 5:.0f}"
        for sel, name in (("v:0", "video"), ("a:0", "audio0"), ("a:1", "audio1"), ("a:2", "audio2")):
            first = _floats(_ffprobe(path, ["-select_streams", sel, "-show_entries", "packet=pts_time",
                                            "-read_intervals", f"0%+#{head_packets}", "-of", "csv=p=0"]))
            tail = sorted(_floats(_ffprobe(path, ["-select_streams", sel, "-show_entries", "packet=pts_time",
                                                  "-read_intervals", tail_from, "-of", "csv=p=0"], timeout=180)))
            if not first and not tail:
                if name.startswith("audio") and name != "audio0":
                    continue
                if name == "audio0":
                    info["audio0_missing"] = True
                continue
            info[f"{name}_first"] = round(min(first), 4) if first else None
            info[f"{name}_last"] = round(tail[-1], 4) if tail else None
            if name == "video" and len(tail) > 10:
                dts = [b - a for a, b in zip(tail, tail[1:])]
                med = sorted(dts)[len(dts) // 2]
                info["video_frame_dt"] = round(med, 5)
                gaps = [d for d in dts if d > med * 1.5]
                info["video_gaps"] = len(gaps)
                info["video_max_gap_ms"] = round(max(dts) * 1000, 2)
    except (FfmpegError, ValueError) as exc:
        return {**info, "error": str(exc)}
    return info


def _judge(info: dict[str, Any], end_ms: float, start_ms: float) -> list[str]:
    if "error" in info:
        return [f"probe failed: {info['error']}"]
    flags: list[str] = []
    if info.get("audio0_missing"):
        flags.append("no audio stream")
    v_last = info.get("video_last")
    for k in ("audio0", "audio1", "audio2"):
        a_last = info.get(f"{k}_last")
        if v_last is not None and a_last is not None:
            diff = (v_last - a_last) * 1000
            info[f"{k}_end_mismatch_ms"] = round(diff, 1)
            if abs(diff) > end_ms:
                flags.append(f"{k} ends {abs(diff):.0f} ms {'before' if diff > 0 else 'after'} video")
        v_first, a_first = info.get("video_first"), info.get(f"{k}_first")
        if v_first is not None and a_first is not None:
            skew = (a_first - v_first) * 1000
            info[f"{k}_start_skew_ms"] = round(skew, 1)
            if abs(skew) > start_ms:
                flags.append(f"{k} starts {abs(skew):.0f} ms {'after' if skew > 0 else 'before'} video")
    if info.get("video_gaps"):
        flags.append(f"{info['video_gaps']} video timestamp gap(s) in the tail, max {info['video_max_gap_ms']} ms (dropped frames)")
    return flags


def tape_audit(
    folder: str,
    tail_seconds: float = 20.0,
    end_mismatch_ms: float = 100.0,
    start_skew_ms: float = 100.0,
    workers: int = 2,
    extensions: list[str] | None = None,
) -> dict[str, Any]:
    """Cheap audit of every recording in ``folder`` (no decode).

    Reads only the first packets and the last ``tail_seconds`` of packet
    timestamps per stream. Flags audio ending away from video, audio/video
    start skew, files whose duration is unreadable (unfinalized) and video
    timestamp gaps (dropped frames) in the tail.
    """
    root = Path(folder)
    if not root.is_dir():
        return {"ok": False, "error": f"not a folder: {folder}"}
    exts = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in (extensions or _MEDIA_EXTS))
    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in exts)
    if not files:
        return {"ok": False, "error": f"no media files in {folder}"}
    with ThreadPoolExecutor(max(1, min(int(workers), 4))) as ex:
        infos = list(ex.map(lambda p: _tape_probe(str(p), tail_seconds), files))
    flagged = 0
    for info in infos:
        info["flags"] = _judge(info, end_mismatch_ms, start_skew_ms)
        flagged += bool(info["flags"])
    return {"ok": flagged == 0, "folder": str(root), "files": len(infos), "flagged": flagged,
            "convention": "end mismatch = video_last - audio_last (positive: audio ends early)", "results": infos}
