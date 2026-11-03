"""Loop-seam checks: last frames vs frame 1 (SSIM) and audio-level continuity."""
from __future__ import annotations

import audioop
import math
import os
import tempfile
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media

DEFAULT_SSIM_MIN = 0.85
DEFAULT_AUDIO_JUMP_DB = 6.0
_SSIM_RE = __import__("re").compile(r"All:(\d+(?:\.\d+)?)")


def _extract_frame(path: str, dest: str, *, first: bool) -> None:
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    if first:
        cmd += ["-i", path, "-vf", "select=eq(n\\,0)", "-vframes", "1", dest]
    else:
        cmd += ["-sseof", "-0.05", "-i", path, "-vframes", "1", dest]
    run(cmd, timeout=60)


def _ssim(first_path: str, last_path: str) -> float | None:
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-i",
                last_path,
                "-i",
                first_path,
                "-filter_complex",
                "ssim",
                "-f",
                "null",
                "-",
            ],
            timeout=60,
        )
    except FfmpegError:
        return None
    text = proc.stderr or proc.stdout or ""
    match = _SSIM_RE.search(text)
    if not match:
        return None
    return float(match.group(1))


def _pcm_tail(path: str, *, start: bool, seconds: float = 0.1, sample_rate: int = 8000) -> bytes:
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    if start:
        cmd += ["-i", path, "-t", f"{seconds:.3f}"]
    else:
        cmd += ["-sseof", f"-{seconds:.3f}", "-i", path]
    cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-acodec", "pcm_s16le", tmp.name]
    try:
        run(cmd, timeout=60)
        with open(tmp.name, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def _rms_db(pcm: bytes) -> float:
    if len(pcm) < 4:
        return -120.0
    rms = audioop.rms(pcm, 2)
    if rms <= 0:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


def check_loop(
    path: str,
    *,
    ssim_min: float = DEFAULT_SSIM_MIN,
    audio_jump_db: float = DEFAULT_AUDIO_JUMP_DB,
    mode: str = "fail",
) -> dict[str, Any]:
    """SSIM of last frames vs frame 1, plus first/last 100 ms audio-level jump."""
    if str(mode).lower() == "off":
        return {
            "id": "loop_seam",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "value": 0,
            "limit": 0,
        }
    if not path:
        return {"ok": False, "id": "loop_seam", "pass": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "id": "loop_seam", "pass": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return {"ok": False, "id": "loop_seam", "pass": False, "error": probe.get("error")}

    ssim = None
    tmp = tempfile.mkdtemp(prefix="rushcut-loop-")
    first_img = os.path.join(tmp, "first.png")
    last_img = os.path.join(tmp, "last.png")
    try:
        if probe.get("has_video"):
            _extract_frame(path, first_img, first=True)
            _extract_frame(path, last_img, first=False)
            ssim = _ssim(first_img, last_img)
    except FfmpegError as exc:
        return {
            "ok": False,
            "id": "loop_seam",
            "pass": False,
            "error": str(exc),
            "mode": str(mode).lower(),
        }
    finally:
        for img in (first_img, last_img):
            try:
                os.remove(img)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    audio_jump = None
    first_db = last_db = None
    if probe.get("has_audio"):
        try:
            first_pcm = _pcm_tail(path, start=True)
            last_pcm = _pcm_tail(path, start=False)
            first_db = round(_rms_db(first_pcm), 2)
            last_db = round(_rms_db(last_pcm), 2)
            audio_jump = round(abs(last_db - first_db), 2)
        except FfmpegError:
            audio_jump = None

    video_ok = ssim is None or ssim >= float(ssim_min)
    audio_ok = audio_jump is None or audio_jump <= float(audio_jump_db)
    passed = video_ok and audio_ok
    detail_parts = []
    if ssim is not None and not video_ok:
        detail_parts.append(f"SSIM {ssim:.3f} < {ssim_min}")
    if audio_jump is not None and not audio_ok:
        detail_parts.append(f"audio jump {audio_jump:.1f} dB > {audio_jump_db}")
    return {
        "ok": True,
        "id": "loop_seam",
        "pass": passed,
        "mode": str(mode).lower(),
        "ssim": round(ssim, 4) if ssim is not None else None,
        "ssim_min": float(ssim_min),
        "video_pass": video_ok,
        "audio_jump_db": audio_jump,
        "audio_jump_limit_db": float(audio_jump_db),
        "audio_first_db": first_db,
        "audio_last_db": last_db,
        "audio_pass": audio_ok,
        "value": 0 if passed else 1,
        "limit": 0,
        "detail": "; ".join(detail_parts) or None,
        "path": os.path.abspath(path),
    }
