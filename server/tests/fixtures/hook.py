"""Synthetic hook fixtures: an approved Short stand-in vs a menu-open clip."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def approved_short(ffmpeg_bin: str, dest: Path, *, duration: float = 4.0) -> Path:
    """Vertical Short with motion, a title card, and a first-second loudness rise."""
    size = "320x568"
    vf = (
        "[0:v]drawbox=4:10:312:120:c=0x3a6ea5:t=fill,"
        "drawbox=16:28:288:28:c=white:t=fill[v];"
        "[1:a]volume=0.02[quiet];[2:a]adelay=200|200,volume=1.0[loud];"
        "[quiet][loud]amix=inputs=2:duration=first:normalize=0[a]"
    )
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={size}:rate=24",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=8000:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=8000:duration=1.2",
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-shortest",
            str(dest),
        ]
    )
    return dest


def menu_open_clip(ffmpeg_bin: str, dest: Path, *, duration: float = 4.0) -> Path:
    """Static pause-menu: dark frame, small center buttons, near-silent audio, no card."""
    size = "320x568"
    vf = (
        "drawbox=90:220:140:28:c=0x4a4a4a:t=fill,"
        "drawbox=90:260:140:28:c=0x3a3a3a:t=fill,"
        "drawbox=90:300:140:28:c=0x3a3a3a:t=fill"
    )
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x141414:s={size}:r=24:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=120:sample_rate=8000:duration={duration},volume=0.004",
            "-vf",
            vf,
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-shortest",
            str(dest),
        ]
    )
    return dest
