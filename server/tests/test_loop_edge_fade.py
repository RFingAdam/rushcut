"""check-loop edge-fade must flag a silence ramp, not a later onset (#60, #62)."""
from __future__ import annotations

import array
import audioop
import math
import subprocess
import wave
from pathlib import Path

from rushcut.cli import main as cli_main
from rushcut.quality.loop import (
    _linear_rms_db,
    _pcm_tail,
    check_loop,
    check_loop_audio,
)

_SR = 48000
# anoisesrc amplitude ≈ FS peak. 0.316 ≈ -10 dBFS peak, ~-20 dBFS RMS.
_STEADY_AMP = 10.0 ** (-10.0 / 20.0)
_BURST_AMP = 10.0 ** (-3.0 / 20.0)
_FADE_LIMIT = 0.08


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _mp4(ffmpeg_bin: str, dest: Path, audio_filter: str, duration: float = 1.0) -> Path:
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            f"color=c=green:s=160x90:r=24:d={duration}",
            "-f",
            "lavfi",
            "-i",
            audio_filter,
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-ar",
            str(_SR),
            "-ac",
            "2",
            str(dest),
        ]
    )
    return dest


def _steady_with_burst(ffmpeg_bin: str, dest: Path, burst_start: float, burst_end: float) -> Path:
    """1 s of steady noise with a louder burst spliced into [burst_start, burst_end)."""
    head = burst_start
    mid = burst_end - burst_start
    tail = 1.0 - burst_end
    filt = (
        f"anoisesrc=d={head:.3f}:c=white:r={_SR}:a={_STEADY_AMP}[h];"
        f"anoisesrc=d={mid:.3f}:c=white:r={_SR}:a={_BURST_AMP}[m];"
        f"anoisesrc=d={tail:.3f}:c=white:r={_SR}:a={_STEADY_AMP}[t];"
        f"[h][m][t]concat=n=3:v=0:a=1"
    )
    return _mp4(ffmpeg_bin, dest, filt, 1.0)


def _fs_from_dbfs(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0) * 32767.0


def _lcg_noise(n: int, dbfs: float, seed: int = 1234567) -> list[int]:
    amp = _fs_from_dbfs(dbfs)
    x = seed
    out: list[int] = []
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        value = ((x / 0x7FFFFFFF) * 2.0 - 1.0) * amp
        out.append(int(max(-32767, min(32767, value))))
    return out


def _sine_samples(n: int, dbfs: float, freq: float = 440.0) -> list[int]:
    amp = _fs_from_dbfs(dbfs)
    return [
        int(max(-32767, min(32767, amp * math.sin(2.0 * math.pi * freq * i / _SR))))
        for i in range(n)
    ]


def _write_wav(path: Path, samples: list[int]) -> Path:
    with wave.open(str(path), "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(_SR)
        fh.writeframes(array.array("h", samples).tobytes())
    return path


def _aac_from_wav(ffmpeg_bin: str, wav: Path, dest: Path, *, bitrate: str = "320k") -> Path:
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-i",
            str(wav),
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=24:d=1",
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-ar",
            str(_SR),
            "-ac",
            "2",
            str(dest),
        ]
    )
    return dest


def _edge_frames_db(path: Path, *, from_start: bool, count: int = 8) -> list[float]:
    pcm = _pcm_tail(str(path), start=from_start, seconds=0.35, sample_rate=_SR)
    width = 2
    frame = int(_SR * 0.002) * width
    n = len(pcm) // frame
    levels = [
        _linear_rms_db(audioop.rms(pcm[i * frame : (i + 1) * frame], width)) for i in range(n)
    ]
    if from_start:
        return levels[:count]
    return levels[-count:]


def _n1103_aac_with_priming(ffmpeg_bin: str, dest: Path) -> Path:
    """n1103_a render_v3 stand-in: AAC, first ~2 ms below -45 dBFS, program by 2-4 ms, onset ~0.18 s."""
    dbs: list[float] = []
    for quiet_ms in (2, 4, 6, 8):
        quiet = int(_SR * quiet_ms / 1000.0)
        head = int(_SR * 0.178)
        burst = int(_SR * 0.170)
        tail = _SR - quiet - head - burst
        samples = (
            _lcg_noise(quiet, -50.0)
            + _lcg_noise(head, -25.0)
            + _lcg_noise(burst, -3.0)
            + _lcg_noise(max(0, tail), -25.0)
        )
        wav = dest.with_name(f"{dest.stem}_{quiet_ms}ms.wav")
        _write_wav(wav, samples)
        _aac_from_wav(ffmpeg_bin, wav, dest)
        dbs = _edge_frames_db(dest, from_start=True, count=6)
        if len(dbs) >= 2 and dbs[0] <= -45.0 and dbs[1] > -40.0:
            return dest
    raise AssertionError(
        f"AAC fixture did not keep a sub-10 ms silent priming frame, last decode={dbs!r}"
    )


