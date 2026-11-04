"""ffmpeg / ffprobe process helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any

_tls = threading.local()

# Wall-clock cost of a libx264 encode, relative to 1080p30 medium.
# 14 min 1080p60 medium must land well above the old 300 s cap.
_PRESET_COST = {
    "ultrafast": 0.4,
    "superfast": 0.5,
    "veryfast": 0.7,
    "faster": 0.9,
    "fast": 1.2,
    "medium": 2.0,
    "slow": 3.5,
    "slower": 5.0,
    "veryslow": 8.0,
}
_REF_PIXEL_RATE = 1920 * 1080 * 30.0


class FfmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def which_ffmpeg(name: str = "ffmpeg") -> str:
    override = os.environ.get(name.upper() + "_BIN") or os.environ.get(name.upper())
    if override and os.path.isfile(override):
        return override
    found = shutil.which(name)
    if not found:
        raise FfmpegError(
            f"{name} is not on PATH. Install ffmpeg (includes ffprobe) and retry."
        )
    return found


def bind_job_process(job: Any | None) -> None:
    """Route :func:`run` through a tracked :class:`JobProcess` on this thread."""
    _tls.job = job


def spawn(args: list[str], *, text: bool = True) -> subprocess.Popen[Any]:
    """Start ffmpeg/ffprobe without waiting. Used so a render can be killed."""
    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
    except FileNotFoundError as exc:
        raise FfmpegError(f"Executable not found: {args[0]}") from exc


def run(
    args: list[str],
    *,
    timeout: float = 120,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    job = getattr(_tls, "job", None)
    if job is not None and capture and text:
        return job.run(args, timeout=timeout, check=check)
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=capture,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"Timed out after {timeout}s: {' '.join(args[:4])}") from exc
    except FileNotFoundError as exc:
        raise FfmpegError(f"Executable not found: {args[0]}") from exc
    if check and proc.returncode != 0:
        err_raw = proc.stderr or proc.stdout or ""
        err = err_raw.decode("utf-8", "replace") if isinstance(err_raw, (bytes, bytearray)) else str(err_raw)
        err = err.strip()
        raise FfmpegError(
            f"{args[0]} exited {proc.returncode}: {err[:800]}",
            stderr=err,
        )
    return proc


def encode_timeout_seconds(
    duration_seconds: float | None,
    *,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    preset: str | None = "medium",
    copy: bool = False,
    override: float | None = None,
) -> float:
    """Default ffmpeg timeout, scaled to program length and format.

    There is no fixed 300 s cap. A 14 min 1080p60 medium encode gets a
    long-form budget; stream-copy only needs mux time.
    """
    if override is not None:
        return max(1.0, float(override))
    duration = max(0.0, float(duration_seconds or 0.0))
    if copy:
        return max(60.0, duration * 0.15 + 30.0)
    megapixels = max(1, int(width or 1920)) * max(1, int(height or 1080))
    rate = float(fps or 30.0) or 30.0
    pixel_scale = max(0.25, (megapixels * rate) / _REF_PIXEL_RATE)
    cost = _PRESET_COST.get(str(preset or "medium").lower(), 2.0)
    return max(90.0, duration * pixel_scale * cost + 60.0)


def decode_timeout_seconds(duration_seconds: float | None, override: float | None = None) -> float:
    """Full-decode QA timeout, scaled to program length."""
    if override is not None:
        return max(1.0, float(override))
    duration = max(0.0, float(duration_seconds or 0.0))
    return max(180.0, duration * 0.5 + 30.0)


def ffprobe_json(path: str, extra: list[str] | None = None) -> dict[str, Any]:
    cmd = [
        which_ffmpeg("ffprobe"),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if extra:
        cmd.extend(extra)
    cmd.append(path)
    proc = run(cmd, timeout=60)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FfmpegError("ffprobe returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise FfmpegError("ffprobe JSON was not an object")
    return data
