"""ffmpeg / ffprobe process helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


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


def run(
    args: list[str],
    *,
    timeout: float = 120,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"Timed out after {timeout}s: {' '.join(args[:4])}") from exc
    except FileNotFoundError as exc:
        raise FfmpegError(f"Executable not found: {args[0]}") from exc
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise FfmpegError(
            f"{args[0]} exited {proc.returncode}: {err[:800]}",
            stderr=err,
        )
    return proc


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
