"""Synthetic-fixture tests for stitch_session, region presence, check, check_media."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rushcut.analysis.loudness import measure_loudness
from rushcut.analysis.probe import probe_media
from rushcut.analysis.region import detect_region_presence
from rushcut.analysis.session import lookup_session_time, repair_media, stitch_session
from rushcut.cli import main as cli_main
from rushcut.quality import check_loop, check_media, check_must_include, run_checks
from rushcut.quality.media import output_join_times


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _clip(ffmpeg_bin: str, dest: Path, duration: float, color: str = "red", freq: int = 440) -> Path:
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:r=24:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dest),
        ]
    )
    return dest


def test_stitch_session_four_files_offsets_round_trip(tmp_path, ffmpeg_bin):
    clips = []
    for i in range(4):
        clips.append(
            _clip(
                ffmpeg_bin,
                tmp_path / f"2026-11-01_12-0{i}-00.mp4",
                0.8,
                color=["red", "green", "blue", "yellow"][i],
                freq=220 * (i + 1),
            )
        )
    out = tmp_path / "session.mp4"
    result = stitch_session([str(p) for p in clips], gap_threshold_s=600, output_path=str(out))
    assert result["ok"] is True
    assert result["session_count"] == 1
    offset_map = result["offset_map"]
    assert len(offset_map) == 4
    summed = sum(row["duration_seconds"] for row in offset_map)
    measured = float(result["duration_seconds"] or 0)
    assert abs(measured - summed) <= 1.0
    assert 2.5 <= measured <= 4.2
    # Offsets round-trip: session time at each file start maps back to that file at 0.
    for row in offset_map:
        hit = lookup_session_time(offset_map, row["offset_seconds"] + 0.05)
        assert hit["ok"] is True
        assert hit["path"] == row["path"]
        assert abs(hit["file_seconds"] - 0.05) < 0.08
        assert abs(hit["offset_seconds"] + hit["file_seconds"] - (row["offset_seconds"] + 0.05)) < 1e-6
    code = cli_main(
        ["stitch-session", *[str(p) for p in clips], "--gap", "600", "--output", str(tmp_path / "cli.mp4")]
    )
    assert code == 0


def test_stitch_session_gap_splits_sessions(tmp_path, ffmpeg_bin):
    a = _clip(ffmpeg_bin, tmp_path / "2026-11-01_12-00-00.mp4", 0.6)
    b = _clip(ffmpeg_bin, tmp_path / "2026-11-01_18-00-00.mp4", 0.6)
    result = stitch_session([str(a), str(b)], gap_threshold_s=60)
    assert result["ok"] is True
    assert result["session_count"] == 2


def test_repair_media_copy_does_not_touch_source(tmp_path, ffmpeg_bin, color_clip):
    src = tmp_path / "src.mkv"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(color_clip),
            "-c",
            "copy",
            str(src),
        ]
    )
    before = src.read_bytes()
    repaired = repair_media(str(src), str(tmp_path / "out.mkv"))
    assert repaired["ok"] is True
    assert src.read_bytes() == before
    probe = probe_media(repaired["output_path"])
    assert probe["ok"] is True
    assert probe["duration_seconds"] > 1.0


def test_detect_region_presence_box_then_absent(tmp_path, ffmpeg_bin):
    dest = tmp_path / "cam.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=8:d=4",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=80x80:r=8:d=2",
            "-filter_complex",
            "[0:v][1:v]overlay=240:0:shortest=0:enable='lt(t,2)'[v]",
            "-map",
            "[v]",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-t",
            "4",
            str(dest),
        ]
    )
    result = detect_region_presence(
        str(dest),
        {"x": 240, "y": 0, "w": 80, "h": 80},
        mode="activity",
        sample_fps=1,
    )
    assert result["ok"] is True
    by_t = {round(s["t"]): s["present"] for s in result["samples"]}
    assert by_t.get(0) is True or by_t.get(1) is True
    assert by_t.get(3) is False
    assert result["cpu_fallback"] is True


def test_must_include_two_events_fail_when_cut_away():
    cuts = [
        {"start_seconds": 0.0, "end_seconds": 10.0},
        {"start_seconds": 40.0, "end_seconds": 50.0},
    ]
    events = [
        {"t": 12.0, "pre": 1.0, "post": 1.0, "id": "event_a"},
        {"t": 20.0, "pre": 0.5, "post": 0.5, "id": "event_b"},
    ]
    result = check_must_include(cuts, events)
    assert result["pass"] is False
    assert {m["id"] for m in result["missing"]} == {"event_a", "event_b"}
    kept = check_must_include(cuts, [{"t": 5.0, "pre": 1, "post": 1, "id": "hook"}])
    assert kept["pass"] is True
    report = run_checks(cuts=cuts, must_include=events, mode="strict")
    assert report["ok"] is False
    ids = {c["id"] for c in report["checks"]}
    assert "must_include" in ids


def test_check_loop_fade_to_black_fails(tmp_path, ffmpeg_bin):
    dest = tmp_path / "fade.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:r=24:d=1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=24:d=0.6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.6",
            "-filter_complex",
            "[0:v][2:v]concat=n=2:v=1:a=0[v];[1:a][3:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dest),
        ]
    )
    result = check_loop(str(dest), ssim_min=0.85)
    assert result["ok"] is True
    assert result["ssim"] is not None
    assert result["pass"] is False
    assert result["video_pass"] is False
    code = cli_main(["check-loop", str(dest)])
    assert code == 2


def test_check_loop_does_not_require_or_force_a_fade(tmp_path, ffmpeg_bin):
    """Seamless loops keep level through the wrap; a forced fade fails."""
    dest = _clip(ffmpeg_bin, tmp_path / "hold.mp4", 1.5, color="red", freq=440)
    result = check_loop(str(dest), ssim_min=0.85, audio_jump_db=6.0)
    assert result["ok"] is True
    assert result["pass"] is True
    assert (result.get("edge_fade_in_seconds") or 0) <= 0.08
    assert (result.get("edge_fade_out_seconds") or 0) <= 0.08


def test_check_loop_constant_color_passes(tmp_path, ffmpeg_bin):
    dest = _clip(ffmpeg_bin, tmp_path / "hold.mp4", 1.5, color="red", freq=440)
    result = check_loop(str(dest), ssim_min=0.85, audio_jump_db=6.0)
    assert result["ok"] is True
    assert result["ssim"] is not None
    assert result["ssim"] >= 0.85
    assert result["video_pass"] is True
    assert result["pass"] is True


def test_check_media_loudness_and_chopped_word(tmp_path, ffmpeg_bin, color_clip):
    tone = tmp_path / "tone.wav"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=3",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            str(tone),
        ]
    )
    loud = measure_loudness(str(tone))
    assert loud["ok"] is True
    assert loud["integrated_lufs"] is not None
    assert abs(loud["integrated_lufs"] - (-16.0)) <= 1.5
    again = measure_loudness(str(tone))
    assert abs(again["integrated_lufs"] - loud["integrated_lufs"]) <= 0.2

    words = [
        {"start_seconds": 0.8, "end_seconds": 1.2, "word": "chopped"},
        {"start_seconds": 2.0, "end_seconds": 2.3, "word": "later"},
    ]
    cuts = [
        {"start_seconds": 0.0, "end_seconds": 1.0},
        {"start_seconds": 5.0, "end_seconds": 6.0},
    ]
    assert output_join_times(cuts) == [1.0]
    result = check_media(str(color_clip), cuts=cuts, words=words, transcribe=False)
    assert result["duration_seconds"]
    assert result["fps"]
    assert result["chopped_word_count"] >= 1
    assert result["ok"] is False
    clean = check_media(
        str(color_clip),
        cuts=cuts,
        words=[{"start_seconds": 0.1, "end_seconds": 0.4, "word": "hello"}],
        transcribe=False,
    )
    assert clean["chopped_word_count"] == 0


def test_cut_media_accepts_session_time(tmp_path, ffmpeg_bin):
    from rushcut.analysis.edit_ops import cut_media

    a = _clip(ffmpeg_bin, tmp_path / "a.mp4", 1.0, color="red")
    b = _clip(ffmpeg_bin, tmp_path / "b.mp4", 1.0, color="blue")
    stitched = stitch_session([str(a), str(b)], output_path=str(tmp_path / "all.mp4"))
    offset_map = stitched["offset_map"]
    out = tmp_path / "piece.mp4"
    cut = cut_media(
        offset_map=offset_map,
        session_start=0.2,
        session_end=0.7,
        output_path=str(out),
    )
    assert cut["ok"] is True
    probe = probe_media(str(out))
    assert 0.3 <= probe["duration_seconds"] <= 0.8


def test_cli_check_must_include_and_media_flags(tmp_path):
    events = tmp_path / "events.json"
    events.write_text(
        json.dumps([{"t": 12, "pre": 1, "post": 1, "id": "event_a"}]),
        encoding="utf-8",
    )
    cuts = tmp_path / "cuts.json"
    cuts.write_text(
        json.dumps([{"start_seconds": 0, "end_seconds": 5}]),
        encoding="utf-8",
    )
    code = cli_main(["check", "--cuts", str(cuts), "--must-include", str(events)])
    assert code == 2
