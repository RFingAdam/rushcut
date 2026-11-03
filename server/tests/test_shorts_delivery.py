"""Shorts delivery checks: duration cap, loop wrap, report.json, fits_cap (#38)."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from rushcut.cli import main as cli_main
from rushcut.formats import fits_cap, predicted_output_seconds
from rushcut.profile import profile_from_data
from rushcut.quality import (
    check_duration_cap,
    check_frame1_not_black,
    check_loop_audio,
    check_loop_video,
    run_checks,
)
from rushcut.quality.delivery import stream_max_duration


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _clip(
    ffmpeg_bin: str,
    dest: Path,
    duration: float,
    color: str = "red",
    freq: int = 440,
    fps: int = 30,
    volume: str | None = None,
) -> Path:
    audio = f"sine=frequency={freq}:sample_rate=48000:duration={duration}"
    if volume:
        audio = f"{audio},volume={volume}"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:r={fps}:d={duration}",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(dest),
        ]
    )
    return dest


def test_duration_cap_uses_stream_max_not_format_only():
    probe = {
        "ok": True,
        "format_duration_seconds": 29.97,
        "duration_seconds": 29.97,
        "stream_durations": [30.03, 29.50],
        "stream_max_duration_seconds": 30.03,
    }
    assert stream_max_duration(probe=probe) == 30.03
    result = check_duration_cap("unused.mp4", max_seconds=30.0, probe=probe)
    assert result["id"] == "duration_cap"
    assert result["pass"] is False
    assert result["value"] == 30.03
    assert result["limit"] == 30.0


def test_duration_cap_30_03_fails_29_97_passes(tmp_path, ffmpeg_bin):
    over = _clip(ffmpeg_bin, tmp_path / "over.mp4", 30.03)
    under = _clip(ffmpeg_bin, tmp_path / "under.mp4", 29.97)
    fail = check_duration_cap(str(over), max_seconds=30.0)
    ok = check_duration_cap(str(under), max_seconds=30.0)
    assert fail["pass"] is False
    assert fail["value"] >= 30.03 - 0.05
    assert ok["pass"] is True
    assert ok["value"] < 30.0
    # Stream-max, not format duration only.
    assert fail["stream_durations"]
    assert abs(max(fail["stream_durations"]) - fail["value"]) < 1e-6


def test_loop_video_continuous_shot_passes_scene_change_fails(tmp_path, ffmpeg_bin):
    continuous = tmp_path / "loop.mp4"
    # One continuous testhue shot; append frame 0 so the wrap is the same instant.
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=160x90:r=24:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.1",
            "-filter_complex",
            "[0:v]split[a][b];[b]select=eq(n\\,0)[f];[a][f]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(continuous),
        ]
    )
    hard = tmp_path / "cut.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(hard),
        ]
    )
    good = check_loop_video(str(continuous), ssim_min=0.85)
    bad = check_loop_video(str(hard), ssim_min=0.85)
    assert good["pass"] is True
    assert good["value"] >= 0.85
    assert good.get("ssim") >= 0.85
    assert bad["pass"] is False
    assert (bad.get("ssim") or 0) < 0.85


def test_loop_audio_flags_12db_jump_and_sample_step(tmp_path, ffmpeg_bin):
    jump = tmp_path / "jump.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=24:d=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.5",
            "-filter_complex",
            "[1:a]volume=0.05[q];[2:a]volume=0.2[l];[q][l]concat=n=2:v=0:a=1[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(jump),
        ]
    )
    jumped = check_loop_audio(str(jump), audio_jump_db=6.0)
    assert jumped["pass"] is False
    assert jumped["audio_jump_db"] is not None
    assert abs(jumped["audio_jump_db"]) >= 10.0
    assert "dB" in (jumped.get("detail") or "")

    click = tmp_path / "click.mkv"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=24:d=1.0",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0:sample_rate=48000:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.8:sample_rate=48000:duration=0.5",
            "-filter_complex",
            "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "pcm_s16le",
            str(click),
        ]
    )
    stepped = check_loop_audio(str(click), audio_jump_db=24.0, sample_step=0.5)
    assert stepped["pass"] is False
    assert stepped["sample_step"] > 0.5
    assert stepped.get("audio_jump_db") is not None


def test_frame1_not_black_pass_and_fail(tmp_path, ffmpeg_bin):
    red = _clip(ffmpeg_bin, tmp_path / "red.mp4", 1.0, color="red")
    black = _clip(ffmpeg_bin, tmp_path / "black.mp4", 1.0, color="black")
    assert check_frame1_not_black(str(red))["pass"] is True
    assert check_frame1_not_black(str(black))["pass"] is False


def test_run_checks_shorts_profile_writes_report_json(tmp_path, ffmpeg_bin):
    clip = _clip(ffmpeg_bin, tmp_path / "short.mp4", 1.2, color="red")
    profile = {
        "id": "yt-shorts",
        "shorts": {"max_seconds": 30.0, "loop": True, "ssim_min": 0.85},
        "quality": {"gate": "strict", "speech_boundary": {"mode": "off"}},
    }
    result = run_checks(target=str(clip), profile=profile)
    ids = {c["id"] for c in result["checks"]}
    assert {"duration_cap", "loop_video", "loop_audio", "frame1_not_black"} <= ids
    for check in result["checks"]:
        assert "id" in check and "pass" in check and "value" in check and "limit" in check
    report_path = Path(result["report_path"])
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["checks"]
    for check in report["checks"]:
        assert set(check) >= {"id", "pass", "value", "limit"}
    assert cli_main(["check", str(clip), "--json"]) in (0, 2)


def test_render_with_preset_gates_on_shorts_profile(fake_session, tmp_path, ffmpeg_bin):
    from rushcut import tools

    over = _clip(ffmpeg_bin, tmp_path / "over.mp4", 30.03)
    out = tmp_path / "out.mp4"
    out.write_bytes(over.read_bytes())

    def handler(method, params):
        if method == "render.list_templates":
            return {
                "ok": True,
                "templates": [
                    {
                        "renderer": "MAGIX AVC/AAC MP4",
                        "renderer_name": "MAGIX AVC/AAC MP4",
                        "template": "Vertical 1080x1920",
                        "width": 1080,
                        "height": 1920,
                    }
                ],
            }
        if method == "render.start":
            return {"ok": True, "job_id": "s1", "state": "running", "async": True}
        if method == "render.status":
            return {"ok": True, "job_id": "s1", "state": "complete", "percent": 100}
        raise AssertionError(method)

    fake_session(handler)
    result = tools.render_with_preset(
        str(out),
        "vertical",
        edit_meta={
            "profile": {
                "id": "shorts",
                "shorts": {"max_seconds": 30.0, "loop": True},
                "quality": {"gate": "strict", "speech_boundary": {"mode": "off"}},
            }
        },
        wait=True,
    )
    assert result["ok"] is False
    ids = {c["id"] for c in (result["quality"] or {}).get("checks") or []}
    assert "duration_cap" in ids
    cap = next(c for c in result["quality"]["checks"] if c["id"] == "duration_cap")
    assert cap["pass"] is False
    assert (tmp_path / "report.json").is_file()


def test_fits_cap_speed_ramps_within_one_frame(tmp_path, ffmpeg_bin):
    spec = {
        "fps": 30,
        "max_seconds": 30.0,
        "segments": [
            {"start_seconds": 0, "end_seconds": 10, "speed": 1.0},
            {"start_seconds": 10, "end_seconds": 20, "speed": 2.0},
            {
                "start_seconds": 0,
                "end_seconds": 6,
                "speed_ramp": [{"at": 0, "speed": 1.0}, {"at": 6, "speed": 2.0}],
            },
        ],
    }
    expected = 10.0 + 5.0 + (6.0 * math.log(2.0))
    predicted = predicted_output_seconds(spec)
    assert abs(predicted - expected) <= 1.0 / 30.0
    result = fits_cap(spec)
    assert result["ok"] is True
    assert result["fits"] is True
    assert abs(result["predicted_seconds"] - expected) <= result["frame_seconds"]

    src = _clip(ffmpeg_bin, tmp_path / "src.mp4", 2.0, fps=24)
    sped = tmp_path / "sped.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(src),
            "-filter:v",
            "setpts=PTS/2",
            "-filter:a",
            "atempo=2.0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(sped),
        ]
    )
    from rushcut.analysis.probe import probe_media

    actual = float(probe_media(str(sped))["duration_seconds"])
    plan = fits_cap(
        {"segments": [{"start_seconds": 0, "end_seconds": 2.0, "speed": 2.0}], "fps": 24},
        max_seconds=30.0,
        fps=24,
    )
    assert abs(plan["predicted_seconds"] - actual) <= (1.0 / 24.0) + 0.05
    assert plan["fits"] is True
    over = fits_cap(
        {"segments": [{"start_seconds": 0, "end_seconds": 40, "speed": 1.0}]},
        max_seconds=30.0,
        fps=30,
    )
    assert over["fits"] is False
    profile = profile_from_data({"id": "shorts", "shorts": {"max_seconds": 30.0, "loop": True}})
    assert profile.shorts.max_seconds == 30.0
    assert profile.shorts.loop is True
    assert cli_main(["fits-cap", json.dumps(spec["segments"]), "--max", "30", "--fps", "30"]) == 0
