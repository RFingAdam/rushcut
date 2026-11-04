"""File-level audio ops: music bed, bleeps, VO insert/replace, loop fades.

Each function works on media files without an NLE. ``apply_timeline`` in
``rushcut.edit.timeline`` exposes the same ops as timeline effects.
"""
from __future__ import annotations

import array
import math
import os
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .pcm import (
    apply_gain_region,
    clamp_sample,
    db_to_gain,
    decode_pcm,
    default_output,
    encode_pcm,
    mix_into,
    ok_path,
    raised_cosine,
    refuse_output,
    rms_db,
)
from .probe import probe_media

_SPAN_START = ("start", "start_seconds", "in_seconds")
_SPAN_END = ("end", "end_seconds", "out_seconds")


def _span_times(span: dict[str, Any] | list | tuple) -> tuple[float, float]:
    if isinstance(span, dict):
        start = 0.0
        end = 0.0
        for key in _SPAN_START:
            if span.get(key) is not None:
                start = float(span[key])
                break
        for key in _SPAN_END:
            if span.get(key) is not None:
                end = float(span[key])
                break
        return start, end
    return float(span[0]), float(span[1])


def _voice_activity(
    samples: array.array,
    sample_rate: int,
    channels: int,
    *,
    window: float = 0.02,
    threshold_db: float = -40.0,
) -> list[tuple[int, int]]:
    """Return sample-index ranges (inclusive start, exclusive end) that look like voice."""
    hop = max(channels, int(sample_rate * window) * channels)
    active: list[tuple[int, int]] = []
    n = len(samples)
    i = 0
    while i < n:
        j = min(n, i + hop)
        if rms_db(samples, i, j) >= threshold_db:
            if active and i - active[-1][1] <= hop:
                active[-1] = (active[-1][0], j)
            else:
                active.append((i, j))
        i = j
    return active


def add_music_bed(
    path: str,
    onto: str | None = None,
    output_path: str | None = None,
    gain: float = 1.0,
    duck_under: str | None = None,
    duck_db: float = -12.0,
    threshold: float = 0.02,
    ratio: float = 8.0,
    attack: float = 20.0,
    release: float = 200.0,
    fade_in: float = 0.5,
    fade_out: float = 0.5,
) -> dict[str, Any]:
    """Mix a music bed under a program (or write the bed alone) with ducking.

    ``duck_under`` is a voice (or SFX) sidechain. Ducking uses a raised-cosine
    envelope whose depth is ``duck_db``; ``threshold`` / ``ratio`` / ``attack``
    / ``release`` shape the sidechain (ffmpeg ``sidechaincompress`` when
    available, plus a deterministic envelope so depth is measurable).
    """
    err = ok_path(path, "path")
    if err:
        return err
    if onto:
        err = ok_path(onto, "onto")
        if err:
            return err
    sidechain = duck_under or onto
    if not output_path:
        output_path = default_output(onto or path, "bed")
    refused = refuse_output(output_path)
    if refused:
        return refused
    try:
        bed, sr, ch = decode_pcm(path)
        if onto:
            program, sr_p, ch_p = decode_pcm(onto, sample_rate=sr, channels=ch)
            sr, ch = sr_p, ch_p
        else:
            program = array.array("h", [0] * len(bed))
        if len(bed) < len(program):
            bed.extend([0] * (len(program) - len(bed)))
        elif len(bed) > len(program):
            program.extend([0] * (len(bed) - len(program)))
        voice = None
        if sidechain:
            voice, _, _ = decode_pcm(sidechain, sample_rate=sr, channels=ch)
            if len(voice) < len(bed):
                voice.extend([0] * (len(bed) - len(voice)))
        g = float(gain)
        fade_in_n = int(max(0.0, fade_in) * sr) * ch
        fade_out_n = int(max(0.0, fade_out) * sr) * ch
        for i, s in enumerate(bed):
            env = g
            if fade_in_n and i < fade_in_n:
                env *= raised_cosine(i / fade_in_n)
            if fade_out_n and i >= len(bed) - fade_out_n:
                env *= raised_cosine((len(bed) - i) / fade_out_n)
            bed[i] = clamp_sample(s * env)
        duck_gain = db_to_gain(duck_db) if duck_db <= 0 else db_to_gain(-abs(duck_db))
        attack_n = max(ch, int((max(1.0, attack) / 1000.0) * sr) * ch)
        release_n = max(ch, int((max(1.0, release) / 1000.0) * sr) * ch)
        if voice is not None:
            # Sidechain: duck wherever the voice/SFX is above threshold.
            # threshold is linear 0..1 of full scale; also honor duck_db depth.
            thresh_db = 20.0 * math.log10(max(threshold, 1e-6))
            regions = _voice_activity(voice, sr, ch, threshold_db=thresh_db)
            for start, end in regions:
                apply_gain_region(
                    bed,
                    start,
                    end,
                    duck_gain,
                    ramp=max(attack_n, release_n // 4),
                    channels=ch,
                )
            # ffmpeg sidechaincompress — extra depth on transients, same files.
            _try_sidechain_file(bed, voice, sr, ch, threshold, ratio, attack, release, duck_gain)
        mixed = array.array("h", program)
        mix_into(mixed, bed, 0, 1.0)
        video_from = onto if onto and probe_media(onto).get("has_video") else None
        written = encode_pcm(mixed, output_path, sr, ch, video_from=video_from)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": written,
        "gain": g,
        "duck_db": float(duck_db),
        "duck_gain": duck_gain,
        "threshold": threshold,
        "ratio": ratio,
        "attack": attack,
        "release": release,
        "fade_in": fade_in,
        "fade_out": fade_out,
    }


