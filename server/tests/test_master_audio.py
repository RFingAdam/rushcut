"""Two-pass loudnorm mastering and encode-aware true peak."""
from __future__ import annotations

import subprocess
from pathlib import Path

from rushcut.analysis.loudness import master_audio, measure_loudness, measure_true_peak_4x
from rushcut.render.presets import RENDER_PRESETS


def _tone(ffmpeg_bin: str, dest: Path, seconds: float, freq: float = 440, amp: str = "0.25") -> Path:
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
            "-af",
            f"volume={amp}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def test_master_audio_20s_within_half_lu(tmp_path, ffmpeg_bin):
    src = _tone(ffmpeg_bin, tmp_path / "20s.wav", 20.0, amp="0.08")
    out = tmp_path / "20s.master.wav"
    report = master_audio(str(src), str(out), target_i=-14, target_tp=-1.5, encode_aware=True)
    assert report["ok"] is True, report
    assert report["output_path"]
    assert "I" in report["input"] and "I" in report["output"]
    assert "TP" in report["input"] and "TP" in report["output"]
    assert "LRA" in report["input"] and "LRA" in report["output"]
    assert "iterations" in report
    loud = measure_loudness(str(out))
    assert loud["ok"] is True
    assert abs(loud["integrated_lufs"] - (-14.0)) <= 0.5, loud
    aac_tp = report.get("decoded_aac_true_peak_dbtp")
    assert aac_tp is not None
    assert aac_tp <= -1.5 + 0.4, report


def test_master_audio_10min_within_half_lu(tmp_path, ffmpeg_bin):
    src = _tone(ffmpeg_bin, tmp_path / "10min.wav", 600.0, amp="0.05")
    out = tmp_path / "10min.master.wav"
    report = master_audio(str(src), str(out), target_i=-14, target_tp=-1.5, encode_aware=False)
    assert report["ok"] is True, report
    loud = measure_loudness(str(out))
    assert abs(loud["integrated_lufs"] - (-14.0)) <= 0.5, loud


def test_master_audio_copies_video_byte_identical(tmp_path, ffmpeg_bin):
    src = tmp_path / "av.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = tmp_path / "av.master.mp4"
    report = master_audio(str(src), str(out), target_i=-14, target_tp=-1.5, encode_aware=True)
    assert report["ok"] is True, report
    assert report["video_copied"] is True
    assert report["video_md5_in"]
    assert report["video_md5_in"] == report["video_md5_out"]


def test_decoded_aac_true_peak_slack(tmp_path, ffmpeg_bin):
    # Hot sine so AAC encode wants to overshoot without the TP loop.
    src = _tone(ffmpeg_bin, tmp_path / "hot.wav", 8.0, amp="0.95")
    out = tmp_path / "hot.master.wav"
    report = master_audio(str(src), str(out), target_i=-14, target_tp=-1.5, encode_aware=True)
    assert report["ok"] is True, report
    tp = measure_true_peak_4x(str(out))
    assert tp["ok"] is True
    # Delivery check is on the encoded+decoded AAC, not the WAV.
    assert report["decoded_aac_true_peak_dbtp"] <= -1.5 + 0.4


def test_render_preset_master_flag(monkeypatch, tmp_path, ffmpeg_bin, color_clip):
    assert "master" in RENDER_PRESETS["vertical"]
    calls: list[str] = []

    def fake_master(path, output_path=None, **_k):
        calls.append(path)
        return {
            "ok": True,
            "output_path": output_path or path,
            "input": {"I": -18, "TP": -1.0, "LRA": 4},
            "output": {"I": -14, "TP": -1.5, "LRA": 4},
            "iterations": 1,
        }

    monkeypatch.setenv("DIRECTOR_EDITOR", "ffmpeg")
    from rushcut.backends import reset_backend
    from rushcut.backends.ffmpeg import FfmpegBackend
    from rushcut import tools

    reset_backend(FfmpegBackend())
    monkeypatch.setattr("rushcut.analysis.loudness.master_audio", fake_master)
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(320, 180, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 0.8)
    out = tmp_path / "preset.mp4"
    result = tools.render_with_preset(str(out), "vertical", wait=True, master=True)
    assert result.get("started") or result.get("ok")
    assert calls, "master_audio should run when master=True"
    assert result.get("master", {}).get("ok") is True
    reset_backend()
