"""Integrated loudness (EBU R128) and two-pass loudnorm mastering.

``measure_loudness`` is a read-only ebur128 pass. ``master_audio`` measures,
applies linear loudnorm with the measured values, brick-wall limits, then
optionally runs an encode-aware true-peak loop (AAC 192k → decode → 4×
oversampled TP). The two-pass + limiter path is pure ffmpeg; the
encode-aware TP pass can use the ``analysis`` extra (numpy) for local
gain reduction and falls back to ffmpeg-only windowed reduction.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .pcm import default_output, mux_audio_onto_video, ok_path, refuse_output
from .probe import probe_media

_I_RE = re.compile(r"\bI:\s*([+-]?\d+(?:\.\d+)?)\s*LUFS", re.IGNORECASE)
_TP_RE = re.compile(
    r"True peak:\s*([+-]?\d+(?:\.\d+)?)\s*dB(?:TP|FS)?",
    re.IGNORECASE,
)
_PEAK_RE = re.compile(r"\bPeak:\s*([+-]?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)
_LRA_RE = re.compile(r"\bLRA:\s*([+-]?\d+(?:\.\d+)?)\s*LU", re.IGNORECASE)
_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_ASTATS_PEAK = re.compile(
    r"Peak level dB:\s*([+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

DEFAULT_I = -14.0
DEFAULT_TP = -1.5
DEFAULT_LRA = 11.0
AAC_BITRATE = "192k"
TP_SLACK_DB = 0.4


def _parse_ebur128(text: str) -> dict[str, float | None]:
    summary = text
    idx = text.lower().rfind("summary:")
    if idx >= 0:
        summary = text[idx:]
    integrated = None
    match_i = _I_RE.search(summary) or _I_RE.search(text)
    if match_i:
        integrated = float(match_i.group(1))
    true_peak = None
    match_tp = _TP_RE.search(summary) or _TP_RE.search(text)
    if match_tp:
        true_peak = float(match_tp.group(1))
    else:
        match_peak = _PEAK_RE.search(summary) or _PEAK_RE.search(text)
        if match_peak:
            true_peak = float(match_peak.group(1))
    lra = None
    match_lra = _LRA_RE.search(summary) or _LRA_RE.search(text)
    if match_lra:
        lra = float(match_lra.group(1))
    return {"integrated_lufs": integrated, "true_peak_dbtp": true_peak, "lra": lra}


def measure_loudness(path: str) -> dict[str, Any]:
    """Return integrated LUFS, true peak and LRA for a media file."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_audio"):
        return {
            "ok": True,
            "path": os.path.abspath(path),
            "has_audio": False,
            "integrated_lufs": None,
            "true_peak_dbtp": None,
            "lra": None,
            "note": "no audio stream",
        }
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-nostats",
                "-i",
                path,
                "-filter_complex",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            timeout=600,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc), "path": os.path.abspath(path)}
    text = proc.stderr or proc.stdout or ""
    parsed = _parse_ebur128(text)
    if parsed["integrated_lufs"] is None:
        return {"ok": False, "error": "ebur128 did not report integrated loudness", "stderr": text[-800:]}
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "has_audio": True,
        "integrated_lufs": round(float(parsed["integrated_lufs"]), 3),
        "true_peak_dbtp": (
            round(float(parsed["true_peak_dbtp"]), 3) if parsed["true_peak_dbtp"] is not None else None
        ),
        "lra": round(float(parsed["lra"]), 3) if parsed["lra"] is not None else None,
    }


def _loudnorm_measure(path: str, target_i: float, target_tp: float, target_lra: float) -> dict[str, Any]:
    filt = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    proc = run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            filt,
            "-f",
            "null",
            "-",
        ],
        timeout=600,
    )
    text = proc.stderr or proc.stdout or ""
    match = _JSON_RE.search(text)
    if not match:
        # Some builds pretty-print across more braces; take the last JSON object.
        last = text.rfind("{")
        if last >= 0:
            try:
                return json.loads(text[last:])
            except json.JSONDecodeError:
                pass
        raise FfmpegError("loudnorm did not print a JSON measurement", stderr=text[-800:])
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise FfmpegError("loudnorm JSON was invalid") from exc


def _limiter_ceiling(target_tp: float) -> float:
    return 10.0 ** (float(target_tp) / 20.0)


def _apply_loudnorm(
    path: str,
    dest: str,
    target_i: float,
    target_tp: float,
    target_lra: float,
    measured: dict[str, Any],
) -> None:
    def _num(key: str, fallback: float) -> str:
        raw = measured.get(key)
        if raw in (None, "", "nan"):
            return f"{fallback:.2f}"
        return str(raw)

    filt = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:"
        f"measured_I={_num('input_i', target_i)}:"
        f"measured_TP={_num('input_tp', target_tp)}:"
        f"measured_LRA={_num('input_lra', target_lra)}:"
        f"measured_thresh={_num('input_thresh', target_i - 10)}:"
        f"offset={_num('target_offset', 0.0)}:"
        f"linear=true:print_format=summary,"
        f"alimiter=limit={_limiter_ceiling(target_tp):.6f}:attack=7:release=50:level=false"
    )
    run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            path,
            "-vn",
            "-af",
            filt,
            dest,
        ],
        timeout=600,
    )