def _try_sidechain_file(
    bed: array.array,
    voice: array.array,
    sr: int,
    ch: int,
    threshold: float,
    ratio: float,
    attack: float,
    release: float,
    duck_gain: float,
) -> None:
    """Optional extra pass through ffmpeg sidechaincompress. Best-effort."""
    tmp_bed = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_sc = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_bed.close()
    tmp_sc.close()
    tmp_out.close()
    try:
        encode_pcm(bed, tmp_bed.name, sr, ch, audio_codec="pcm_s16le")
        encode_pcm(voice, tmp_sc.name, sr, ch, audio_codec="pcm_s16le")
        # makeup stays 1; ratio/threshold come from the caller. The
        # envelope pass already applied duck_db; this tightens transients.
        filt = (
            f"[0:a][1:a]sidechaincompress=threshold={threshold}:ratio={ratio}:"
            f"attack={attack}:release={release}:level_sc=1:makeup=1[a]"
        )
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                tmp_bed.name,
                "-i",
                tmp_sc.name,
                "-filter_complex",
                filt,
                "-map",
                "[a]",
                tmp_out.name,
            ],
            timeout=180,
        )
        ducked, _, _ = decode_pcm(tmp_out.name, sample_rate=sr, channels=ch)
        n = min(len(bed), len(ducked))
        # Keep whichever is quieter in each frame so measured depth is >= duck_db.
        for i in range(n):
            if abs(int(ducked[i])) < abs(int(bed[i])):
                bed[i] = ducked[i]
    except (FfmpegError, OSError):
        return
    finally:
        for p in (tmp_bed.name, tmp_sc.name, tmp_out.name):
            try:
                os.remove(p)
            except OSError:
                pass
        del duck_gain  # depth is already on `bed` from the envelope pass


def bleep_spans(
    path: str,
    spans: list[dict[str, Any]] | None,
    output_path: str | None = None,
    mode: str = "tone",
    freq: float = 1000.0,
) -> dict[str, Any]:
    """Replace listed time spans with a tone, silence, or reversed audio.

    ``spans`` accepts ``generate_captions`` ``masked_spans``
    (``{start, end, word}``) directly. Samples outside the spans are
    left unchanged.
    """
    err = ok_path(path)
    if err:
        return err
    if not spans:
        return {"ok": False, "error": "spans is required"}
    if not output_path:
        output_path = default_output(path, "bleep")
    refused = refuse_output(output_path)
    if refused:
        return refused
    mode_l = (mode or "tone").lower()
    if mode_l not in {"tone", "mute", "reverse"}:
        return {"ok": False, "error": f"mode must be tone|mute|reverse, got {mode!r}"}
    try:
        samples, sr, ch = decode_pcm(path)
        n = len(samples)
        applied: list[dict[str, Any]] = []
        for span in spans:
            start_s, end_s = _span_times(span)
            if end_s <= start_s:
                continue
            a = max(0, int(round(start_s * sr)) * ch)
            b = min(n, int(round(end_s * sr)) * ch)
            if b <= a:
                continue
            if mode_l == "mute":
                for i in range(a, b):
                    samples[i] = 0
            elif mode_l == "reverse":
                region = samples[a:b]
                region.reverse()
                samples[a:b] = region
            else:
                amp = 0.6 * 32767.0
                for i in range(a, b):
                    frame = (i // ch) / sr
                    samples[i] = clamp_sample(amp * math.sin(2 * math.pi * freq * frame))
            applied.append({"start": start_s, "end": end_s, "mode": mode_l})
        video_from = path if probe_media(path).get("has_video") else None
        written = encode_pcm(samples, output_path, sr, ch, video_from=video_from)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": written,
        "mode": mode_l,
        "freq": float(freq) if mode_l == "tone" else None,
        "spans": applied,
    }


