"""read_screen_regions / match_template on a synthetic lavfi fixture."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from rushcut.analysis import screen  # noqa: E402
from rushcut.analysis.screen import (  # noqa: E402
    OCR_INSTALL,
    file_fingerprint,
    match_template,
    parse_numeric,
    read_screen_regions,
)
from rushcut.profile import profile_from_data  # noqa: E402

FONT_DIR = Path(screen.__file__).resolve().parent.parent / "edit" / "fonts"
FONT = "LiberationSans-Regular.ttf"

# Counter values and the spans they are on screen (seconds).
COUNTER = [("$1,250", 0.0, 2.3), ("$3,400", 2.3, 5.7), ("$10,075", 5.7, 10.0)]
CHANGE_TIMES = [2.3, 5.7]
BANNER = (3.2, 6.6)
BANNER_RECT = (380, 240, 220, 80)
SCORE = {"name": "score", "rect": [10, 10, 240, 56], "numeric": True}
LABEL = {"name": "label", "rect": [10, 70, 200, 56]}


def _has_filter(ffmpeg_bin: str, name: str) -> bool:
    proc = subprocess.run([ffmpeg_bin, "-hide_banner", "-filters"], capture_output=True, text=True)
    return f" {name} " in proc.stdout


def _tesseract_ready() -> bool:
    _, _, err = screen._load_ocr()
    return err is None


needs_ocr = pytest.mark.skipif(not _tesseract_ready(), reason="pytesseract / tesseract not installed")


@pytest.fixture(scope="module")
def hud(tmp_path_factory) -> dict[str, Path]:
    from rushcut.analysis.ffmpeg_util import which_ffmpeg

    ffmpeg_bin = which_ffmpeg("ffmpeg")
    if not _has_filter(ffmpeg_bin, "drawtext"):
        pytest.skip("ffmpeg built without drawtext")
    out = tmp_path_factory.mktemp("screen")
    video = out / "hud.mp4"
    filters = []
    for text, a, b in COUNTER:
        filters.append(
            f"drawtext=fontfile={FONT}:text='{text}':x=20:y=20:fontsize=36:"
            f"fontcolor=white:enable='between(t,{a},{b})'"
        )
    filters.append(f"drawtext=fontfile={FONT}:text='ROUND':x=20:y=80:fontsize=36:fontcolor=white")
    bx, by, bw, bh = BANNER_RECT
    filters.append(
        f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=0xC03030@1:t=fill:"
        f"enable='between(t,{BANNER[0]},{BANNER[1]})'"
    )
    filters.append(
        f"drawtext=fontfile={FONT}:text='LIVE NOW':x={bx + 20}:y={by + 22}:fontsize=34:"
        f"fontcolor=white:enable='between(t,{BANNER[0]},{BANNER[1]})'"
    )
    # Run from the font folder so fontfile needs no path escaping.
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x202020:s=640x360:r=30:d=10",
            "-vf", ",".join(filters),
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "30", str(video),
        ],
        check=True,
        cwd=str(FONT_DIR),
        capture_output=True,
    )
    banner = out / "banner.png"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-ss", "4", "-i", str(video),
            "-frames:v", "1", "-vf", f"crop={bw}:{bh}:{bx}:{by}", str(banner),
        ],
        check=True,
        capture_output=True,
    )
    return {"video": video, "banner": banner, "dir": out}


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSHCUT_CACHE_DIR", str(tmp_path / "cache"))


# ------------------------------------------------------------ templates


def _assert_banner_span(result: dict) -> dict:
    assert result["ok"] is True, result
    spans = [s for s in result["spans"] if s["template"] == "banner"]
    assert len(spans) == 1, spans  # nothing in the static stretches
    span = spans[0]
    assert abs(span["start"] - BANNER[0]) <= 0.5
    assert abs(span["end"] - BANNER[1]) <= 0.5
    assert span["score"] >= 0.9
    assert abs(span["rect"]["x"] - BANNER_RECT[0]) <= 4
    assert abs(span["rect"]["y"] - BANNER_RECT[1]) <= 4
    assert result["frames_read"] == 20
    assert result["runtime_seconds"] >= 0
    return span


def test_match_template_numpy_finds_banner_span(hud):
    result = match_template(str(hud["video"]), [str(hud["banner"])], engine="numpy", use_cache=False)
    _assert_banner_span(result)
    assert result["engine"] == "numpy"
    # Change detection: the static stretches are not re-matched.
    assert result["stats"]["unchanged_skips"] > result["stats"]["match_calls"]


def test_match_template_opencv_agrees(hud):
    pytest.importorskip("cv2")
    result = match_template(str(hud["video"]), [str(hud["banner"])], engine="opencv", use_cache=False)
    _assert_banner_span(result)
    assert result["engine"] == "opencv"


def test_match_template_seek_and_region(hud):
    result = match_template(
        str(hud["video"]),
        [{"name": "banner", "path": str(hud["banner"])}],
        regions=[{"name": "lower_right", "rect": [0.5, 0.5, 0.5, 0.5]}],
        start_seconds=2.0,
        end_seconds=8.0,
        engine="numpy",
        use_cache=False,
    )
    assert result["ok"] is True, result
    assert result["frames_read"] == 12
    span = result["spans"][0]
    assert abs(span["start"] - BANNER[0]) <= 0.5
    assert abs(span["end"] - BANNER[1]) <= 0.5
    assert span["region"] == "lower_right"


def test_match_template_absent_graphic_has_no_spans(hud, tmp_path):
    from PIL import Image

    noise = (np.random.default_rng(1).random((40, 60)) * 255).astype("uint8")
    path = tmp_path / "noise.png"
    Image.fromarray(noise).save(path)
    result = match_template(str(hud["video"]), [str(path)], engine="numpy", use_cache=False)
    assert result["ok"] is True
    assert result["spans"] == []


def test_match_template_profile_templates(hud, tmp_path):
    profile = {
        "screen": {
            "regions": [{"name": "lower_right", "rect": [320, 180, 320, 180]}],
            "templates": [{"name": "live", "path": str(hud["banner"]), "region": "lower_right"}],
        }
    }
    result = match_template(str(hud["video"]), profile=profile, engine="numpy", use_cache=False)
    assert result["ok"] is True, result
    assert [s["template"] for s in result["spans"]] == ["live"]


def test_match_template_profile_file_resolves_relative_paths(hud, tmp_path):
    (tmp_path / "tpl").mkdir()
    (tmp_path / "tpl" / "live.png").write_bytes(hud["banner"].read_bytes())
    prof = tmp_path / "edit_profile.yaml"
    prof.write_text(
        "schema: rushcut.profile/v1\n"
        "screen:\n"
        "  templates:\n"
        "    - {name: live, path: tpl/live.png, threshold: 0.85}\n",
        encoding="utf-8",
    )
    result = match_template(str(hud["video"]), profile=str(prof), engine="numpy", use_cache=False)
    assert result["ok"] is True, result
    assert result["templates"][0]["threshold"] == 0.85
    assert [s["template"] for s in result["spans"]] == ["live"]


def test_match_template_errors(hud, tmp_path):
    from PIL import Image

    assert match_template(str(hud["video"]), [])["ok"] is False
    missing = match_template(str(hud["video"]), [str(tmp_path / "nope.png")])
    assert "not found" in missing["error"]
    flat = tmp_path / "flat.png"
    Image.new("L", (30, 30), 128).save(flat)
    assert "no contrast" in match_template(str(hud["video"]), [str(flat)])["error"]
    big = tmp_path / "big.png"
    Image.fromarray((np.indices((50, 50)).sum(0) % 255).astype("uint8")).save(big)
    small = match_template(str(hud["video"]), [str(big)], regions=[[0, 0, 20, 20]], scale=1.0)
    assert "larger than its search region" in small["error"]


def test_ncc_numpy_matches_known_offset():
    rng = np.random.default_rng(7)
    image = (rng.random((90, 120)) * 255).astype("uint8")
    tmpl = image[30:50, 70:100].copy()
    f0 = tmpl.astype(float) - tmpl.mean()
    pad = np.zeros(image.shape)
    pad[:20, :30] = f0
    fft = np.conj(np.fft.rfft2(pad))
    res = screen._ncc_numpy(np, image, fft, float(np.sqrt((f0 * f0).sum())), 20, 30)
    iy, ix = np.unravel_index(int(np.argmax(res)), res.shape)
    assert (iy, ix) == (30, 70)
    assert res[iy, ix] == pytest.approx(1.0, abs=1e-6)
    # Brightness/contrast invariant, like TM_CCOEFF_NORMED.
    brighter = np.clip(image.astype(float) * 0.5 + 60, 0, 255).astype("uint8")
    res2 = screen._ncc_numpy(np, brighter, fft, float(np.sqrt((f0 * f0).sum())), 20, 30)
    assert float(res2.max()) > 0.99
    try:
        import cv2
    except ImportError:
        return
    ref = screen._ncc_opencv(np, cv2, image, tmpl)
    assert np.allclose(ref, res, atol=1e-3)


def test_build_spans_merge_gap_and_min_duration():
    rect = {"x": 0, "y": 0, "w": 4, "h": 4}

    def samples(pattern: str) -> list:
        return [(i * 0.5, ch == "1", 0.9 if ch == "1" else 0.1, rect, None) for i, ch in enumerate(pattern)]

    # 1.0-2.0 present, one-sample dropout, 2.5-3.5 present, then a blip at 5.0.
    pat = "0011011000100"
    merged = screen._build_spans(samples(pat), 0.5, "x", min_duration=0.0, merge_gap=0.5)
    assert [(s["start"], s["end"]) for s in merged] == [(1.0, 3.5), (5.0, 5.5)]
    split = screen._build_spans(samples(pat), 0.5, "x", min_duration=0.0, merge_gap=0.0)
    assert [(s["start"], s["end"]) for s in split] == [(1.0, 2.0), (2.5, 3.5), (5.0, 5.5)]
    long_only = screen._build_spans(samples(pat), 0.5, "x", min_duration=1.0, merge_gap=0.5)
    assert [(s["start"], s["end"]) for s in long_only] == [(1.0, 3.5)]


# ----------------------------------------------------------------- cache


def test_cache_hit_and_invalidation(hud, tmp_path):
    video = tmp_path / "copy.mp4"
    video.write_bytes(hud["video"].read_bytes())
    first = match_template(str(video), [str(hud["banner"])], engine="numpy")
    assert first["cache"]["hit"] is False
    assert first["cache"]["path"] and os.path.isfile(first["cache"]["path"])
    assert Path(first["cache"]["path"]).parent == tmp_path / "cache" / "screen"
    second = match_template(str(video), [str(hud["banner"])], engine="numpy")
    assert second["cache"]["hit"] is True
    assert second["frames_read"] == 0
    assert second["cache"]["original_frames_read"] == first["frames_read"]
    assert second["spans"] == first["spans"]
    # Different params miss.
    other = match_template(str(video), [str(hud["banner"])], engine="numpy", threshold=0.7)
    assert other["cache"]["hit"] is False
    # A changed file misses.
    before = file_fingerprint(str(video))
    st = video.stat()
    os.utime(video, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert file_fingerprint(str(video)) != before
    third = match_template(str(video), [str(hud["banner"])], engine="numpy")
    assert third["cache"]["hit"] is False
    json.dumps(third)  # results are plain JSON


# ------------------------------------------------------------------- OCR


def test_read_screen_regions_reports_missing_ocr(hud, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    result = read_screen_regions(str(hud["video"]), [SCORE])
    assert result["ok"] is False
    assert result["install"] == OCR_INSTALL == "pip install 'rushcut-mcp[ocr]'"
    assert "pytesseract" in result["error"]


def test_read_screen_regions_reports_missing_tesseract_binary(hud, tmp_path, monkeypatch):
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", str(tmp_path / "no-tesseract"))
    result = read_screen_regions(str(hud["video"]), [SCORE])
    assert result["ok"] is False
    assert result["install"] == OCR_INSTALL
    assert "tesseract" in result["error"]


def test_parse_numeric():
    assert parse_numeric("$1,250") == 1250
    assert parse_numeric("3,400.50") == 3400.5
    assert parse_numeric("17") == 17
    assert parse_numeric("$") is None
    assert parse_numeric("1.2.3") is None


@needs_ocr
def test_read_screen_regions_emits_only_changes(hud):
    result = read_screen_regions(str(hud["video"]), [SCORE, LABEL], use_cache=False)
    assert result["ok"] is True, result
    score = [e for e in result["events"] if e["region"] == "score"]
    label = [e for e in result["events"] if e["region"] == "label"]
    # Initial value, then one event per change; nothing in the static stretches.
    assert [e["value"] for e in score] == [1250, 3400, 10075]
    assert [e["text"] for e in score] == ["$1,250", "$3,400", "$10,075"]
    assert score[0]["t"] == 0.0
    for event, expected in zip(score[1:], CHANGE_TIMES):
        assert abs(event["t"] - expected) <= 0.5
    assert [e["text"] for e in label] == ["ROUND"]
    assert all(e["confidence"] >= 0.95 for e in result["events"])
    assert result["frames_read"] == 20
    assert result["runtime_seconds"] > 0
    stats = result["stats"]
    # Change detection skipped OCR on unchanged regions.
    assert stats["ocr_calls"] <= 6
    assert stats["unchanged_skips"] >= 30


@needs_ocr
def test_read_screen_regions_confidence_floor_is_configurable(hud):
    strict = read_screen_regions(str(hud["video"]), [SCORE], min_confidence=0.999, use_cache=False)
    assert strict["ok"] is True
    assert strict["events"] == []
    assert strict["rejected"] and all(r["reason"] == "low_confidence" for r in strict["rejected"])
    per_region = read_screen_regions(
        str(hud["video"]), [{**SCORE, "min_confidence": 0.5}], min_confidence=0.999, use_cache=False
    )
    assert len(per_region["events"]) == 3


@needs_ocr
def test_read_screen_regions_from_profile_and_cache(hud):
    profile = profile_from_data({"screen": {"regions": [LABEL]}})
    first = read_screen_regions(str(hud["video"]), profile=profile, start_seconds=1.0, end_seconds=4.0)
    assert first["ok"] is True, first
    assert [e["text"] for e in first["events"]] == ["ROUND"]
    assert first["events"][0]["t"] == 1.0
    second = read_screen_regions(str(hud["video"]), profile=profile, start_seconds=1.0, end_seconds=4.0)
    assert second["cache"]["hit"] is True
    assert second["events"] == first["events"]


def test_read_screen_regions_validates_regions(hud):
    if not _tesseract_ready():
        pytest.skip("validation runs after the OCR check")
    assert read_screen_regions(str(hud["video"]), [])["ok"] is False
    dup = read_screen_regions(str(hud["video"]), [LABEL, LABEL])
    assert "duplicate" in dup["error"]


# --------------------------------------------------------------- profile


def test_profile_screen_block_round_trip():
    profile = profile_from_data(
        {
            "screen": {
                "regions": [
                    {"name": "score", "rect": [0.02, 0.02, 0.3, 0.08], "numeric": True},
                    {"name": "status", "rect": {"x": 10, "y": 300, "w": 400, "h": 40}, "whitelist": "ABC"},
                ],
                "templates": [{"name": "banner", "path": "templates/banner.png", "region": "status"}],
            }
        }
    )
    assert profile.screen is not None
    assert profile.screen.regions[0].numeric is True
    assert profile.screen.templates[0].region == "status"
    assert profile_from_data({}).screen is None


@pytest.mark.parametrize(
    "screen_block, message",
    [
        ({"regions": [{"name": "a", "rect": [1, 2, 3]}]}, "rect"),
        ({"regions": [{"name": "a", "rect": [0, 0, 1, 1]}, {"name": "a", "rect": [0, 0, 1, 1]}]}, "unique"),
        ({"templates": [{"name": "t", "path": "t.png", "region": "missing"}]}, "unknown region"),
        ({"regions": [{"name": "a", "rect": [0, 0, 1, 1], "min_confidence": 2}]}, "min_confidence"),
    ],
)
def test_profile_screen_block_rejects_bad_input(screen_block, message):
    with pytest.raises(ValueError, match=message):
        profile_from_data({"screen": screen_block})


# ----------------------------------------------------- stalls and errors


def _stalling_ffmpeg(tmp_path: Path, frames: int) -> Path:
    """A stand-in ffmpeg that writes `frames` blank frames, then hangs."""
    script = tmp_path / "stall_ffmpeg.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import re, sys, time\n"
        "vf = sys.argv[sys.argv.index('-vf') + 1]\n"
        "w, h = map(int, re.search(r'crop=(\\d+):(\\d+)', vf).groups())\n"
        "m = re.search(r'scale=(\\d+):(\\d+)', vf)\n"
        "if m:\n"
        "    w, h = map(int, m.groups())\n"
        f"sys.stdout.buffer.write(bytes(w * h) * {frames})\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    return script


def test_match_template_stalled_ffmpeg_returns_partial(hud, tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("FFMPEG_BIN", str(_stalling_ffmpeg(tmp_path, frames=3)))
    monkeypatch.setattr(screen, "_STALL_MIN_SECONDS", 1.0)
    monkeypatch.setattr(screen, "_STALL_SAMPLE_PERIODS", 0.0)
    began = time.monotonic()
    result = match_template(str(hud["video"]), [str(hud["banner"])], engine="numpy")
    # The hung ffmpeg is killed after the stall budget, not after it exits by itself.
    assert time.monotonic() - began < 20
    assert result["ok"] is True, result
    assert result["partial"] is True
    assert result["frames_read"] == 3
    assert result["read_until"] == 1.0
    assert "no frame" in result["partial_reason"]
    # A cut-short read is not cached.
    assert result["cache"]["key"] is None


def test_match_template_stall_before_any_frame_is_an_error(hud, tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", str(_stalling_ffmpeg(tmp_path, frames=0)))
    monkeypatch.setattr(screen, "_STALL_MIN_SECONDS", 1.0)
    monkeypatch.setattr(screen, "_STALL_SAMPLE_PERIODS", 0.0)
    result = match_template(str(hud["video"]), [str(hud["banner"])], engine="numpy")
    assert result["ok"] is False
    assert "no frame" in result["error"]


class _FakeTesseractError(Exception):
    pass


def _fake_ocr(monkeypatch, ocr_crop) -> None:
    fake = type(sys)("pytesseract")
    fake.TesseractError = _FakeTesseractError
    monkeypatch.setattr(screen, "_load_ocr", lambda: (fake, "5.0.0", None))
    monkeypatch.setattr(screen, "_ocr_crop", ocr_crop)


def test_read_screen_regions_slow_ocr_is_not_a_stall(hud, monkeypatch):
    """The stall budget counts only time waiting on ffmpeg, never OCR time."""
    import time

    calls = []

    def slow_ocr(np_mod, tess, crop, spec):
        calls.append(spec["name"])
        if len(calls) == 1:
            time.sleep(1.5)
        return "ROUND", 1.0

    _fake_ocr(monkeypatch, slow_ocr)
    monkeypatch.setattr(screen, "_STALL_MIN_SECONDS", 1.0)
    monkeypatch.setattr(screen, "_STALL_SAMPLE_PERIODS", 0.0)
    result = read_screen_regions(str(hud["video"]), [LABEL], use_cache=False)
    assert result["ok"] is True, result
    assert result["partial"] is False
    assert result["frames_read"] == 20
    assert [e["text"] for e in result["events"]] == ["ROUND"]


def test_read_screen_regions_tesseract_error_is_an_error_result(hud, monkeypatch):
    def broken_ocr(np_mod, tess, crop, spec):
        raise _FakeTesseractError("(1, 'Error opening data file')")

    _fake_ocr(monkeypatch, broken_ocr)
    result = read_screen_regions(str(hud["video"]), [LABEL], use_cache=False)
    assert result["ok"] is False
    assert "tesseract failed" in result["error"]
    assert "Error opening data file" in result["error"]
    assert "label" in result["error"]
