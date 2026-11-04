"""Speaker attribution from separate mic and mix audio tracks (#28)."""
from __future__ import annotations

import math
import os
import struct
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media
from .transcript import word_times

HOST = "host"
OTHER = "other"
OVERLAP = "overlap"
UNKNOWN = "unknown"


def _decode_stream(
    path: str,
    *,
    stream_index: int | None = None,
    audio_index: int | None = None,
    sample_rate: int = 8000,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> tuple[list[float], int]:
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    if start_seconds:
        cmd += ["-ss", f"{float(start_seconds):.3f}"]
    cmd += ["-i", path]
    if stream_index is not None:
        cmd += ["-map", f"0:{int(stream_index)}"]
    elif audio_index is not None:
        cmd += ["-map", f"0:a:{int(audio_index)}"]
    if end_seconds is not None:
        duration = float(end_seconds) - float(start_seconds or 0)
        cmd += ["-t", f"{max(0.05, duration):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(int(sample_rate)), "-f", "s16le", "-acodec", "pcm_s16le", tmp.name]
    try:
        run(cmd, timeout=180)
        with open(tmp.name, "rb") as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    samples = [s / 32768.0 for (s,) in struct.iter_unpack("<h", raw[: len(raw) - (len(raw) % 2)])]
    return samples, int(sample_rate)


def _rms(samples: list[float], start: int = 0, end: int | None = None) -> float:
    sl = samples[start:end]
    if not sl:
        return 0.0
    return math.sqrt(sum(s * s for s in sl) / len(sl))


def _cross_correlation_delay(
    mic: list[float],
    mix: list[float],
    sample_rate: int,
    max_delay_s: float = 0.25,
) -> tuple[float, float]:
    """Return (delay_seconds, gain) so mix ≈ gain * delayed(mic).

    Positive delay means the mic leads the mix (mix is later).
    """
    n = min(len(mic), len(mix), sample_rate * 20)
    if n < sample_rate // 10:
        return 0.0, 1.0
    a = mic[:n]
    b = mix[:n]
    max_lag = max(1, int(max_delay_s * sample_rate))
    best_lag = 0
    best = float("-inf")
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    a0 = [x - mean_a for x in a]
    b0 = [x - mean_b for x in b]
    # Search lags where mix[t] aligns with mic[t - lag] (mic earlier).
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            left = a0[: n - lag]
            right = b0[lag:]
        else:
            left = a0[-lag:]
            right = b0[: n + lag]
        if not left:
            continue
        score = sum(x * y for x, y in zip(left, right)) / len(left)
        if score > best:
            best = score
            best_lag = lag
    delay_s = best_lag / float(sample_rate)
    if best_lag >= 0:
        mic_aln = a[: n - best_lag]
        mix_aln = b[best_lag:]
    else:
        mic_aln = a[-best_lag:]
        mix_aln = b[: n + best_lag]
    # Least-squares gain on host-active samples so other mix energy is ignored.
    thresh = 0.02
    num = 0.0
    den = 0.0
    for mic_s, mix_s in zip(mic_aln, mix_aln):
        if abs(mic_s) < thresh:
            continue
        num += mic_s * mix_s
        den += mic_s * mic_s
    gain = (num / den) if den > 1e-8 else 1.0
    return delay_s, gain


def _window_energy(samples: list[float], sr: int, start_s: float, end_s: float) -> float:
    i0 = max(0, int(start_s * sr))
    i1 = max(i0 + 1, int(end_s * sr))
    return _rms(samples, i0, i1)


def _resolve_track(
    audio_streams: list[dict[str, Any]],
    spec: Any,
    role: str,
) -> dict[str, Any] | None:
    if spec is None:
        for stream in audio_streams:
            title = str(stream.get("title") or stream.get("handler_name") or "").casefold()
            if role in title:
                return stream
        return None
    if not isinstance(spec, (dict, str, int)) and spec is not None:
        spec = {
            "role": getattr(spec, "role", None),
            "stream_index": getattr(spec, "stream_index", None),
            "audio_index": getattr(spec, "audio_index", None),
            "title": getattr(spec, "title", None),
        }
    if isinstance(spec, dict):
        if spec.get("stream_index") is not None:
            spec = spec.get("stream_index")
        elif spec.get("audio_index") is not None:
            spec = spec.get("audio_index")
        elif spec.get("title"):
            spec = spec.get("title")
        elif spec.get("role"):
            spec = spec.get("role")
    if isinstance(spec, str):
        key = spec.strip().casefold()
        if key.startswith("a:"):
            try:
                want = int(key.split(":", 1)[1])
            except ValueError:
                want = None
            if want is not None:
                for stream in audio_streams:
                    if int(stream.get("audio_index") or -1) == want:
                        return stream
        for stream in audio_streams:
            title = str(stream.get("title") or "").casefold()
            handler = str(stream.get("handler_name") or "").casefold()
            if key in {title, handler} or key in title or key in handler:
                return stream
        return None
    try:
        value = int(spec)
    except (TypeError, ValueError):
        return None
    for stream in audio_streams:
        if int(stream.get("index") or -1) == value:
            return stream
    for stream in audio_streams:
        if int(stream.get("audio_index") or -1) == value:
            return stream
    # 1-based audio order (tracks={mic: 2, mix: 1})
    if value >= 1:
        for stream in audio_streams:
            if int(stream.get("audio_index") or -1) == value - 1:
                return stream
    return None


def _tracks_from_profile(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    audio = getattr(profile, "audio", None)
    if audio is None and isinstance(profile, dict):
        audio = profile.get("audio")
    tracks = getattr(audio, "tracks", None) if audio is not None else None
    if tracks is None and isinstance(audio, dict):
        tracks = audio.get("tracks")
    out: dict[str, Any] = {}
    for item in tracks or []:
        if isinstance(item, dict):
            role = item.get("role")
            out[str(role)] = item
        else:
            role = getattr(item, "role", None)
            out[str(role)] = item
    return out


def attribute_speakers(
    path: str,
    tracks: dict[str, Any] | None = None,
    transcript: dict[str, Any] | list[dict[str, Any]] | None = None,
    *,
    profile: Any = None,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Label each transcript word host / other / overlap from mic vs mix energy."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    audio_streams = list(probe.get("audio_streams") or [])
    roles = dict(tracks or {})
    if not roles:
        roles.update(_tracks_from_profile(profile))
    if not roles and len(audio_streams) >= 2:
        roles = {"mix": audio_streams[0].get("index"), "mic": audio_streams[1].get("index")}

    word_list: list[dict[str, Any]]
    if words is not None:
        word_list = [dict(w) for w in words]
    elif isinstance(transcript, list):
        word_list = [dict(w) for w in transcript]
    elif isinstance(transcript, dict):
        word_list = [dict(w) for w in (transcript.get("words") or [])]
    else:
        word_list = []

    if len(audio_streams) < 2:
        labeled = []
        for word in word_list:
            item = dict(word)
            item["speaker"] = UNKNOWN
            item["confidence"] = 0.0
            labeled.append(item)
        return {
            "ok": True,
            "path": os.path.abspath(path),
            "note": "single-track source; speaker set to unknown",
            "speaker": UNKNOWN,
            "delay_seconds": 0.0,
            "gain": 1.0,
            "words": labeled,
            "spans": [],
            "audio_streams": audio_streams,
        }

    mic_stream = _resolve_track(audio_streams, roles.get("mic"), "mic")
    mix_stream = _resolve_track(audio_streams, roles.get("mix"), "mix")
    if mic_stream is None or mix_stream is None:
        return {
            "ok": False,
            "error": "could not resolve mic and mix tracks",
            "audio_streams": audio_streams,
            "tracks": roles,
        }

    try:
        mic, sr = _decode_stream(path, stream_index=int(mic_stream["index"]))
        mix, _sr = _decode_stream(path, stream_index=int(mix_stream["index"]), sample_rate=sr)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    delay_s, gain = _cross_correlation_delay(mic, mix, sr)
    delay_samples = round(delay_s * sr)

    def mix_residual_rms(start_s: float, end_s: float) -> tuple[float, float]:
        mic_e = _window_energy(mic, sr, start_s, end_s)
        # Mix window is later by delay_s when delay is positive.
        mix_e = _window_energy(mix, sr, start_s + delay_s, end_s + delay_s)
        i0 = max(0, int((start_s + delay_s) * sr))
        i1 = max(i0 + 1, int((end_s + delay_s) * sr))
        residual: list[float] = []
        for i, sample in enumerate(mix[i0:i1], start=i0):
            mic_i = i - delay_samples
            bleed = gain * mic[mic_i] if 0 <= mic_i < len(mic) else 0.0
            residual.append(sample - bleed)
        return mic_e, _rms(residual) if residual else mix_e

    labeled = []
    for word in word_list:
        item = dict(word)
        pair = word_times(item)
        if pair is None:
            item["speaker"] = UNKNOWN
            item["confidence"] = 0.0
            labeled.append(item)
            continue
        start, end = pair
        if end <= start:
            end = start + 0.08
        mic_e, resid_e = mix_residual_rms(start, end)
        speech_floor = 0.012
        mic_on = mic_e >= speech_floor
        other_floor = speech_floor * 0.85
        if mic_on:
            # Host bleed leftover after gain/delay subtraction is not another speaker.
            other_floor = max(other_floor, mic_e * 0.55)
        other_on = resid_e >= other_floor
        if mic_on and other_on:
            speaker = OVERLAP
            confidence = min(1.0, (mic_e + resid_e) / 0.08)
        elif mic_on:
            speaker = HOST
            confidence = min(1.0, mic_e / 0.04)
        elif other_on:
            speaker = OTHER
            confidence = min(1.0, resid_e / 0.04)
        else:
            speaker = UNKNOWN
            confidence = 0.0
        item["speaker"] = speaker
        item["confidence"] = round(float(confidence), 3)
        item["mic_rms"] = round(mic_e, 5)
        item["mix_residual_rms"] = round(resid_e, 5)
        labeled.append(item)

    spans: list[dict[str, Any]] = []
    for word in labeled:
        pair = word_times(word)
        if pair is None:
            continue
        speaker = word.get("speaker") or UNKNOWN
        if spans and spans[-1]["speaker"] == speaker and pair[0] - spans[-1]["end_seconds"] <= 0.35:
            spans[-1]["end_seconds"] = round(pair[1], 3)
            spans[-1]["word_count"] += 1
        else:
            spans.append(
                {
                    "speaker": speaker,
                    "start_seconds": round(pair[0], 3),
                    "end_seconds": round(pair[1], 3),
                    "word_count": 1,
                }
            )

    return {
        "ok": True,
        "path": os.path.abspath(path),
        "delay_seconds": round(delay_s, 4),
        "gain": round(gain, 4),
        "tracks": {
            "mic": {"index": mic_stream.get("index"), "title": mic_stream.get("title")},
            "mix": {"index": mix_stream.get("index"), "title": mix_stream.get("title")},
        },
        "words": labeled,
        "spans": spans,
        "audio_streams": audio_streams,
    }
