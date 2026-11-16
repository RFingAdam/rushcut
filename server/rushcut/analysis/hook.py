"""Opening-window hook strength. CPU-only, no NLE, no ASR."""
from __future__ import annotations

import audioop
import os
from typing import Any

from .audio import pcm_dbfs
from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media
from .region import _face_present, _stats

DEFAULT_WINDOW_S = 3.0
_SAMPLE_FPS = 8.0
_SCALE_WIDTH = 160
_AUDIO_RATE = 8000
_FIRST_SECOND = 1.0
_BASELINE_S = 0.15
_HOP_S = 0.08
_SPEECH_DBFS = -35.0
_SPEECH_RMS_MIN = 200.0
_RISE_DB_FULL = 12.0
_MOTION_FULL = 0.05
_CARD_MEAN = 30.0
_CARD_EDGE = 8.0
# Title-card band: top ~22% of the frame, matching a typical Short card.
_CARD_BOX = (0.01, 0.018, 0.98, 0.22)
_WEIGHTS = {
    "motion_energy": 0.25,
    "audio_loudness_rise": 0.20,
    "speech_first_second": 0.20,
    "text_card_present": 0.25,
    "face_present": 0.10,
}


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _round01(value: float) -> float:
    return round(_clip01(value), 4)


def _decode_gray_window(
    path: str,
    duration: float,
    width: int,
    height: int,
    fps: float,
) -> tuple[bytes, int, int]:
    scale_h = max(2, int(round(height * _SCALE_WIDTH / max(1, width))))
    if scale_h % 2:
        scale_h += 1
    vf = f"fps={fps:.6f},scale={_SCALE_WIDTH}:{scale_h},format=gray"
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        path,
        "-t",
        f"{duration:.6f}",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    timeout = max(30.0, duration * 5.0 + 10.0)
    proc = run(cmd, timeout=timeout, text=False)
    return proc.stdout or b"", _SCALE_WIDTH, scale_h


def _decode_pcm_window(path: str, duration: float, sample_rate: int = _AUDIO_RATE) -> bytes:
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        path,
        "-t",
        f"{duration:.6f}",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    timeout = max(30.0, duration * 5.0 + 10.0)
    proc = run(cmd, timeout=timeout, text=False)
    return proc.stdout or b""


def _frame_at(raw: bytes, index: int, frame_size: int) -> bytes:
    start = index * frame_size
    return raw[start : start + frame_size]


def _motion_energy(raw: bytes, frame_size: int, n_frames: int) -> float:
    if n_frames < 2 or frame_size <= 0:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(1, n_frames):
        prev = _frame_at(raw, i - 1, frame_size)
        cur = _frame_at(raw, i, frame_size)
        if len(prev) < frame_size or len(cur) < frame_size:
            continue
        acc = 0
        for a, b in zip(prev, cur):
            acc += abs(a - b)
        total += acc / frame_size / 255.0
        pairs += 1
    if not pairs:
        return 0.0
    return total / pairs


def _crop_frame(frame: bytes, width: int, height: int, box: tuple[float, float, float, float]) -> tuple[bytes, int, int]:
    x, y, w, h = box
    bx = max(0, min(width - 2, int(round(x * width))))
    by = max(0, min(height - 2, int(round(y * height))))
    bw = max(2, min(width - bx, int(round(w * width))))
    bh = max(2, min(height - by, int(round(h * height))))
    rows = []
    for row in range(bh):
        start = (by + row) * width + bx
        rows.append(frame[start : start + bw])
    return b"".join(rows), bw, bh


def _card_present(raw: bytes, width: int, height: int, n_frames: int) -> bool:
    frame_size = width * height
    for i in range(n_frames):
        frame = _frame_at(raw, i, frame_size)
        if len(frame) < frame_size:
            continue
        crop, cw, ch = _crop_frame(frame, width, height, _CARD_BOX)
        if cw * ch <= 0 or len(crop) < cw * ch:
            continue
        mean, _variance, edge = _stats(crop)
        if mean >= _CARD_MEAN or edge >= _CARD_EDGE:
            return True
    return False


def _face_flag(raw: bytes, width: int, height: int, n_frames: int) -> tuple[bool, str]:
    frame_size = width * height
    detector = "unavailable"
    step = max(1, int(round(_SAMPLE_FPS / 2.0)))
    for i in range(0, n_frames, step):
        frame = _frame_at(raw, i, frame_size)
        if len(frame) < frame_size:
            continue
        found, detector = _face_present(frame, width, height)
        if detector == "unavailable":
            return False, detector
        if found:
            return True, detector
    return False, detector