def measure_true_peak_4x(path: str) -> dict[str, Any]:
    """4×-oversampled true peak. numpy when the analysis extra is present.

    The ffmpeg path resamples to 4× the source rate (capped at 192 kHz) and
    reads astats / ebur128. Documented: encode-aware TP may use ``.[analysis]``.
    """
    try:
        import numpy as np  # type: ignore

        from .pcm import decode_pcm

        samples, sr, ch = decode_pcm(path, sample_rate=48000, channels=2)
        arr = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64) / 32768.0
        if ch > 1:
            arr = arr.reshape(-1, ch)
        # Linear 4× upsample per channel.
        up = []
        for c in range(arr.shape[1] if arr.ndim > 1 else 1):
            col = arr[:, c] if arr.ndim > 1 else arr
            n = col.size
            x = np.arange(n)
            xi = np.linspace(0, n - 1, n * 4)
            up.append(np.interp(xi, x, col))
        stacked = np.stack(up, axis=0)
        peak = float(np.max(np.abs(stacked)))
        tp = -120.0 if peak <= 0 else 20.0 * float(np.log10(peak))
        return {"ok": True, "true_peak_dbtp": round(tp, 3), "method": "numpy_4x", "used_numpy": True}
    except ImportError:
        pass
    except Exception:
        pass
    # ffmpeg: 4× oversample via aresample (48 kHz → 192 kHz) + astats / ebur128.
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-nostats",
                "-i",
                path,
                "-af",
                "aresample=192000,astats=measure_perchannel=0,ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            timeout=600,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    text = proc.stderr or proc.stdout or ""
    parsed = _parse_ebur128(text)
    tp = parsed["true_peak_dbtp"]
    if tp is None:
        match = _ASTATS_PEAK.search(text)
        if match:
            tp = float(match.group(1))
    if tp is None:
        return {"ok": False, "error": "could not read 4x true peak", "stderr": text[-600:]}
    return {"ok": True, "true_peak_dbtp": round(float(tp), 3), "method": "ffmpeg_4x", "used_numpy": False}


def _local_tp_reduce(path: str, dest: str, target_tp: float, overshoot_db: float) -> None:
    """Apply local (windowed) gain reduction where 4× TP exceeds target."""
    extra = max(0.15, overshoot_db + 0.1)
    try:
        import numpy as np  # type: ignore

        from .pcm import decode_pcm, encode_pcm, apply_gain_region, db_to_gain

        samples, sr, ch = decode_pcm(path, sample_rate=48000, channels=2)
        arr = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64) / 32768.0
        if ch > 1:
            frames = arr.reshape(-1, ch)
        else:
            frames = arr.reshape(-1, 1)
        hop = max(1, int(sr * 0.02))
        gain = db_to_gain(-extra)
        for i in range(0, frames.shape[0], hop):
            sl = frames[i : i + hop]
            # 4× linear upsample of this window.
            peak = 0.0
            for c in range(sl.shape[1]):
                col = sl[:, c]
                if col.size < 2:
                    peak = max(peak, float(np.max(np.abs(col))) if col.size else 0.0)
                    continue
                x = np.arange(col.size)
                xi = np.linspace(0, col.size - 1, col.size * 4)
                up = np.interp(xi, x, col)
                peak = max(peak, float(np.max(np.abs(up))))
            tp = -120.0 if peak <= 0 else 20.0 * float(np.log10(peak))
            if tp > target_tp:
                a = i * ch
                b = min(len(samples), (i + hop) * ch)
                apply_gain_region(samples, a, b, gain, ramp=int(0.005 * sr) * ch, channels=ch)
        encode_pcm(samples, dest, sr, ch, audio_codec="pcm_s16le")
        return
    except ImportError:
        pass
    except Exception:
        pass
    # ffmpeg-only: another limiter pass a bit under the target, plus a
    # small global pull so the next encode-aware iteration can pass.
    ceiling = 10.0 ** ((target_tp - extra) / 20.0)
    vol = 10.0 ** ((-extra * 0.5) / 20.0)
    run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            path,
            "-af",
            f"volume={vol:.6f},alimiter=limit={ceiling:.6f}:attack=5:release=40:level=false",
            dest,
        ],
        timeout=600,
    )


def _encode_aac(src: str, dest: str) -> None:
    run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            src,
            "-c:a",
            "aac",
            "-b:a",
            AAC_BITRATE,
            dest,
        ],
        timeout=600,
    )