def insert_audio(
    path: str,
    clip: str,
    at: float,
    output_path: str | None = None,
    duck_db: float = -6.0,
    ramp_ms: float = 200.0,
    ramp_shape: str = "raised_cosine",
) -> dict[str, Any]:
    """Drop a voice line into an existing mix and duck the bed around it.

    Report: ``vo_start``, ``vo_end``, ``duck_db``, ramp windows.
    """
    err = ok_path(path)
    if err:
        return err
    err = ok_path(clip, "clip")
    if err:
        return err
    if not output_path:
        output_path = default_output(path, "insert")
    refused = refuse_output(output_path)
    if refused:
        return refused
    shape = (ramp_shape or "raised_cosine").lower()
    if shape not in {"raised_cosine", "linear", "cosine"}:
        return {"ok": False, "error": f"unknown ramp_shape {ramp_shape!r}"}
    try:
        bed, sr, ch = decode_pcm(path)
        vo, _, _ = decode_pcm(clip, sample_rate=sr, channels=ch)
        vo_start = max(0.0, float(at))
        vo_len = len(vo) / (sr * ch)
        vo_end = vo_start + vo_len
        need = int(math.ceil(vo_end * sr)) * ch
        if need > len(bed):
            bed.extend([0] * (need - len(bed)))
        duck_gain = db_to_gain(duck_db) if duck_db <= 0 else db_to_gain(-abs(duck_db))
        ramp_s = max(0.0, float(ramp_ms) / 1000.0)
        ramp_n = int(ramp_s * sr) * ch
        duck_start = max(0, int(round((vo_start - ramp_s) * sr)) * ch)
        duck_end = min(len(bed), int(round((vo_end + ramp_s) * sr)) * ch)
        apply_gain_region(
            bed,
            duck_start,
            duck_end,
            duck_gain,
            ramp=ramp_n,
            channels=ch,
            shape=shape,
        )
        offset = int(round(vo_start * sr)) * ch
        mix_into(bed, vo, offset, 1.0)
        video_from = path if probe_media(path).get("has_video") else None
        written = encode_pcm(bed, output_path, sr, ch, video_from=video_from)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    ramp_in = {"start": max(0.0, vo_start - ramp_s), "end": vo_start}
    ramp_out = {"start": vo_end, "end": vo_end + ramp_s}
    return {
        "ok": True,
        "output_path": written,
        "vo_start": vo_start,
        "vo_end": vo_end,
        "duck_db": float(duck_db) if duck_db <= 0 else -abs(float(duck_db)),
        "ramp_ms": float(ramp_ms),
        "ramp_shape": shape,
        "ramp_windows": {"in": ramp_in, "out": ramp_out},
        "duck_window": {"start": ramp_in["start"], "end": ramp_out["end"]},
        "clip": os.path.abspath(clip),
    }


def _dot(a: array.array, a0: int, b: array.array, n: int) -> float:
    acc = 0.0
    for i in range(n):
        acc += a[a0 + i] * b[i]
    return acc


def _align_clip(
    haystack: array.array,
    needle: array.array,
    center: int,
    radius: int,
) -> int:
    """Search ±radius samples around ``center`` for the best alignment."""
    n = len(needle)
    if n == 0 or not haystack:
        return center
    best_off = center
    best = float("-inf")
    lo = max(0, center - radius)
    hi = min(len(haystack) - n, center + radius)
    energy = _dot(needle, 0, needle, n) or 1.0
    for off in range(lo, hi + 1):
        score = _dot(haystack, off, needle, n) / energy
        if score > best:
            best = score
            best_off = off
    return best_off