def test_later_gunshot_or_speech_onset_is_not_a_forced_fade(tmp_path, ffmpeg_bin):
    """Issue #60 synthetic: -20 dBFS bed, -3 dBFS burst at 0.20-0.30 s."""
    dest = _steady_with_burst(ffmpeg_bin, tmp_path / "burst.mp4", 0.20, 0.30)
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] <= _FADE_LIMIT, audio
    assert audio["edge_fade_out_seconds"] <= _FADE_LIMIT, audio
    assert "forced edge fade" not in (audio.get("detail") or "")
    looped = check_loop(str(dest), ssim_min=0.85)
    assert looped["ok"] is True
    assert (looped.get("edge_fade_in_seconds") or 0) <= _FADE_LIMIT
    assert (looped.get("edge_fade_out_seconds") or 0) <= _FADE_LIMIT
    assert looped["pass"] is True
    assert cli_main(["check-loop", str(dest)]) == 0


def test_n1103_pattern_flat_head_then_onset_is_not_a_forced_fade(tmp_path, ffmpeg_bin):
    """n1103_a v3: flat head RMS, onset after ~0.18 s, wrap still at program level."""
    dest = _steady_with_burst(ffmpeg_bin, tmp_path / "n1103_pattern.mp4", 0.18, 0.35)
    audio = check_loop_audio(str(dest))
    assert audio["audio_first_db"] > -40.0, audio
    assert audio["edge_fade_in_seconds"] <= _FADE_LIMIT, audio
    assert audio["edge_fade_out_seconds"] <= _FADE_LIMIT, audio
    assert audio["pass"] is True, audio.get("detail")


def test_real_0_11s_afade_at_the_head_still_fails(tmp_path, ffmpeg_bin):
    """s2_04 render_v2 stand-in: ffmpeg afade 0.11 s from silence."""
    filt = (
        f"anoisesrc=d=1:c=white:r={_SR}:a={_STEADY_AMP},"
        f"afade=t=in:st=0:d=0.11"
    )
    dest = _mp4(ffmpeg_bin, tmp_path / "afade011.mp4", filt, 1.0)
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] > _FADE_LIMIT, audio
    assert audio["pass"] is False
    assert "forced edge fade" in (audio.get("detail") or "")
    assert cli_main(["check-loop", str(dest)]) == 2


def test_monotonic_0_11s_ramp_from_minus_80_dbfs_fails(tmp_path, ffmpeg_bin):
    """Linear gain from -80 dBFS to program level over 0.11 s."""
    # 0.0001 FS = -80 dBFS; body amplitude is _STEADY_AMP.
    filt = (
        f"anoisesrc=d=1:c=white:r={_SR}:a={_STEADY_AMP},"
        "volume='if(lt(t,0.11), 0.0001+(1-0.0001)*t/0.11, 1)':eval=frame"
    )
    dest = _mp4(ffmpeg_bin, tmp_path / "ramp011.mp4", filt, 1.0)
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] > _FADE_LIMIT, audio
    assert audio["pass"] is False
    assert "forced edge fade" in (audio.get("detail") or "")


def test_later_burst_near_the_tail_is_not_a_forced_fade_out(tmp_path, ffmpeg_bin):
    """A gunshot in the far half of the 0.35 s tail window is not a fade-out."""
    dest = _steady_with_burst(ffmpeg_bin, tmp_path / "tail_burst.mp4", 0.70, 0.80)
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_out_seconds"] <= _FADE_LIMIT, audio
    assert audio["edge_fade_in_seconds"] <= _FADE_LIMIT, audio
    assert audio["pass"] is True, audio.get("detail")


