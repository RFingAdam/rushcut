"""Caption-burn timeouts and concat quality (#52)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from rushcut.analysis.captions import burn_captions, generate_captions
from rushcut.analysis.edit_ops import concat_media
from rushcut.analysis.ffmpeg_util import encode_timeout_seconds
from rushcut.analysis.probe import probe_media, streams_compatible
from rushcut.quality import run_checks
from rushcut.render.sidecar import verify_output

LONGFORM_SECONDS = 14 * 60
LONGFORM_FPS = 60.0
FRAME = 1.0 / LONGFORM_FPS


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _seed_1080p60(ffmpeg_bin: str, dest: Path, duration: float = 1.0, color: str = "blue") -> Path:
    _run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1920x1080:r=60:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-r",
            "60",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "60",
            "-x264-params",
            "keyint=60:min-keyint=60:scenecut=0",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            str(dest),
        ]
    )
    return dest


def _loop_copy(ffmpeg_bin: str, src: Path, dest: Path, seconds: float) -> Path:
    """Repeat *src* with the concat demuxer so the result stays CFR / copyable."""
    src_probe = probe_media(str(src))
    piece = float(src_probe.get("video_duration_seconds") or src_probe.get("duration_seconds") or 1.0)
    n = max(1, int(round(float(seconds) / piece)))
    listing = dest.with_suffix(".concat.txt")
    escaped = str(src.resolve()).replace("\\", "/").replace("'", r"'\''")
    listing.write_text("".join(f"file '{escaped}'\n" for _ in range(n)), encoding="utf-8")
    _run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(dest),
        ]
    )
    return dest


def test_encode_timeout_scales_past_300s_for_14min_1080p60():
    medium = encode_timeout_seconds(
        LONGFORM_SECONDS, width=1920, height=1080, fps=60, preset="medium"
    )
    ultra = encode_timeout_seconds(
        LONGFORM_SECONDS, width=1920, height=1080, fps=60, preset="ultrafast"
    )
    copied = encode_timeout_seconds(LONGFORM_SECONDS, copy=True)
    assert medium > 300
    assert ultra > 300
    assert copied >= 60
    assert medium > ultra
    assert encode_timeout_seconds(2, width=320, height=180, fps=24, override=12) == 12


def test_concat_stream_copy_matching_codecs(tmp_path, ffmpeg_bin):
    seed = _seed_1080p60(ffmpeg_bin, tmp_path / "seed.mp4", duration=1.0)
    a = _loop_copy(ffmpeg_bin, seed, tmp_path / "a.mp4", 2.0)
    b = _loop_copy(ffmpeg_bin, seed, tmp_path / "b.mp4", 2.0)
    out = tmp_path / "cat.mp4"
    result = concat_media([str(a), str(b)], str(out))
    assert result["ok"] is True, result
    assert result["stream_copy"] is True
    assert result["preset"] is None
    src = probe_media(str(a))
    other = probe_media(str(b))
    dest = probe_media(str(out))
    assert dest["video_codec"] == src["video_codec"]
    assert dest["audio_codec"] == src["audio_codec"]
    expected = float(src.get("video_duration_seconds") or src["duration_seconds"]) + float(
        other.get("video_duration_seconds") or other["duration_seconds"]
    )
    fps = float(dest.get("fps") or 60)
    got = float(dest.get("video_duration_seconds") or dest["duration_seconds"])
    frames = dest.get("nb_frames")
    expected_frames = round(expected * fps)
    if frames is not None:
        assert abs(int(frames) - expected_frames) <= 1
    else:
        assert abs(got - expected) <= (1.0 / fps) + 1e-3


def test_concat_quality_preset_when_codecs_differ(tmp_path, ffmpeg_bin):
    hd = tmp_path / "hd.mp4"
    sd = tmp_path / "sd.mp4"
    _seed_1080p60(ffmpeg_bin, hd, duration=0.5, color="red")
    _run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=640x360:r=30:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.5",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(sd),
        ]
    )
    assert streams_compatible([probe_media(str(hd)), probe_media(str(sd))]) is False
    out = tmp_path / "mixed.mp4"
    result = concat_media([str(hd), str(sd)], str(out), preset="medium", crf=23)
    assert result["ok"] is True, result
    assert result["stream_copy"] is False
    assert result["preset"] == "medium"
    assert result["crf"] == 23
    assert probe_media(str(out))["ok"] is True


def test_burn_captions_reports_preset_crf_and_scaled_timeout(tmp_path, ffmpeg_bin, color_clip):
    caps = generate_captions(
        path=str(color_clip),
        segments=[{"start_seconds": 0.0, "end_seconds": 1.0, "text": "hello"}],
        output_dir=str(tmp_path),
        style="default",
        group_words=False,
    )
    assert caps["ok"] is True
    burned = tmp_path / "burned.mp4"
    result = burn_captions(
        str(color_clip),
        caps["ass_path"],
        str(burned),
        preset="ultrafast",
        crf=28,
    )
    assert result["ok"] is True, result
    assert result["preset"] == "ultrafast"
    assert result["crf"] == 28
    assert result["timeout_seconds"] > 60
    probe = probe_media(str(color_clip))
    default_timeout = encode_timeout_seconds(
        probe.get("duration_seconds"),
        width=probe.get("width"),
        height=probe.get("height"),
        fps=probe.get("fps"),
        preset="medium",
    )
    assert default_timeout != 300


def test_14min_1080p60_burn_and_concat_without_timeout(tmp_path, ffmpeg_bin):
    """Acceptance: 14 min 1080p60 burns and concatenates; check duration + stream-copy."""
    seed = _seed_1080p60(ffmpeg_bin, tmp_path / "seed.mp4", duration=1.0)
    half = LONGFORM_SECONDS / 2.0
    part_a = _loop_copy(ffmpeg_bin, seed, tmp_path / "part_a.mp4", half)
    part_b = _loop_copy(ffmpeg_bin, seed, tmp_path / "part_b.mp4", half)
    assert streams_compatible([probe_media(str(part_a)), probe_media(str(part_b))]) is True

    joined = tmp_path / "program.mp4"
    cat = concat_media([str(part_a), str(part_b)], str(joined))
    assert cat["ok"] is True, cat
    assert cat["stream_copy"] is True
    assert cat.get("stream_copy_fallback") is None
    assert cat["timeout_seconds"] != 300
    assert encode_timeout_seconds(
        LONGFORM_SECONDS, width=1920, height=1080, fps=60, preset="medium"
    ) > 300
    joined_probe = probe_media(str(joined))
    assert joined_probe["ok"] is True
    assert joined_probe["width"] == 1920
    assert joined_probe["height"] == 1080
    assert abs(float(joined_probe["fps"] or 0) - 60) < 0.05
    joined_video = float(joined_probe.get("video_duration_seconds") or joined_probe["duration_seconds"])
    joined_frames = joined_probe.get("nb_frames")
    expected_frames = int(round(LONGFORM_SECONDS * LONGFORM_FPS))
    if joined_frames is not None:
        assert abs(int(joined_frames) - expected_frames) <= 1
    else:
        assert abs(joined_video - LONGFORM_SECONDS) <= FRAME + 1e-3
    seed_probe = probe_media(str(seed))
    assert joined_probe["video_codec"] == seed_probe["video_codec"]
    assert joined_probe["audio_codec"] == seed_probe["audio_codec"]

    checked_cat = run_checks(target=str(joined))
    assert checked_cat["ok"] is True, checked_cat
    verified_cat = verify_output(
        str(joined),
        expect_audio=True,
        expect_width=1920,
        expect_height=1080,
        expect_duration=joined_video,
        duration_tolerance=0.05,
    )
    assert verified_cat["ok"] is True, verified_cat

    caps = generate_captions(
        segments=[{"start_seconds": 0.0, "end_seconds": 2.0, "text": "long form"}],
        output_dir=str(tmp_path),
        stem="longform",
        style="default",
        group_words=False,
    )
    assert caps["ok"] is True
    burned = tmp_path / "captioned.mp4"
    default_burn_timeout = encode_timeout_seconds(
        LONGFORM_SECONDS, width=1920, height=1080, fps=60, preset="medium"
    )
    assert default_burn_timeout > 300
    burned_result = burn_captions(
        str(joined),
        caps["ass_path"],
        str(burned),
        preset="ultrafast",
        crf=28,
    )
    assert burned_result["ok"] is True, burned_result
    assert burned_result["timeout_seconds"] > 300
    assert burned_result["preset"] == "ultrafast"
    burned_probe = probe_media(str(burned))
    burned_video = float(burned_probe.get("video_duration_seconds") or burned_probe["duration_seconds"])
    burned_frames = burned_probe.get("nb_frames")
    source_frames = joined_frames if joined_frames is not None else expected_frames
    if burned_frames is not None:
        assert abs(int(burned_frames) - int(source_frames)) <= 1
        assert abs(int(burned_frames) - expected_frames) <= 1
    else:
        assert abs(burned_video - joined_video) <= FRAME + 1e-3
        assert abs(burned_video - LONGFORM_SECONDS) <= FRAME + 1e-3
    checked_burn = run_checks(target=str(burned))
    assert checked_burn["ok"] is True, checked_burn
    verified_burn = verify_output(
        str(burned),
        expect_audio=True,
        expect_width=1920,
        expect_height=1080,
        expect_duration=burned_video,
        duration_tolerance=0.05,
    )
    assert verified_burn["ok"] is True, verified_burn