def replace_audio_span(
    path: str,
    old_clip: str,
    new_clip: str | None = None,
    output_path: str | None = None,
    at: float | None = None,
    search_samples: int = 64,
) -> dict[str, Any]:
    """Remove a previously inserted line and optionally insert a new one.

    Alignment search is ±``search_samples`` (a few samples) around ``at``,
    or a wider window when ``at`` is omitted.
    """
    err = ok_path(path)
    if err:
        return err
    err = ok_path(old_clip, "old_clip")
    if err:
        return err
    if new_clip:
        err = ok_path(new_clip, "new_clip")
        if err:
            return err
    if not output_path:
        output_path = default_output(path, "replace")
    refused = refuse_output(output_path)
    if refused:
        return refused
    try:
        mix, sr, ch = decode_pcm(path)
        old, _, _ = decode_pcm(old_clip, sample_rate=sr, channels=ch)
        if at is None:
            # Coarse hop, then refine. Enough for short VO lines.
            hop = max(ch, len(old) // 4 or ch)
            center = 0
            best = float("-inf")
            energy = _dot(old, 0, old, len(old)) or 1.0
            limit = max(0, len(mix) - len(old))
            for off in range(0, limit + 1, hop):
                score = _dot(mix, off, old, len(old)) / energy
                if score > best:
                    best = score
                    center = off
            radius = max(search_samples, hop)
        else:
            center = int(round(float(at) * sr)) * ch
            radius = max(1, int(search_samples)) * ch
        found = _align_clip(mix, old, center, radius)
        # Subtract the old clip (it was mixed on top of a ducked bed).
        n = min(len(old), len(mix) - found)
        for i in range(n):
            mix[found + i] = clamp_sample(mix[found + i] - old[i])
        residual = rms_db(old, 0, n)
        leftover = array.array("h", [0] * n)
        # leftover energy of `old` still in mix: correlate remaining
        leftover_energy = 0.0
        old_energy = 0.0
        for i in range(n):
            leftover_energy += mix[found + i] * old[i]
            old_energy += old[i] * old[i]
        # After subtraction the projection of `old` onto mix should be ~0.
        # Residual dB relative to the removed clip:
        proj = leftover_energy / (old_energy or 1.0)
        for i in range(n):
            leftover[i] = clamp_sample(old[i] * proj)
        residual_rel_db = rms_db(leftover) - residual if residual > -119 else -120.0
        new_at = found / (sr * ch)
        if new_clip:
            fresh, _, _ = decode_pcm(new_clip, sample_rate=sr, channels=ch)
            if found + len(fresh) > len(mix):
                mix.extend([0] * (found + len(fresh) - len(mix)))
            mix_into(mix, fresh, found, 1.0)
        video_from = path if probe_media(path).get("has_video") else None
        written = encode_pcm(mix, output_path, sr, ch, video_from=video_from)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": written,
        "found_at": new_at,
        "search_samples": search_samples,
        "residual_db": residual_rel_db,
        "removed_samples": n,
    }


def loop_fades(
    path: str,
    in_s: float = 0.2,
    out_s: float = 0.2,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Optional matching fades at both ends (silence + black).

    First ``in_s`` seconds fade up from silence and black; last ``out_s``
    seconds fade down to silence and black. This is an explicit look, not
    a loop fix: seamless loops must *not* get a forced fade (last frame
    has to match frame 1). ``check_loop`` fails fade-to-black / fade-to-
    silence. Do not call this to make a Short wrap.
    """
    err = ok_path(path)
    if err:
        return err
    if not output_path:
        output_path = default_output(path, "loop")
    refused = refuse_output(output_path)
    if refused:
        return refused
    probe = probe_media(path)
    duration = float(probe.get("duration_seconds") or 0)
    fade_in = max(0.0, float(in_s))
    fade_out = max(0.0, float(out_s))
    if duration > 0:
        fade_in = min(fade_in, duration / 2.0)
        fade_out = min(fade_out, duration / 2.0)
    vf: list[str] = []
    af: list[str] = []
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in:.4f}:color=black")
        af.append(f"afade=t=in:st=0:d={fade_in:.4f}")
    if fade_out > 0:
        start = max(0.0, duration - fade_out)
        vf.append(f"fade=t=out:st={start:.4f}:d={fade_out:.4f}:color=black")
        af.append(f"afade=t=out:st={start:.4f}:d={fade_out:.4f}")
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", path]
    if probe.get("has_video") and vf:
        cmd.extend(["-vf", ",".join(vf)])
    elif probe.get("has_video"):
        cmd.extend(["-c:v", "copy"])
    if probe.get("has_audio") and af:
        cmd.extend(["-af", ",".join(af)])
    elif probe.get("has_audio"):
        cmd.extend(["-c:a", "copy"])
    if probe.get("has_video") and vf:
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    if probe.get("has_audio") and af:
        cmd.extend(["-c:a", "aac"])
    cmd.append(output_path)
    try:
        run(cmd, timeout=180)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "in_s": fade_in,
        "out_s": fade_out,
        "duration_seconds": duration,
        "note": (
            "First and last in_s/out_s fade to/from silence and black. "
            "Optional look only — do not apply to looping Shorts; "
            "check_loop fails a forced fade (last frame must match frame 1)."
        ),
        "breaks_loop": True,
    }