def _audio_signals(pcm: bytes, sample_rate: int) -> tuple[float, bool, float, float]:
    if not pcm or len(pcm) < 8:
        return 0.0, False, pcm_dbfs(0.0), pcm_dbfs(0.0)
    first_n = min(len(pcm), int(sample_rate * _FIRST_SECOND) * 2)
    first_rms = audioop.rms(pcm[:first_n], 2)
    first_db = pcm_dbfs(first_rms)
    speech = first_db >= _SPEECH_DBFS and first_rms >= _SPEECH_RMS_MIN

    hop = max(2, int(sample_rate * _HOP_S) * 2)
    peak_rms = 0.0
    for i in range(0, len(pcm) - 8, hop):
        chunk = pcm[i : i + hop]
        if len(chunk) < 8:
            continue
        rms = audioop.rms(chunk, 2)
        if rms > peak_rms:
            peak_rms = rms
    base_n = min(len(pcm), max(8, int(sample_rate * _BASELINE_S) * 2))
    base_rms = audioop.rms(pcm[:base_n], 2)
    rise_db = pcm_dbfs(peak_rms) - pcm_dbfs(base_rms)
    rise = _round01(rise_db / _RISE_DB_FULL)
    return rise, speech, rise_db, first_db


def _breakdown_row(value: float, weight: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "value": _round01(value),
        "weight": weight,
        "contribution": round(_round01(value) * weight, 4),
    }
    if extra:
        row.update(extra)
    return row


def score_hook(path: str, window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
    """Score the opening ``window_s`` seconds of a clip for hook strength."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        window = float(window_s)
    except (TypeError, ValueError):
        return {"ok": False, "error": "window_s must be a number"}
    if window <= 0:
        return {"ok": False, "error": "window_s must be greater than 0"}

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_video"):
        return {"ok": False, "error": "no video stream", "path": os.path.abspath(path)}

    duration = float(probe.get("duration_seconds") or 0.0)
    window = min(window, duration) if duration > 0 else window
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "video has no size", "path": os.path.abspath(path)}

    try:
        raw, sw, sh = _decode_gray_window(path, window, width, height, _SAMPLE_FPS)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    frame_size = sw * sh
    n_frames = len(raw) // frame_size if frame_size else 0
    motion_raw = _motion_energy(raw, frame_size, n_frames)
    motion = _round01(motion_raw / _MOTION_FULL)
    card = _card_present(raw, sw, sh, n_frames)
    face, face_detector = _face_flag(raw, sw, sh, n_frames)

    rise = 0.0
    speech = False
    rise_db = 0.0
    first_db = pcm_dbfs(0.0)
    has_audio = bool(probe.get("has_audio"))
    if has_audio:
        try:
            pcm = _decode_pcm_window(path, max(window, _FIRST_SECOND))
        except FfmpegError as exc:
            return {"ok": False, "error": str(exc)}
        rise, speech, rise_db, first_db = _audio_signals(pcm, _AUDIO_RATE)

    breakdown = {
        "motion_energy": _breakdown_row(motion, _WEIGHTS["motion_energy"], {"raw": round(motion_raw, 6)}),
        "audio_loudness_rise": _breakdown_row(
            rise, _WEIGHTS["audio_loudness_rise"], {"rise_db": round(rise_db, 3)}
        ),
        "speech_first_second": _breakdown_row(
            1.0 if speech else 0.0,
            _WEIGHTS["speech_first_second"],
            {"present": speech, "first_second_dbfs": round(first_db, 3)},
        ),
        "text_card_present": _breakdown_row(
            1.0 if card else 0.0, _WEIGHTS["text_card_present"], {"present": card}
        ),
        "face_present": _breakdown_row(
            1.0 if face else 0.0,
            _WEIGHTS["face_present"],
            {"present": face, "detector": face_detector},
        ),
    }
    hook_score = round(sum(row["contribution"] for row in breakdown.values()), 4)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "window_s": round(window, 4),
        "duration_seconds": duration,
        "motion_energy": motion,
        "audio_loudness_rise": rise,
        "speech_first_second": speech,
        "text_card_present": card,
        "face_present": face,
        "hook_score": hook_score,
        "breakdown": breakdown,
    }
