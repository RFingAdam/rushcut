"""Loop-seam checks: last frames vs frame 1 (SSIM/PSNR) and audio wrap."""
from __future__ import annotations

import audioop
import math
import os
import re
import tempfile
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media

DEFAULT_SSIM_MIN = 0.85
DEFAULT_PSNR_MIN = 20.0
DEFAULT_AUDIO_JUMP_DB = 6.0
DEFAULT_SAMPLE_STEP = 0.5
_SSIM_RE = re.compile(r"All:(\d+(?:\.\d+)?)")
_PSNR_RE = re.compile(r"average:(\d+(?:\.\d+)?)", re.IGNORECASE)


def _extract_frame(path: str, dest: str, *, first: bool, offset_frames: int = 0) -> None:
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    if first:
        sel = f"eq(n\\,{offset_frames})"
        cmd += ["-i", path, "-vf", f"select={sel}", "-vframes", "1", dest]
    elif offset_frames:
        cmd += ["-i", path, "-vf", f"select=eq(n\\,{offset_frames})", "-vframes", "1", dest]
    else:
        cmd += ["-sseof", "-0.04", "-i", path, "-vframes", "1", dest]
    run(cmd, timeout=60)


def _last_frame_index(probe: dict[str, Any]) -> int | None:
    fps = probe.get("fps")
    duration = probe.get("stream_max_duration_seconds") or probe.get("duration_seconds")
    if fps and duration:
        frames = max(1, int(round(float(duration) * float(fps))))
        return frames - 1
    return None


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


def _psnr(first_path: str, last_path: str) -> float | None:
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
                "psnr",
                "-f",
                "null",
                "-",
            ],
            timeout=60,
        )
    except FfmpegError:
        return None
    text = proc.stderr or proc.stdout or ""
    match = _PSNR_RE.search(text)
    if not match:
        return None
    return float(match.group(1))


def _mean_abs_diff(a_path: str, b_path: str) -> float | None:
    """Mean abs luma difference in 0..1 via a 1x1 gray scale of the absdiff."""
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-i",
                a_path,
                "-i",
                b_path,
                "-filter_complex",
                "[0:v][1:v]blend=all_mode=difference,format=gray,scale=1:1",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            timeout=60,
            text=False,
        )
    except FfmpegError:
        return None
    raw = proc.stdout or b""
    if not raw:
        return None
    return raw[0] / 255.0


def _pcm_tail(path: str, *, start: bool, seconds: float = 0.1, sample_rate: int = 48000) -> bytes:
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


def _sample_step(first_pcm: bytes, last_pcm: bytes) -> float:
    """Absolute first-vs-last sample step as a fraction of full scale."""
    if len(first_pcm) < 2 or len(last_pcm) < 2:
        return 0.0
    first = int.from_bytes(first_pcm[:2], "little", signed=True)
    last = int.from_bytes(last_pcm[-2:], "little", signed=True)
    return abs(last - first) / 32768.0


