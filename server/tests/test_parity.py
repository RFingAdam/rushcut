"""Layout-parity gate (#51). Fixtures are synthetic stand-ins for the private cuts."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from rushcut.cli import main as cli_main
from rushcut.quality.parity import (
    check_parity,
    default_regions,
    mean_abs_diff,
    parse_edit_region,
    parse_region,
    parse_time_map_segment,
    sample_times,
    ssim_luma,
    validate_time_map,
)


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _h264(
    ffmpeg_bin: str,
    dest: Path,
    *,
    duration: float,
    vf: str | None = None,
    size: str = "1080x1920",
    fps: int = 30,
    crf: int = 12,
    extra_inputs: list[str] | None = None,
    audio: bool = True,
    copy_audio_from: Path | None = None,
) -> Path:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={size}:r={fps}:d={duration}",
    ]
    if extra_inputs:
        cmd.extend(extra_inputs)
    if audio and copy_audio_from is None:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
        ]
    audio_index = 1 + (sum(1 for token in extra_inputs if token == "-i") if extra_inputs else 0)
    if vf and "[" in vf:
        cmd += ["-filter_complex", vf, "-map", "[v]"]
        if audio and copy_audio_from is None:
            cmd += ["-map", f"{audio_index}:a:0"]
    elif vf:
        cmd += ["-vf", vf]
    cmd += [
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        str(crf),
    ]
    if copy_audio_from is not None:
        cmd += ["-i", str(copy_audio_from), "-c:a", "copy", "-map", "0:v:0", "-map", "1:a:0"]
    elif audio:
        cmd += ["-c:a", "aac", "-ac", "1", "-ar", "48000", "-shortest"]
    else:
        cmd += ["-an"]
    cmd.append(str(dest))
    _ffmpeg(cmd)
    return dest


def _approved_short_vf(card_color: str = "0x1c2a44", fill_top: str | None = None) -> str:
    parts = [
        "[1:v]split=2[src][src2]",
        "[src]crop=1080:1130:0:0[game]",
        "[src2]crop=240:240:40:40[cam]",
        "[0:v]drawbox=0:470:1080:120:c=0x3a4555:t=fill[hud]",
        "[hud][game]overlay=0:590[mid]",
        "[mid][cam]overlay=820:1450[laid]",
        "[laid]drawbox=0:1720:1080:200:c=black:t=fill[banded]",
    ]
    src = "banded"
    if fill_top:
        parts.append(f"[{src}]drawbox=0:0:1080:470:c={fill_top}:t=fill[filled]")
        src = "filled"
    parts.append(
        f"[{src}]drawbox=12:34:1056:420:c={card_color}:t=fill:enable='lt(n\\,105)'[v]"
    )
    return ";".join(parts)


def _short_pair(tmp_path: Path, ffmpeg_bin: str, duration: float = 5.0) -> tuple[Path, Path, Path]:
    extras = ["-f", "lavfi", "-i", f"testsrc=s=1080x1920:r=30:d={duration}"]
    approved = _h264(
        ffmpeg_bin,
        tmp_path / "approved_v5.mp4",
        duration=duration,
        extra_inputs=extras,
        vf=_approved_short_vf(),
    )
    rejected = _h264(
        ffmpeg_bin,
        tmp_path / "rejected_v6.mp4",
        duration=duration,
        extra_inputs=extras,
        vf=_approved_short_vf(card_color="0x334466", fill_top="0x88aacc"),
    )
    passing = tmp_path / "pass_v8.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(approved),
            "-vf",
            "drawbox=12:34:1056:420:c=0x334466:t=fill:enable='lt(n\\,105)'",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-c:a",
            "copy",
            str(passing),
        ]
    )
    return approved, rejected, passing


SHORT_REGIONS = [
    "top_band=0,0,1080,470",
    "hud=0,470,1080,590",
    "gameplay=0,590,1080,1920",
]
CARD_EDIT = "12,34,1068,454,0,3.5"


def test_ssim_identical_and_mad_scale():
    a = np.full((32, 32), 40, dtype=np.uint8)
    assert ssim_luma(a, a) == 1.0
    assert mean_abs_diff(a, a) == 0.0
    b = np.clip(a.astype(np.int16) + 10, 0, 255).astype(np.uint8)
    assert abs(mean_abs_diff(a, b) - 10.0) < 1e-6
    assert ssim_luma(a, b) < 1.0


def test_parsers_and_default_bands():
    region = parse_region("top_band=0,0,1080,470")
    assert region["name"] == "top_band"
    assert region["y1"] == 470
    edit = parse_edit_region("12,34,1068,454,0,3.5")
    assert edit["t1"] == 3.5
    seg = parse_time_map_segment("16.8,18.033,2.8")
    assert seg["offset"] == 2.8
    bands = default_regions(1080, 1920)
    assert [b["name"] for b in bands] == ["top", "mid", "bottom", "full"]
    assert bands[0]["y1"] == 640
    times = sample_times(5.0, 24, 30.0)
    assert len(times) == 24
    assert times[0] == 0.0
    assert any(t > 3.5 for t in times)


def test_time_map_validation_rejects_overlap():
    try:
        validate_time_map(
            [{"cand_t0": 0, "cand_t1": 2, "offset": 0}, {"cand_t0": 1.5, "cand_t1": 3, "offset": 0}],
            candidate_duration=3.0,
            approved_duration=3.0,
            fps=30,
        )
    except ValueError as exc:
        assert "monotonic" in str(exc)
    else:
        raise AssertionError("expected overlap to fail")


def test_samples_minimum_and_missing_files(tmp_path):
    missing = check_parity(str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4"))
    assert missing["ok"] is False
    assert "not found" in missing["error"]
    low = check_parity("a", "b", samples=12)
    assert low["ok"] is False
    assert "20" in low["error"]


def test_card_fix_fails_after_edit_window(tmp_path, ffmpeg_bin):
    approved, rejected, _passing = _short_pair(tmp_path, ffmpeg_bin)
    result = check_parity(
        str(approved),
        str(rejected),
        edit_regions=[CARD_EDIT],
        regions=SHORT_REGIONS,
        samples=24,
    )
    assert result["ok"] is False
    failing_names = {item["region"] for item in result["failures"] if item.get("region")}
    assert "top_band" in failing_names
    assert any(item["t"] > 3.5 and item.get("region") == "top_band" for item in result["failures"])
    top = result["regions"]["top_band"]
    assert top["pass"] is False
    assert top["worst_mad"] is not None and top["worst_mad"] >= 20
    hud = result["regions"]["hud"]
    assert hud["worst_mad"] is not None and hud["worst_mad"] <= 2.0
    gameplay = result["regions"]["gameplay"]
    assert gameplay["worst_mad"] is not None and gameplay["worst_mad"] <= 2.0


def test_opaque_card_rerender_passes(tmp_path, ffmpeg_bin):
    approved, _rejected, passing = _short_pair(tmp_path, ffmpeg_bin)
    result = check_parity(
        str(approved),
        str(passing),
        edit_regions=[CARD_EDIT],
        regions=SHORT_REGIONS,
        samples=24,
    )
    assert result["ok"] is True, result["failures"][:8]
    worst_ssim = min(
        info["worst_ssim"]
        for info in result["regions"].values()
        if info["worst_ssim"] is not None
    )
    worst_mad = max(
        info["worst_mad"] for info in result["regions"].values() if info["worst_mad"] is not None
    )
    assert worst_ssim >= 0.95
    assert worst_mad <= 3.0


def test_reencode_crf16_passes(tmp_path, ffmpeg_bin):
    approved, _rejected, _passing = _short_pair(tmp_path, ffmpeg_bin)
    reenc = tmp_path / "reenc.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(approved),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "16",
            "-c:a",
            "copy",
            str(reenc),
        ]
    )
    result = check_parity(
        str(approved),
        str(reenc),
        regions=SHORT_REGIONS,
        samples=24,
    )
    assert result["ok"] is True, result["failures"][:8]
    for info in result["regions"].values():
        if info["worst_ssim"] is not None:
            assert info["worst_ssim"] >= 0.98
        if info["worst_mad"] is not None:
            assert info["worst_mad"] <= 1.5


def test_band_fill_fails_keep_facecam(tmp_path, ffmpeg_bin):
    extras = ["-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30:d=5"]
    approved = _h264(
        ffmpeg_bin,
        tmp_path / "cam_approved.mp4",
        duration=5.0,
        extra_inputs=extras,
        vf=_approved_short_vf(),
    )
    filled = tmp_path / "cam_blurfill.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(approved),
            "-vf",
            (
                "drawbox=0:0:1080:470:c=0xccaa44:t=fill:enable='gte(n\\,105)',"
                "drawbox=0:1720:1080:200:c=0xbb9944:t=fill:enable='gte(n\\,105)'"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-c:a",
            "copy",
            str(filled),
        ]
    )
    result = check_parity(
        str(approved),
        str(filled),
        edit_regions=[CARD_EDIT],
        regions=[
            "top_band=0,0,1080,470",
            "bottom_band=0,1720,1080,1920",
            "facecam=820,1450,1060,1690",
        ],
        presence_region="facecam",
        samples=24,
    )
    assert result["ok"] is False
    assert result["regions"]["top_band"]["worst_ssim"] < 0.2
    assert result["regions"]["bottom_band"]["worst_ssim"] < 0.4
    assert result["regions"]["facecam"]["pass"] is True
    assert result["regions"]["facecam"]["worst_ssim"] >= 0.97


def _color_clip(
    ffmpeg_bin: str,
    dest: Path,
    duration: float,
    color: str,
    fps: int = 30,
    size: str = "160x90",
) -> Path:
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r={fps}:d={duration}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-an",
            str(dest),
        ]
    )
    return dest


def test_trim_time_map_and_seek_tolerance(tmp_path, ffmpeg_bin):
    a = _color_clip(ffmpeg_bin, tmp_path / "a.mp4", 3.0, "red")
    b = _color_clip(ffmpeg_bin, tmp_path / "b.mp4", 0.8, "green")
    c = _color_clip(ffmpeg_bin, tmp_path / "c.mp4", 3.0, "blue")
    approved = tmp_path / "approved_trim.mp4"
    candidate = tmp_path / "candidate_trim.mp4"
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(a),
            "-i",
            str(b),
            "-i",
            str(c),
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            str(approved),
        ]
    )
    _ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(a),
            "-i",
            str(c),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            str(candidate),
        ]
    )
    good_map = ["0,3.0,0", "3.0,6.0,0.8"]
    ok = check_parity(str(approved), str(candidate), time_map=good_map, samples=20)
    assert ok["ok"] is True, ok["failures"][:8]

    bad_map = ["0,3.0,0", "3.0,6.0,0.767"]
    fail = check_parity(str(approved), str(candidate), time_map=bad_map, samples=20)
    assert fail["ok"] is False
    assert fail["failures"]
    seam = [item for item in fail["failures"] if abs(item["t"] - 3.0) < 0.05]
    assert seam, fail["failures"][:8]

    tolerated = check_parity(
        str(approved),
        str(candidate),
        time_map=bad_map,
        samples=20,
        seek_tolerance=1,
    )
    assert tolerated["ok"] is True, tolerated["failures"][:8]


def test_no_counterpart_and_allow_new(tmp_path, ffmpeg_bin):
    approved = _color_clip(ffmpeg_bin, tmp_path / "short.mp4", 2.0, "red")
    candidate = _color_clip(ffmpeg_bin, tmp_path / "long.mp4", 3.0, "red")
    fail = check_parity(str(approved), str(candidate), samples=20)
    assert fail["ok"] is False
    assert fail["no_counterpart"]
    assert any(item["reason"] == "no_counterpart" for item in fail["failures"])
    ok = check_parity(str(approved), str(candidate), samples=20, allow_new=1.05)
    assert ok["ok"] is True, ok["failures"][:8]


def test_presence_region_missing_facecam(tmp_path, ffmpeg_bin):
    extras = ["-f", "lavfi", "-i", "testsrc=s=160x90:r=30:d=2"]
    approved = _h264(
        ffmpeg_bin,
        tmp_path / "cam_on.mp4",
        duration=2.0,
        size="160x90",
        extra_inputs=extras,
        vf="[0:v][1:v]overlay=10:10[v]",
        audio=False,
    )
    candidate = _color_clip(ffmpeg_bin, tmp_path / "cam_off.mp4", 2.0, "black", size="160x90")
    result = check_parity(
        str(approved),
        str(candidate),
        regions=["facecam=10,10,80,80"],
        presence_region="facecam",
        samples=20,
        ssim_min=0.0,
        mad_max=255.0,
    )
    assert result["ok"] is False
    assert any(item.get("reason") == "presence" for item in result["failures"])


def test_default_bands_and_json_file(tmp_path, ffmpeg_bin):
    clip = _color_clip(ffmpeg_bin, tmp_path / "same.mp4", 2.0, "blue", size="90x120")
    report = tmp_path / "parity.json"
    result = check_parity(str(clip), str(clip), samples=20, json_path=str(report))
    assert result["ok"] is True
    assert set(result["regions"]) == {"top", "mid", "bottom", "full"}
    assert report.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["json_path"]


def test_cli_parity_exit_and_json(tmp_path, ffmpeg_bin):
    approved, rejected, passing = _short_pair(tmp_path, ffmpeg_bin, duration=5.0)
    fail_json = tmp_path / "fail.json"
    fail_code = cli_main(
        [
            "check",
            "--parity",
            str(approved),
            str(rejected),
            "--edit-region",
            CARD_EDIT,
            "--region",
            SHORT_REGIONS[0],
            "--region",
            SHORT_REGIONS[1],
            "--region",
            SHORT_REGIONS[2],
            "--samples",
            "24",
            "--json",
            str(fail_json),
        ]
    )
    assert fail_code == 1
    assert fail_json.is_file()
    payload = json.loads(fail_json.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert any(item.get("region") == "top_band" for item in payload["failures"])

    pass_code = cli_main(
        [
            "check",
            "--parity",
            str(approved),
            str(passing),
            "--edit-region",
            CARD_EDIT,
            "--region",
            SHORT_REGIONS[0],
            "--region",
            SHORT_REGIONS[1],
            "--region",
            SHORT_REGIONS[2],
            "--samples",
            "24",
        ]
    )
    assert pass_code == 0


def test_cli_time_map_validation(tmp_path, ffmpeg_bin):
    clip = _color_clip(ffmpeg_bin, tmp_path / "t.mp4", 2.0, "red")
    code = cli_main(
        [
            "check",
            "--parity",
            str(clip),
            str(clip),
            "--time-map",
            "0,1.0,0",
            "0.5,2.0,0",
            "--samples",
            "20",
        ]
    )
    assert code == 1


def test_time_map_outside_duration(tmp_path, ffmpeg_bin):
    clip = _color_clip(ffmpeg_bin, tmp_path / "u.mp4", 2.0, "red")
    result = check_parity(
        str(clip),
        str(clip),
        time_map=["0,2.0,5.0"],
        samples=20,
    )
    assert result["ok"] is False
    assert "approved duration" in result["error"]