def _video_md5(path: str) -> str | None:
    """Hash of the copied video bitstream (not the container)."""
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-i",
                path,
                "-an",
                "-c:v",
                "copy",
                "-f",
                "md5",
                "-",
            ],
            timeout=180,
        )
    except FfmpegError:
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"MD5=([0-9a-fA-F]+)", text)
    return match.group(1).lower() if match else None


def master_audio(
    path: str,
    output_path: str | None = None,
    target_i: float = DEFAULT_I,
    target_tp: float = DEFAULT_TP,
    target_lra: float = DEFAULT_LRA,
    encode_aware: bool = True,
    max_iterations: int = 6,
) -> dict[str, Any]:
    """Two-pass loudnorm + limiter + optional encode-aware true-peak loop.

    Accepts audio or an MP4. Video is copied byte-identical (``-c:v copy``);
    only the audio stream is rewritten. Default target: -14 LUFS / -1.5 dBTP
    / LRA 11. The encode-aware pass encodes AAC 192k, decodes, measures 4×
    oversampled true peak, and applies local gain reduction until
    ``TP <= target + 0.4 dB``.
    """
    err = ok_path(path)
    if err:
        return err
    if not output_path:
        output_path = default_output(path, "master")
    refused = refuse_output(output_path)
    if refused:
        return refused
    probe = probe_media(path)
    if not probe.get("has_audio"):
        return {"ok": False, "error": "no audio stream to master", "path": path}
    has_video = bool(probe.get("has_video"))
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    incoming = measure_loudness(path)
    if not incoming.get("ok"):
        return incoming

    from ..output_policy import make_work_dir

    tmpdir = make_work_dir(prefix="rushcut-master-")
    measured_wav = os.path.join(tmpdir, "norm.wav")
    aac_path = os.path.join(tmpdir, "probe.m4a")
    try:
        measured = _loudnorm_measure(path, target_i, target_tp, target_lra)
        _apply_loudnorm(path, measured_wav, target_i, target_tp, target_lra, measured)
        current = measured_wav
        iterations = 0
        last_tp = None
        used_numpy = False
        if encode_aware:
            while iterations < max_iterations:
                _encode_aac(current, aac_path)
                tp_info = measure_true_peak_4x(aac_path)
                if not tp_info.get("ok"):
                    return tp_info
                last_tp = float(tp_info["true_peak_dbtp"])
                used_numpy = used_numpy or bool(tp_info.get("used_numpy"))
                iterations += 1
                if last_tp <= target_tp + TP_SLACK_DB:
                    break
                nxt = os.path.join(tmpdir, f"reduce-{iterations}.wav")
                _local_tp_reduce(current, nxt, target_tp, last_tp - target_tp)
                current = nxt
        else:
            iterations = 0

        if has_video:
            mux_audio_onto_video(path, current, output_path, copy_video=True)
        else:
            ext = os.path.splitext(output_path)[1].lower()
            codec = "pcm_s16le" if ext in {".wav", ".aif", ".aiff"} else "aac"
            cmd = [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                current,
                "-c:a",
                codec,
            ]
            if codec == "aac":
                cmd.extend(["-b:a", AAC_BITRATE])
            cmd.append(output_path)
            run(cmd, timeout=600)

        outgoing = measure_loudness(output_path)
        if encode_aware:
            # Final decoded-AAC TP on the delivered file.
            delivered_audio = os.path.join(tmpdir, "delivered.m4a")
            _encode_aac(output_path, delivered_audio)
            final_tp = measure_true_peak_4x(delivered_audio)
        else:
            final_tp = {"ok": True, "true_peak_dbtp": outgoing.get("true_peak_dbtp")}

        video_md5_in = _video_md5(path) if has_video else None
        video_md5_out = _video_md5(output_path) if has_video else None
        return {
            "ok": True,
            "output_path": os.path.abspath(output_path),
            "target_i": float(target_i),
            "target_tp": float(target_tp),
            "target_lra": float(target_lra),
            "input": {
                "I": incoming.get("integrated_lufs"),
                "TP": incoming.get("true_peak_dbtp"),
                "LRA": incoming.get("lra"),
            },
            "output": {
                "I": outgoing.get("integrated_lufs"),
                "TP": outgoing.get("true_peak_dbtp"),
                "LRA": outgoing.get("lra"),
            },
            "iterations": iterations,
            "encode_aware": bool(encode_aware),
            "decoded_aac_true_peak_dbtp": final_tp.get("true_peak_dbtp") if final_tp else last_tp,
            "video_copied": bool(has_video),
            "video_md5_in": video_md5_in,
            "video_md5_out": video_md5_out,
            "used_numpy": used_numpy,
            "note": (
                "Two-pass loudnorm + limiter is pure ffmpeg. "
                "Encode-aware true-peak local reduction uses numpy when "
                "installed (pip install 'rushcut-mcp[analysis]') and "
                "ffmpeg windowed limiting otherwise."
            ),
        }
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