def _edge_fade_seconds(pcm: bytes, sample_rate: int, *, from_start: bool) -> float:
    """Seconds until the envelope reaches half of the window peak (0 if none)."""
    width = 2
    if len(pcm) < width * 8:
        return 0.0
    peak = audioop.rms(pcm, width)
    if peak <= 0:
        return 0.0
    threshold = peak * 0.5
    frame = max(width * 2, int(sample_rate * 0.002) * width)
    n = len(pcm) // frame
    if n <= 1:
        return 0.0
    order = range(n) if from_start else range(n - 1, -1, -1)
    for i in order:
        chunk = pcm[i * frame : (i + 1) * frame]
        if audioop.rms(chunk, width) >= threshold:
            samples = (i * frame // width) if from_start else ((n - 1 - i) * frame // width)
            return samples / float(sample_rate)
    return 0.0


def check_loop_video(
    path: str,
    *,
    ssim_min: float = DEFAULT_SSIM_MIN,
    psnr_min: float = DEFAULT_PSNR_MIN,
    mode: str = "fail",
) -> dict[str, Any]:
    """SSIM/PSNR of the last frame vs frame 1, plus a motion-continuity hint."""
    if str(mode).lower() == "off":
        return {
            "id": "loop_video",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "value": 0,
            "limit": float(ssim_min),
        }
    if not path or not os.path.isfile(path):
        return {
            "id": "loop_video",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(ssim_min),
            "error": f"file not found: {path}",
        }
    probe = probe_media(path)
    if not probe.get("ok"):
        return {
            "id": "loop_video",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(ssim_min),
            "error": probe.get("error"),
        }
    if not probe.get("has_video"):
        return {
            "id": "loop_video",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "value": 0,
            "limit": float(ssim_min),
            "note": "no video stream",
        }
    tmp = tempfile.mkdtemp(prefix="rushcut-loop-v-")
    first_img = os.path.join(tmp, "first.png")
    last_img = os.path.join(tmp, "last.png")
    prev_img = os.path.join(tmp, "prev.png")
    ssim = psnr = None
    motion_internal = motion_wrap = None
    try:
        last_n = _last_frame_index(probe)
        _extract_frame(path, first_img, first=True)
        if last_n is not None:
            _extract_frame(path, last_img, first=False, offset_frames=last_n)
            if last_n > 0:
                try:
                    _extract_frame(path, prev_img, first=False, offset_frames=last_n - 1)
                except FfmpegError:
                    prev_img = ""
        else:
            _extract_frame(path, last_img, first=False)
            prev_img = ""
        ssim = _ssim(first_img, last_img)
        psnr = _psnr(first_img, last_img)
        if prev_img and os.path.isfile(prev_img):
            motion_internal = _mean_abs_diff(prev_img, last_img)
            motion_wrap = _mean_abs_diff(last_img, first_img)
    except FfmpegError as exc:
        return {
            "id": "loop_video",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(ssim_min),
            "error": str(exc),
        }
    finally:
        for img in (first_img, last_img, prev_img):
            if img:
                try:
                    os.remove(img)
                except OSError:
                    pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    ssim_ok = ssim is not None and ssim >= float(ssim_min)
    psnr_ok = psnr is None or psnr >= float(psnr_min)
    motion_hint = None
    if motion_internal is not None and motion_wrap is not None:
        # Wrap should not jump much more than the last internal step.
        continuous = motion_wrap <= max(0.08, motion_internal * 2.5 + 0.02)
        motion_hint = "continuous" if continuous else "discontinuous"
    passed = ssim_ok and psnr_ok
    detail_parts = []
    if ssim is not None and not ssim_ok:
        detail_parts.append(f"SSIM {ssim:.3f} < {ssim_min}")
    if psnr is not None and not psnr_ok:
        detail_parts.append(f"PSNR {psnr:.1f} < {psnr_min}")
    if motion_hint == "discontinuous":
        detail_parts.append("motion wrap is discontinuous")
    return {
        "id": "loop_video",
        "pass": passed,
        "mode": str(mode).lower(),
        "value": round(ssim, 4) if ssim is not None else None,
        "limit": float(ssim_min),
        "ssim": round(ssim, 4) if ssim is not None else None,
        "psnr": round(psnr, 3) if psnr is not None else None,
        "psnr_min": float(psnr_min),
        "motion_hint": motion_hint,
        "motion_internal": motion_internal,
        "motion_wrap": motion_wrap,
        "detail": "; ".join(detail_parts) or None,
        "path": os.path.abspath(path),
    }


def check_loop_audio(
    path: str,
    *,
    audio_jump_db: float = DEFAULT_AUDIO_JUMP_DB,
    sample_step: float = DEFAULT_SAMPLE_STEP,
    mode: str = "fail",
) -> dict[str, Any]:
    """RMS/LUFS jump, sample-step click, and edge-fade length across the wrap."""
    if str(mode).lower() == "off":
        return {
            "id": "loop_audio",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "value": 0,
            "limit": float(audio_jump_db),
        }
    if not path or not os.path.isfile(path):
        return {
            "id": "loop_audio",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(audio_jump_db),
            "error": f"file not found: {path}",
        }
    probe = probe_media(path)
    if not probe.get("ok"):
        return {
            "id": "loop_audio",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(audio_jump_db),
            "error": probe.get("error"),
        }
    if not probe.get("has_audio"):
        return {
            "id": "loop_audio",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "value": 0,
            "limit": float(audio_jump_db),
            "note": "no audio stream",
        }
    sample_rate = 48000
    try:
        first_pcm = _pcm_tail(path, start=True, sample_rate=sample_rate)
        last_pcm = _pcm_tail(path, start=False, sample_rate=sample_rate)
    except FfmpegError as exc:
        return {
            "id": "loop_audio",
            "pass": False,
            "mode": str(mode).lower(),
            "value": None,
            "limit": float(audio_jump_db),
            "error": str(exc),
        }
    first_db = round(_rms_db(first_pcm), 2)
    last_db = round(_rms_db(last_pcm), 2)
    jump = round(last_db - first_db, 2)
    abs_jump = abs(jump)
    step = round(_sample_step(first_pcm, last_pcm), 4)
    fade_in = round(_edge_fade_seconds(first_pcm, sample_rate, from_start=True), 4)
    fade_out = round(_edge_fade_seconds(last_pcm, sample_rate, from_start=False), 4)
    jump_ok = abs_jump <= float(audio_jump_db)
    step_ok = step <= float(sample_step)
    passed = jump_ok and step_ok
    detail_parts = []
    if not jump_ok:
        detail_parts.append(f"audio jump {jump:+.1f} dB (abs {abs_jump:.1f} > {audio_jump_db})")
    if not step_ok:
        detail_parts.append(f"sample step {step:.3f} > {sample_step} FS")
    return {
        "id": "loop_audio",
        "pass": passed,
        "mode": str(mode).lower(),
        "value": jump,
        "limit": float(audio_jump_db),
        "audio_jump_db": jump,
        "audio_jump_abs_db": abs_jump,
        "audio_first_db": first_db,
        "audio_last_db": last_db,
        "lufs_jump_db": jump,
        "sample_step": step,
        "sample_step_limit": float(sample_step),
        "edge_fade_in_seconds": fade_in,
        "edge_fade_out_seconds": fade_out,
        "detail": "; ".join(detail_parts) or None,
        "path": os.path.abspath(path),
    }


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
    video = check_loop_video(path, ssim_min=ssim_min, mode=mode)
    audio = check_loop_audio(path, audio_jump_db=audio_jump_db, mode=mode)
    if video.get("error") and not video.get("skipped"):
        return {
            "ok": False,
            "id": "loop_seam",
            "pass": False,
            "error": video.get("error"),
            "mode": str(mode).lower(),
        }
    ssim = video.get("ssim")
    audio_jump = audio.get("audio_jump_abs_db")
    first_db = audio.get("audio_first_db")
    last_db = audio.get("audio_last_db")
    video_ok = bool(video.get("skipped") or video.get("pass"))
    audio_ok = bool(audio.get("skipped") or audio.get("pass"))
    passed = video_ok and audio_ok
    detail_parts = [p for p in (video.get("detail"), audio.get("detail")) if p]
    return {
        "ok": True,
        "id": "loop_seam",
        "pass": passed,
        "mode": str(mode).lower(),
        "ssim": ssim,
        "ssim_min": float(ssim_min),
        "psnr": video.get("psnr"),
        "video_pass": video_ok,
        "audio_jump_db": audio_jump,
        "audio_jump_limit_db": float(audio_jump_db),
        "audio_first_db": first_db,
        "audio_last_db": last_db,
        "audio_pass": audio_ok,
        "sample_step": audio.get("sample_step"),
        "value": 0 if passed else 1,
        "limit": 0,
        "detail": "; ".join(detail_parts) or None,
        "path": os.path.abspath(path),
        "loop_video": video,
        "loop_audio": audio,
    }
