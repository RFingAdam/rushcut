"""PCM decode/encode helpers shared by audio ops, mastering and cleanup."""
from __future__ import annotations

import array
import math
import os
import tempfile
from typing import Any

from .ffmpeg_util import run, which_ffmpeg
from .probe import probe_media

PCM_DTYPE = "h"  # signed 16-bit


def db_to_gain(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def gain_to_db(gain: float) -> float:
    if gain <= 1e-12:
        return -120.0
    return 20.0 * math.log10(gain)


def rms_db(samples: array.array, start: int = 0, end: int | None = None) -> float:
    sl = samples[start:end]
    if not sl:
        return -120.0
    acc = 0.0
    for s in sl:
        acc += s * s
    mean = acc / len(sl)
    if mean <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean) / 32768.0)


def peak_abs(samples: array.array, start: int = 0, end: int | None = None) -> int:
    sl = samples[start:end]
    peak = 0
    for s in sl:
        a = abs(int(s))
        if a > peak:
            peak = a
    return peak


def raised_cosine(t: float) -> float:
    """0 at t=0, 1 at t=1. Smooth raised-cosine ramp."""
    t = min(1.0, max(0.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def decode_pcm(
    path: str,
    sample_rate: int = 48000,
    channels: int = 2,
) -> tuple[array.array, int, int]:
    """Decode a media file to interleaved signed 16-bit PCM."""
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                path,
                "-vn",
                "-ac",
                str(channels),
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                tmp.name,
            ],
            timeout=300,
        )
        with open(tmp.name, "rb") as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    samples = array.array(PCM_DTYPE)
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    return samples, sample_rate, channels


def encode_pcm(
    samples: array.array,
    output_path: str,
    sample_rate: int,
    channels: int,
    *,
    video_from: str | None = None,
    audio_codec: str | None = None,
) -> str:
    """Write interleaved s16le to ``output_path``, optionally copying video."""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    try:
        tmp.write(samples.tobytes())
        tmp.close()
        ext = os.path.splitext(output_path)[1].lower()
        if audio_codec is None:
            audio_codec = "pcm_s16le" if ext in {".wav", ".aif", ".aiff"} else "aac"
        cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
        if video_from and os.path.isfile(video_from):
            probe = probe_media(video_from)
            if probe.get("has_video"):
                cmd.extend(["-i", video_from])
                cmd.extend(
                    [
                        "-f",
                        "s16le",
                        "-ar",
                        str(sample_rate),
                        "-ac",
                        str(channels),
                        "-i",
                        tmp.name,
                    ]
                )
                cmd.extend(["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", audio_codec])
                if audio_codec == "aac":
                    cmd.extend(["-b:a", "192k"])
                cmd.append(output_path)
                run(cmd, timeout=300)
                return os.path.abspath(output_path)
        cmd.extend(
            [
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-i",
                tmp.name,
                "-c:a",
                audio_codec,
            ]
        )
        if audio_codec == "aac":
            cmd.extend(["-b:a", "192k"])
        cmd.append(output_path)
        run(cmd, timeout=300)
        return os.path.abspath(output_path)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def mux_audio_onto_video(
    video_path: str,
    audio_path: str,
    output_path: str,
    *,
    copy_video: bool = True,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> str:
    """Replace the audio of ``video_path`` while optionally copying video bytes."""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0?",
        "-map",
        "1:a:0",
        "-c:v",
        "copy" if copy_video else "libx264",
        "-c:a",
        audio_codec,
    ]
    if audio_codec == "aac":
        cmd.extend(["-b:a", audio_bitrate])
    if not copy_video:
        cmd.extend(["-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    cmd.append(output_path)
    run(cmd, timeout=300)
    return os.path.abspath(output_path)


def extract_audio_wav(path: str, dest: str, sample_rate: int = 48000, channels: int = 2) -> str:
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            path,
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            dest,
        ],
        timeout=300,
    )
    return dest


def ok_path(path: str, label: str = "path") -> dict[str, Any] | None:
    if not path:
        return {"ok": False, "error": f"{label} is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    return None


def refuse_output(output_path: str | None) -> dict[str, Any] | None:
    """Refuse writes outside #49's configured output roots."""
    from ..output_policy import check_output_path

    return check_output_path(output_path)


def default_output(path: str, suffix: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}.{suffix}{ext or '.wav'}"


def clamp_sample(value: float) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return int(round(value))


def mix_into(
    dest: array.array,
    src: array.array,
    dest_offset: int,
    gain: float = 1.0,
) -> None:
    n = min(len(src), max(0, len(dest) - dest_offset))
    for i in range(n):
        dest[dest_offset + i] = clamp_sample(dest[dest_offset + i] + src[i] * gain)


def apply_gain_region(
    samples: array.array,
    start: int,
    end: int,
    gain: float,
    *,
    ramp: int = 0,
    channels: int = 1,
) -> None:
    """Multiply [start, end) by ``gain``, with raised-cosine ramps on each side."""
    start = max(0, start)
    end = min(len(samples), end)
    ramp = max(0, int(ramp))
    # Align ramps to a frame boundary so stereo pairs stay balanced.
    ramp -= ramp % max(1, channels)
    fade_in_end = min(end, start + ramp)
    fade_out_start = max(start, end - ramp)
    for i in range(start, end):
        if ramp and i < fade_in_end:
            t = (i - start) / ramp
            g = 1.0 + (gain - 1.0) * raised_cosine(t)
        elif ramp and i >= fade_out_start:
            t = (end - i) / ramp
            g = 1.0 + (gain - 1.0) * raised_cosine(t)
        else:
            g = gain
        samples[i] = clamp_sample(samples[i] * g)
