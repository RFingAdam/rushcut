"""Frame-pipe helpers. Decode/encode one frame at a time — no full-clip buffer."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, which_ffmpeg
from ..analysis.probe import probe_media


def extract_frame(path: str, seconds: float, dest: str | None = None) -> dict[str, Any]:
    """Write one PNG (or return raw RGB) at *seconds*."""
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if dest:
        cmd = [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-ss",
            f"{max(0.0, seconds):.6f}",
            "-i",
            path,
            "-frames:v",
            "1",
            dest,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.isfile(dest):
            return {"ok": False, "error": (proc.stderr or "extract_frame failed")[-400:]}
        return {"ok": True, "path": os.path.abspath(dest), "t": float(seconds)}
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-ss",
        f"{max(0.0, seconds):.6f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else "extract_frame failed"
        return {"ok": False, "error": err[-400:]}
    return {
        "ok": True,
        "rgb": proc.stdout,
        "width": width,
        "height": height,
        "t": float(seconds),
    }


def iter_rgb_frames(
    path: str,
    *,
    start: float = 0.0,
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> Iterator[tuple[int, bytes, int, int]]:
    """Yield (index, rgb24, w, h) without keeping the clip in RAM."""
    probe = probe_media(path)
    if not probe.get("ok"):
        raise FfmpegError(str(probe.get("error") or "probe failed"))
    w = int(width or probe.get("width") or 0)
    h = int(height or probe.get("height") or 0)
    if w < 2 or h < 2:
        raise FfmpegError("video size is required")
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start):.6f}",
        "-i",
        path,
    ]
    if duration is not None:
        cmd.extend(["-t", f"{float(duration):.6f}"])
    cmd.extend(["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    frame_size = w * h * 3
    index = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if not buf or len(buf) < frame_size:
                break
            yield index, buf, w, h
            index += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=5)


def encode_rgb_frames(
    frames: Iterator[bytes],
    dest: str,
    width: int,
    height: int,
    fps: float,
    audio_path: str | None = None,
    audio_start: float = 0.0,
    audio_duration: float | None = None,
) -> dict[str, Any]:
    """Encode a stream of rgb24 frames. Only one frame is held at a time."""
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{fps:.6f}",
        "-i",
        "pipe:0",
    ]
    if audio_path:
        cmd.extend(["-ss", f"{audio_start:.6f}", "-i", audio_path])
        if audio_duration is not None:
            cmd.extend(["-t", f"{audio_duration:.6f}"])
    cmd.extend(
        [
            "-map",
            "0:v",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if audio_path:
        cmd.extend(["-map", "1:a?", "-c:a", "aac", "-shortest"])
    else:
        cmd.append("-an")
    cmd.append(dest)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for frame in frames:
            proc.stdin.write(frame)
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait(timeout=180)
    except Exception:
        proc.kill()
        raise
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace") if stderr else "encode failed"
        return {"ok": False, "error": err[-400:]}
    return {"ok": True, "output_path": os.path.abspath(dest)}


def mean_luma(rgb: bytes, width: int, height: int, box: tuple[int, int, int, int] | None = None) -> float:
    if not rgb:
        return 0.0
    if box is None:
        total = 0
        n = len(rgb) // 3
        for i in range(0, n * 3, 3):
            total += (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000
        return total / max(1, n)
    x, y, w, h = box
    total = 0
    n = 0
    for row in range(max(0, y), min(height, y + h)):
        row_off = row * width * 3
        for col in range(max(0, x), min(width, x + w)):
            i = row_off + col * 3
            total += (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000
            n += 1
    return total / max(1, n)


def laplacian_var(rgb: bytes, width: int, height: int) -> float:
    """Variance of a 4-neighbour Laplacian on luma. Pure Python fallback."""
    if width < 3 or height < 3:
        return 0.0
    luma = bytearray(width * height)
    for i, dst in enumerate(range(0, width * height)):
        src = i * 3
        luma[dst] = (rgb[src] * 299 + rgb[src + 1] * 587 + rgb[src + 2] * 114) // 1000
    acc = 0.0
    acc2 = 0.0
    n = 0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            c = luma[row + x]
            lap = (
                luma[row + x - 1]
                + luma[row + x + 1]
                + luma[row - width + x]
                + luma[row + width + x]
                - 4 * c
            )
            acc += lap
            acc2 += lap * lap
            n += 1
    if n <= 1:
        return 0.0
    mean = acc / n
    return max(0.0, acc2 / n - mean * mean)
