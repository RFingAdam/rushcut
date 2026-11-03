"""Hum / tonal-line cleanup and noise-floor analysis."""
from __future__ import annotations

import math
import subprocess

import numpy as np

from rushcut.analysis.cleanup import analyze_noise_floor, audio_cleanup, iter_stft_chunks
from rushcut.analysis.pcm import decode_pcm


def _band_rms(samples, sr: int, ch: int, lo: float, hi: float) -> float:
    arr = np.frombuffer(samples.tobytes(), dtype=np.int16).astype(np.float64)
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    spec = np.fft.rfft(arr)
    freqs = np.fft.rfftfreq(arr.size, 1.0 / sr)
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return -120.0
    power = float(np.mean(np.abs(spec[mask]) ** 2))
    if power <= 0:
        return -120.0
    # Normalize by bin count so before/after files of equal length compare.
    return 10.0 * math.log10(power / spec.size + 1e-18)


def test_analyze_noise_floor_json(tmp_path, ffmpeg_bin):
    src = tmp_path / "hum.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=60:sample_rate=16000:duration=2.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1110:sample_rate=16000:duration=2.5",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "[a]",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    result = analyze_noise_floor(str(src), window_seconds=1.0, prominence_db=8.0)
    assert result["ok"] is True, result
    assert result["windows"], result
    for window in result["windows"]:
        assert "at_seconds" in window
        assert "bands" in window
        assert window["bands"], window
    lines = result["tonal_lines"]
    assert lines, result
    freqs = [line["freq_hz"] for line in lines]
    assert any(abs(f - 60) < 8 for f in freqs), freqs
    assert any(abs(f - 1110) < 30 for f in freqs), freqs
    for line in lines:
        assert "prominence_db" in line
        assert "mid_db" in line


def test_cleanup_hum_and_whine_under_speech(tmp_path, ffmpeg_bin):
    """60 Hz + 1110 Hz drop ≥ 20 dB; speech-band RMS changes < 1 dB."""
    src = tmp_path / "dirty.wav"
    # Speech-like: band-limited noise (300–800 and 1500–2500 Hz) plus the
    # two stationary tones. Avoids putting extra sines in the speech band.
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:sample_rate=16000:amplitude=0.35:duration=3.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=60:sample_rate=16000:duration=3.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1110:sample_rate=16000:duration=3.0",
            "-filter_complex",
            "[0:a]highpass=f=300,lowpass=f=800,volume=1.2[s1];"
            "[0:a]highpass=f=1500,lowpass=f=2500,volume=0.9[s2];"
            "[1:a]volume=0.55[h];[2:a]volume=0.50[w];"
            "[s1][s2][h][w]amix=inputs=4:duration=first:normalize=0[a]",
            "-map",
            "[a]",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = tmp_path / "clean.wav"
    result = audio_cleanup(
        str(src),
        output_path=str(out),
        notches=[{"freq": 60, "q": 12}, {"freq": 1110, "q": 18}],
        hp_hz=80,
        adaptive_tonal=True,
        ms_mode=False,
        max_reduction_db=30,
        comparison_clip=True,
    )
    assert result["ok"] is True, result
    assert result["streamed_stft"] is True
    assert result["hp_hz"] == 80
    assert result["adaptive_tonal"] is True
    assert result["comparison_clip"]
    before, sr, ch = decode_pcm(str(src), sample_rate=16000, channels=2)
    after, _, _ = decode_pcm(str(out), sample_rate=16000, channels=2)
    hum_in = _band_rms(before, sr, ch, 50, 70)
    hum_out = _band_rms(after, sr, ch, 50, 70)
    whine_in = _band_rms(before, sr, ch, 1090, 1130)
    whine_out = _band_rms(after, sr, ch, 1090, 1130)
    speech_in = _band_rms(before, sr, ch, 350, 800)
    speech_out = _band_rms(after, sr, ch, 350, 800)
    speech2_in = _band_rms(before, sr, ch, 1500, 2500)
    speech2_out = _band_rms(after, sr, ch, 1500, 2500)
    assert hum_in - hum_out >= 20.0, f"60 Hz drop {hum_in - hum_out:.2f} dB"
    assert whine_in - whine_out >= 20.0, f"1110 Hz drop {whine_in - whine_out:.2f} dB"
    assert abs(speech_in - speech_out) < 1.0, f"speech-low change {speech_in - speech_out:.2f} dB"
    assert abs(speech2_in - speech2_out) < 1.0, f"speech-high change {speech2_in - speech2_out:.2f} dB"


def test_stft_streams_in_chunks():
    """A long buffer is processed in bounded STFT chunks, never all frames at once."""
    n = 16000 * 30  # 30 s at 16 kHz — stands in for a multi-hour file.
    x = np.zeros(n, dtype=np.float64)
    x[::100] = 0.1
    seen: list[int] = []

    def on_chunk(_offset: int, frames) -> None:
        seen.append(int(frames.shape[0]))
        # Caller must not retain every frame: each callback is one chunk.
        assert frames.shape[0] <= int((8.0 * 16000) / 512) + 1

    frames_total = 0
    for _off, frames in iter_stft_chunks(x, on_chunk=on_chunk):
        frames_total += frames.shape[0]
        del frames
    assert seen
    assert max(seen) < 400  # ~8 s of hop-512 frames, not 30 s × all hops
    assert frames_total > 100
    assert sum(seen) == frames_total