def test_n1103_aac_encoder_priming_is_not_a_forced_fade(tmp_path, ffmpeg_bin):
    """Issue #62: first ~2 ms AAC decode just under -45 dBFS is priming, not a fade."""
    dest = _n1103_aac_with_priming(ffmpeg_bin, tmp_path / "n1103_a_v3.mp4")
    head = _edge_frames_db(dest, from_start=True, count=4)
    assert head[0] <= -45.0, head
    assert head[1] > -40.0, head
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] == 0.0, audio
    assert audio["edge_fade_out_seconds"] <= _FADE_LIMIT, audio
    assert audio["pass"] is True, audio.get("detail")
    assert "forced edge fade" not in (audio.get("detail") or "")
    assert cli_main(["check-loop", str(dest)]) == 0


def test_two_ms_of_minus_50_dbfs_on_a_tone_is_not_a_forced_fade(tmp_path, ffmpeg_bin):
    """Issue #62 synthetic: 2 ms of -50 dBFS then a tone must pass."""
    quiet = int(_SR * 0.002)
    samples = _lcg_noise(quiet, -50.0) + _sine_samples(_SR - quiet, -12.0)
    dest = tmp_path / "tone_preamble.mp4"
    _aac_from_wav(ffmpeg_bin, _write_wav(tmp_path / "tone_preamble.wav", samples), dest)
    head = _edge_frames_db(dest, from_start=True, count=4)
    assert head[0] <= -45.0, head
    assert head[1] > -40.0, head
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] == 0.0, audio
    assert audio["pass"] is True, audio.get("detail")
    assert "forced edge fade" not in (audio.get("detail") or "")
    assert cli_main(["check-loop", str(dest)]) == 0


def test_two_ms_of_trailing_encoder_padding_is_not_a_forced_fade_out(tmp_path, ffmpeg_bin):
    """Trailing AAC padding shorter than 10 ms is not a fade-out, even with a later burst."""
    quiet = int(_SR * 0.002)
    burst_at = int(_SR * 0.70)
    burst = int(_SR * 0.10)
    mid = _SR - burst_at - burst - quiet
    samples = (
        _lcg_noise(burst_at, -25.0)
        + _lcg_noise(burst, -3.0)
        + _lcg_noise(max(0, mid), -25.0)
        + _lcg_noise(quiet, -50.0)
    )
    dest = tmp_path / "tail_padding.mp4"
    _aac_from_wav(ffmpeg_bin, _write_wav(tmp_path / "tail_padding.wav", samples), dest)
    tail = _edge_frames_db(dest, from_start=False, count=4)
    assert tail[-1] <= -45.0, tail
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_out_seconds"] == 0.0, audio
    assert audio["pass"] is True, audio.get("detail")
    assert "forced edge fade" not in (audio.get("detail") or "")


def test_s2_04_afade_in_0_11_out_0_19_still_fails(tmp_path, ffmpeg_bin):
    """s2_04 render_v2 stand-in: real 0.110 / 0.190 s afade must still fail."""
    filt = (
        f"anoisesrc=d=1:c=white:r={_SR}:a={_STEADY_AMP},"
        "afade=t=in:st=0:d=0.11,"
        "afade=t=out:st=0.81:d=0.19"
    )
    dest = _mp4(ffmpeg_bin, tmp_path / "s2_04_v2.mp4", filt, 1.0)
    audio = check_loop_audio(str(dest))
    assert audio["edge_fade_in_seconds"] > _FADE_LIMIT, audio
    assert audio["edge_fade_out_seconds"] > _FADE_LIMIT, audio
    assert audio["pass"] is False
    assert "forced edge fade" in (audio.get("detail") or "")
    assert cli_main(["check-loop", str(dest)]) == 2


def test_rocket_helis_v1_style_steady_aac_loop_passes(tmp_path, ffmpeg_bin):
    """Old 04 Rocket Helis v1 stand-in: AAC program audio, no afade, must pass."""
    dest = _mp4(
        ffmpeg_bin,
        tmp_path / "rocket_helis_v1.mp4",
        f"sine=frequency=440:sample_rate={_SR}:duration=1,volume=0.3",
        1.0,
    )
    audio = check_loop_audio(str(dest))
    assert audio["pass"] is True, audio.get("detail")
    assert audio["edge_fade_in_seconds"] <= _FADE_LIMIT, audio
    assert audio["edge_fade_out_seconds"] <= _FADE_LIMIT, audio
    assert "forced edge fade" not in (audio.get("detail") or "")
    assert cli_main(["check-loop", str(dest)]) == 0
