"""check-loop edge-fade must flag a silence ramp, not a later onset (#60)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from rushcut.cli import main as cli_main
from rushcut.quality.loop import check_loop, check_loop_audio

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
