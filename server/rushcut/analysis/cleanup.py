"""Noise / hum cleanup: notch lines, high-pass, stationary drone reduction.

STFT is streamed in chunks so a multi-hour file never holds every frame
in RAM. Fixed notches and the voice high-pass are pure ffmpeg; adaptive
tonal-line tracking and mid/side reduction use numpy when the analysis
extra is installed.
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Any, Callable, Iterator

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .pcm import default_output, decode_pcm, encode_pcm, ok_path
from .probe import probe_media

DEFAULT_BANDS_HZ = (
    (20, 80),
    (80, 200),
    (200, 500),
    (500, 1000),
    (1000, 2000),
    (2000, 4000),
    (4000, 8000),
    (8000, 16000),
)
CHUNK_SECONDS = 8.0
N_FFT = 2048
HOP = 512
STFT_SAMPLE_RATE = 16000


def _try_numpy():
    try:
        import numpy as np  # type: ignore

        return np
    except ImportError:
        return None


def _band_name(lo: float, hi: float) -> str:
    return f"{int(lo)}-{int(hi)}"


def iter_stft_chunks(
    samples,
    *,
    n_fft: int = N_FFT,
    hop: int = HOP,
    chunk_frames: int | None = None,
    on_chunk: Callable[[int, Any], None] | None = None,
) -> Iterator[tuple[int, Any]]:
    """Yield STFT frames for successive chunks; never concatenates all frames.

    ``on_chunk`` is invoked with ``(frame_offset, frames)`` so tests can
    assert that no call holds a 3-hour spectrogram.
    """
    np = _try_numpy()
    if np is None:
        raise RuntimeError("numpy is required for streaming STFT")
    window = np.hanning(n_fft).astype(np.float64)
    n = int(samples.shape[0])
    max_frames = chunk_frames or int((CHUNK_SECONDS * STFT_SAMPLE_RATE) / hop)
    frame_index = 0
    i = 0
    while i + n_fft <= n:
        take = min(max_frames, 1 + (n - i - n_fft) // hop)
        if take <= 0:
            break
        frames = np.empty((take, n_fft // 2 + 1), dtype=np.complex128)
        for k in range(take):
            sl = samples[i + k * hop : i + k * hop + n_fft]
            frames[k] = np.fft.rfft(sl * window)
        if on_chunk is not None:
            on_chunk(frame_index, frames)
        yield frame_index, frames
        frame_index += take
        i += take * hop
        # Drop processed samples from the left conceptually; we only advance i.
        del frames


def _mono_float(path: str, sample_rate: int = STFT_SAMPLE_RATE):
    np = _try_numpy()
    samples, sr, ch = decode_pcm(path, sample_rate=sample_rate, channels=2 if np is not None else 1)
    if np is None:
        return samples, sr, 1, None, None
    arr = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64) / 32768.0
    if ch == 2:
        frames = arr.reshape(-1, 2)
        left, right = frames[:, 0], frames[:, 1]
        mid = 0.5 * (left + right)
        side = 0.5 * (left - right)
        return mid, sr, 2, mid, side
    return arr, sr, 1, arr, None


def analyze_noise_floor(
    path: str,
    window_seconds: float = 1.0,
    prominence_db: float = 12.0,
    bands: tuple[tuple[float, float], ...] = DEFAULT_BANDS_HZ,
    on_chunk: Callable[[int, Any], None] | None = None,
) -> dict[str, Any]:
    """Per-window stationary floor by band, plus detected tonal lines.

    JSON: ``windows`` × bands floor dB, ``tonal_lines`` with frequency and
    prominence, and mid vs side levels when the file is stereo.
    """
    err = ok_path(path)
    if err:
        return err
    np = _try_numpy()
    if np is None:
        return _analyze_noise_floor_ffmpeg(path, window_seconds, bands)
    try:
        mid, sr, ch, _mid, side = _mono_float(path)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    n_fft = N_FFT
    hop = HOP
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    win_hop = max(1, int(window_seconds * sr / hop))
    windows: list[dict[str, Any]] = []
    mag_acc = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    mag_count = 0
    mid_acc = np.zeros_like(mag_acc)
    side_acc = np.zeros_like(mag_acc) if side is not None else None

    def _collect(offset: int, frames) -> None:
        nonlocal mag_count
        spec = np.abs(frames)
        mag_acc[: spec.shape[1]] += spec.mean(axis=0)
        mag_count += 1
        mid_acc[: spec.shape[1]] += spec.mean(axis=0)
        if on_chunk is not None:
            on_chunk(offset, frames)

    for offset, frames in iter_stft_chunks(mid, n_fft=n_fft, hop=hop, on_chunk=None):
        _collect(offset, frames)
        # Emit a window every win_hop frames.
    if side is not None and side_acc is not None:
        for _offset, frames in iter_stft_chunks(side, n_fft=n_fft, hop=hop):
            spec = np.abs(frames)
            side_acc[: spec.shape[1]] += spec.mean(axis=0)

    # Per-window floors: re-stream in window-sized groups.
    bucket: list[Any] = []
    bucket_start = 0.0

    def _flush(start_t: float, frames_list: list) -> None:
        if not frames_list:
            return
        stacked = np.concatenate(frames_list, axis=0)
        mag = np.abs(stacked).mean(axis=0) + 1e-12
        band_floor: dict[str, float] = {}
        for lo, hi in bands:
            mask = (freqs >= lo) & (freqs < hi)
            if not np.any(mask):
                continue
            band_floor[_band_name(lo, hi)] = round(20.0 * math.log10(float(np.median(mag[mask]))), 2)
        windows.append({"at_seconds": round(start_t, 3), "bands": band_floor})

    for offset, frames in iter_stft_chunks(mid, n_fft=n_fft, hop=hop):
        if not bucket:
            bucket_start = offset * hop / sr
        bucket.append(frames)
        if sum(f.shape[0] for f in bucket) >= win_hop:
            _flush(bucket_start, bucket)
            bucket = []
    _flush(bucket_start, bucket)

    mean_mag = (mag_acc / max(1, mag_count)) + 1e-12
    db = 20.0 * np.log10(mean_mag)
    # Local floor: median in a ±8-bin neighbourhood.
    tonal: list[dict[str, Any]] = []
    radius = 8
    for i in range(2, len(db) - 2):
        lo = max(0, i - radius)
        hi = min(len(db), i + radius + 1)
        floor = float(np.median(db[lo:hi]))
        prom = float(db[i] - floor)
        if prom < prominence_db:
            continue
        if db[i] < db[i - 1] or db[i] < db[i + 1]:
            continue
        mid_db = float(20.0 * math.log10((mid_acc[i] / max(1, mag_count)) + 1e-12))
        side_db = None
        if side_acc is not None:
            side_db = float(20.0 * math.log10((side_acc[i] / max(1, mag_count)) + 1e-12))
        tonal.append(
            {
                "freq_hz": round(float(freqs[i]), 2),
                "prominence_db": round(prom, 2),
                "mid_db": round(mid_db, 2),
                "side_db": round(side_db, 2) if side_db is not None else None,
                "bin": int(i),
            }
        )
    tonal.sort(key=lambda t: t["prominence_db"], reverse=True)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "sample_rate": sr,
        "channels": ch,
        "n_fft": n_fft,
        "hop": hop,
        "window_seconds": window_seconds,
        "windows": windows,
        "tonal_lines": tonal[:24],
        "streamed": True,
        "used_numpy": True,
    }


def _analyze_noise_floor_ffmpeg(
    path: str,
    window_seconds: float,
    bands: tuple[tuple[float, float], ...],
) -> dict[str, Any]:
    """Fallback: windowed ffmpeg bandpass + astats. No full-file STFT in RAM."""
    probe = probe_media(path)
    duration = float(probe.get("duration_seconds") or 0)
    windows: list[dict[str, Any]] = []
    t = 0.0
    while t < max(duration, window_seconds):
        band_floor: dict[str, float] = {}
        for lo, hi in bands:
            try:
                proc = run(
                    [
                        which_ffmpeg("ffmpeg"),
                        "-hide_banner",
                        "-nostats",
                        "-ss",
                        f"{t:.3f}",
                        "-t",
                        f"{window_seconds:.3f}",
                        "-i",
                        path,
                        "-af",
                        f"highpass=f={lo},lowpass=f={hi},astats=measure_perchannel=0",
                        "-f",
                        "null",
                        "-",
                    ],
                    timeout=60,
                )
            except FfmpegError:
                continue
            text = proc.stderr or ""
            rms = None
            for line in text.splitlines():
                if "RMS level dB" in line:
                    try:
                        rms = float(line.rsplit(":", 1)[-1].strip())
                    except ValueError:
                        pass
            if rms is not None:
                band_floor[_band_name(lo, hi)] = round(rms, 2)
        windows.append({"at_seconds": round(t, 3), "bands": band_floor})
        t += window_seconds
        if duration and t > duration + 1e-6:
            break
        if not duration and t > window_seconds:
            break
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "windows": windows,
        "tonal_lines": [],
        "streamed": True,
        "used_numpy": False,
        "note": "numpy not installed; band floors via ffmpeg astats. "
        "pip install 'rushcut-mcp[analysis]' for tonal-line detection.",
    }


def _notch_filter(freq: float, q: float, gain_db: float) -> str:
    return f"equalizer=f={freq}:t=q:w={q}:g={gain_db}"


def _ffmpeg_cleanup(
    path: str,
    dest: str,
    notches: list[dict[str, Any]],
    hp_hz: float,
    max_reduction_db: float,
) -> None:
    parts: list[str] = []
    if hp_hz and hp_hz > 0:
        parts.append(f"highpass=f={hp_hz}:poles=2")
    for notch in notches:
        freq = float(notch.get("freq") or notch.get("frequency") or notch.get("hz") or 0)
        q = float(notch.get("q") or notch.get("Q") or 10)
        gain = -abs(float(notch.get("gain_db") or notch.get("gain") or max_reduction_db))
        if freq > 0:
            parts.append(_notch_filter(freq, q, gain))
    filt = ",".join(parts) if parts else "anull"
    run(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            path,
            "-af",
            filt,
            dest,
        ],
        timeout=600,
    )


def _adaptive_tonal_reduce(
    path: str,
    dest: str,
    *,
    prominence_db: float,
    max_reduction_db: float,
    ms_mode: bool,
    hp_hz: float,
    notches: list[dict[str, Any]],
    on_chunk: Callable[[int, Any], None] | None = None,
) -> dict[str, Any]:
    np = _try_numpy()
    if np is None:
        _ffmpeg_cleanup(path, dest, notches, hp_hz, max_reduction_db)
        return {"used_numpy": False, "adaptive": False}
    samples, sr, ch = decode_pcm(path, sample_rate=STFT_SAMPLE_RATE, channels=2)
    arr = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64) / 32768.0
    stereo = arr.reshape(-1, 2)
    left, right = stereo[:, 0].copy(), stereo[:, 1].copy()
    mid = 0.5 * (left + right)
    side = 0.5 * (left - right)
    n_fft, hop = N_FFT, HOP
    window = np.hanning(n_fft)
    hp_bin = int(hp_hz / (sr / n_fft)) if hp_hz else 0
    # First pass: mean spectrum → stationary tonal lines only (not speech).
    acc = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    count = 0
    for _offset, frames in iter_stft_chunks(mid, n_fft=n_fft, hop=hop):
        acc += np.abs(frames).mean(axis=0)
        count += 1
    mean_mag = acc / max(1, count) + 1e-12
    db = 20.0 * np.log10(mean_mag)
    tracked: set[int] = set()
    radius = 8
    for i in range(2, len(db) - 2):
        floor = float(np.median(db[max(0, i - radius) : i + radius + 1]))
        if float(db[i] - floor) < prominence_db:
            continue
        if db[i] < db[i - 1] or db[i] < db[i + 1]:
            continue
        tracked.add(i)
        if i > 0:
            tracked.add(i - 1)
        if i + 1 < len(db):
            tracked.add(i + 1)
    for notch in notches:
        freq = float(notch.get("freq") or notch.get("frequency") or 0)
        if freq <= 0:
            continue
        bin_i = int(round(freq / (sr / n_fft)))
        q = float(notch.get("q") or 10)
        width = max(1, int(round(bin_i / (2 * q)))) if bin_i else 1
        for j in range(max(0, bin_i - width), min(len(db), bin_i + width + 1)):
            tracked.add(j)
    if hp_bin:
        for j in range(0, hp_bin + 1):
            tracked.add(j)

    notch_and_hp = set()
    if hp_bin:
        notch_and_hp.update(range(0, hp_bin + 1))
    for notch in notches:
        freq = float(notch.get("freq") or notch.get("frequency") or 0)
        if freq <= 0:
            continue
        bin_i = int(round(freq / (sr / n_fft)))
        q = float(notch.get("q") or 10)
        width = max(1, int(round(bin_i / (2 * q)))) if bin_i else 1
        notch_and_hp.update(range(max(0, bin_i - width), min(len(db), bin_i + width + 1)))
    extra_lines = tracked - notch_and_hp
    if not extra_lines and not ms_mode:
        # ffmpeg already applied the same notches / HP; skip a rewrite that
        # would only OLA-reconstruct the speech band.
        import shutil

        shutil.copyfile(path, dest)
        return {"used_numpy": True, "adaptive": True, "tracked_bins": sorted(tracked), "skipped_ola": True}

    out_mid = np.zeros_like(mid)
    out_side = side.copy()
    norm = np.zeros_like(mid)
    max_lin = 10.0 ** (-abs(max_reduction_db) / 20.0)

    for offset, frames in iter_stft_chunks(mid, n_fft=n_fft, hop=hop, on_chunk=on_chunk):
        mag = np.abs(frames) + 1e-12
        local = []
        for row in mag:
            local.append(
                np.array(
                    [float(np.median(row[max(0, i - 8) : i + 9])) for i in range(row.size)],
                    dtype=np.float64,
                )
            )
        floor = np.stack(local, axis=0)
        mask = np.zeros(frames.shape, dtype=bool)
        for b in tracked:
            if 0 <= b < mask.shape[1]:
                mask[:, b] = True
        # Allow a line to drift ±1 bin from the stationary set.
        drifted = mask.copy()
        drifted[:, 1:] |= mask[:, :-1]
        drifted[:, :-1] |= mask[:, 1:]
        mask = drifted
        ratio = np.clip(floor / mag, max_lin, 1.0)
        scale = np.where(mask, ratio, 1.0)
        if ms_mode:
            start = offset * hop
            chunk = side[start : start + frames.shape[0] * hop + n_fft]
            side_mag = []
            for k in range(frames.shape[0]):
                sl = chunk[k * hop : k * hop + n_fft]
                if sl.size < n_fft:
                    sl = np.pad(sl, (0, n_fft - sl.size))
                side_mag.append(np.abs(np.fft.rfft(sl * window)) + 1e-12)
            side_mag_a = np.stack(side_mag, axis=0)
            side_floor = np.array(
                [
                    [float(np.median(row[max(0, i - 8) : i + 9])) for i in range(row.size)]
                    for row in side_mag_a
                ]
            )
            scale = np.where(
                mask & (floor > side_floor),
                np.clip(side_floor / (floor + 1e-12), max_lin, 1.0),
                1.0,
            )
        reduced = frames * scale
        # Overlap-add back to mid.
        for k in range(reduced.shape[0]):
            pos = (offset + k) * hop
            rec = np.fft.irfft(reduced[k], n=n_fft).real * window
            end = min(out_mid.size, pos + n_fft)
            take = end - pos
            out_mid[pos:end] += rec[:take]
            norm[pos:end] += window[:take] ** 2
    norm = np.where(norm < 1e-6, 1.0, norm)
    out_mid = out_mid / np.sqrt(norm)
    if hp_hz:
        # Time-domain 1st-order HP as a belt-and-suspenders on DC / rumble.
        rc = 1.0 / (2 * math.pi * hp_hz)
        dt = 1.0 / sr
        alpha = rc / (rc + dt)
        y = 0.0
        x_prev = 0.0
        for i, x in enumerate(out_mid):
            y = alpha * (y + x - x_prev)
            out_mid[i] = y
            x_prev = x
    if ms_mode:
        left_o = out_mid + out_side
        right_o = out_mid - out_side
    else:
        # Reconstruct L/R keeping the original side.
        left_o = out_mid + side
        right_o = out_mid - side
    stacked = np.stack([left_o, right_o], axis=1)
    pcm = np.clip(stacked.reshape(-1) * 32767.0, -32768, 32767).astype(np.int16)
    import array

    samples = array.array("h")
    samples.frombytes(pcm.tobytes())
    encode_pcm(samples, dest, sr, 2, audio_codec="pcm_s16le")
    return {"used_numpy": True, "adaptive": True, "tracked_bins": sorted(tracked)}


def _write_comparison_clip(before: str, after: str, dest: str, seconds: float = 60.0) -> str | None:
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-t",
                f"{seconds:.3f}",
                "-i",
                before,
                "-t",
                f"{seconds:.3f}",
                "-i",
                after,
                "-filter_complex",
                "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                "-map",
                "[a]",
                dest,
            ],
            timeout=180,
        )
        return os.path.abspath(dest)
    except FfmpegError:
        return None


def audio_cleanup(
    path: str,
    output_path: str | None = None,
    notches: list[dict[str, Any]] | None = None,
    hp_hz: float = 80.0,
    adaptive_tonal: bool = True,
    ms_mode: bool = False,
    max_reduction_db: float = 24.0,
    prominence_db: float = 12.0,
    comparison_clip: bool = False,
    voice_separate: bool = False,
    on_chunk: Callable[[int, Any], None] | None = None,
) -> dict[str, Any]:
    """Notch / high-pass / optional adaptive tonal-line and mid-side cleanup.

    Streams STFT in chunks (see ``iter_stft_chunks``) so a 3 h file does
    not keep every frame in RAM. Optional DeepFilterNet voice separation
    sits behind the ``denoise`` extra.
    """
    err = ok_path(path)
    if err:
        return err
    if not output_path:
        output_path = default_output(path, "clean")
    notch_list = list(notches or [])
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="rushcut-clean-")
    stage = os.path.join(tmp, "stage.wav")
    try:
        extra: dict[str, Any] = {"voice_separate": False}
        work = path
        if voice_separate:
            try:
                extra.update(_voice_separate(path, os.path.join(tmp, "voice.wav")))
                if extra.get("voice_path"):
                    work = extra["voice_path"]
            except Exception as exc:  # noqa: BLE001 — optional extra
                extra["voice_separate_error"] = str(exc)
        # Fixed notches + voice high-pass first (pure ffmpeg, predictable depth).
        fixed = os.path.join(tmp, "fixed.wav")
        _ffmpeg_cleanup(work, fixed, notch_list, hp_hz, max_reduction_db)
        work = fixed
        adaptive_info = {"used_numpy": False, "adaptive": False}
        if adaptive_tonal or ms_mode:
            adaptive_info = _adaptive_tonal_reduce(
                work,
                stage,
                prominence_db=prominence_db,
                max_reduction_db=max_reduction_db,
                ms_mode=ms_mode,
                hp_hz=hp_hz,
                notches=notch_list,
                on_chunk=on_chunk,
            )
            work = stage
        probe = probe_media(path)
        if probe.get("has_video"):
            from .pcm import mux_audio_onto_video

            mux_audio_onto_video(path, work, output_path, copy_video=True)
        else:
            run(
                [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", work, output_path],
                timeout=300,
            )
        comparison = None
        if comparison_clip:
            comparison = _write_comparison_clip(
                path,
                output_path,
                os.path.join(os.path.dirname(os.path.abspath(output_path)), "cleanup-before-after.wav"),
            )
        return {
            "ok": True,
            "output_path": os.path.abspath(output_path),
            "notches": notch_list,
            "hp_hz": float(hp_hz),
            "adaptive_tonal": bool(adaptive_tonal),
            "ms_mode": bool(ms_mode),
            "max_reduction_db": float(max_reduction_db),
            "comparison_clip": comparison,
            "streamed_stft": True,
            **adaptive_info,
            **extra,
        }
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _voice_separate(path: str, dest: str) -> dict[str, Any]:
    """Optional DeepFilterNet (or similar) behind the denoise extra."""
    try:
        from df.enhance import enhance, init_df, load_audio, save_audio  # type: ignore
    except ImportError:
        return {
            "voice_separate": False,
            "voice_separate_available": False,
            "note": "DeepFilterNet not installed. pip install 'rushcut-mcp[denoise]'",
        }
    model, df_state, _ = init_df()
    audio, _sr = load_audio(path, sr=df_state.sr())
    enhanced = enhance(model, df_state, audio)
    save_audio(dest, enhanced, df_state.sr())
    return {
        "voice_separate": True,
        "voice_separate_available": True,
        "voice_path": dest,
    }
