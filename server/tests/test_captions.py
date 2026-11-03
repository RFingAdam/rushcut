"""Caption chunking, masking, keep-windows, timing, and libass karaoke."""
from __future__ import annotations

import subprocess
from pathlib import Path

from rushcut.analysis.captions import (
    STYLE_PRESETS,
    apply_fix_map,
    apply_keep_windows,
    burn_captions,
    chunk_words,
    generate_captions,
    mask_word_list,
)


def _words(*pairs: tuple[float, float, str]) -> list[dict]:
    return [{"start_seconds": a, "end_seconds": b, "word": w} for a, b, w in pairs]


def test_chunking_max_words():
    words = _words(
        (0.0, 0.2, "one"),
        (0.2, 0.4, "two"),
        (0.4, 0.6, "three"),
        (0.6, 0.8, "four"),
        (0.8, 1.0, "five"),
    )
    chunks = chunk_words(words, max_chars=80, max_words=4, max_gap=0.5, extend_seconds=0.0)
    assert len(chunks) == 2
    assert chunks[0]["text"] == "one two three four"
    assert chunks[1]["text"] == "five"


def test_chunking_max_chars():
    words = _words(
        (0.0, 0.2, "abcdefghij"),
        (0.2, 0.4, "klmnopqrst"),
    )
    # "abcdefghij klmnopqrst" is 21 chars; cap at 18 → split.
    chunks = chunk_words(words, max_chars=18, max_words=8, extend_seconds=0.0)
    assert len(chunks) == 2
    assert chunks[0]["text"] == "abcdefghij"
    assert chunks[1]["text"] == "klmnopqrst"


def test_chunking_gap_split():
    words = _words((0.0, 0.2, "hello"), (0.9, 1.1, "there"))
    chunks = chunk_words(words, max_chars=80, max_words=8, max_gap=0.5, extend_seconds=0.0)
    assert len(chunks) == 2
    same = chunk_words(words, max_chars=80, max_words=8, max_gap=1.0, extend_seconds=0.0)
    assert len(same) == 1
    assert same[0]["text"] == "hello there"


def test_chunking_punctuation_split():
    words = _words((0.0, 0.2, "Stop."), (0.25, 0.4, "Go"))
    chunks = chunk_words(
        words,
        max_chars=80,
        max_words=8,
        max_gap=1.0,
        extend_seconds=0.0,
        split_on_punctuation=True,
    )
    assert len(chunks) == 2
    joined = chunk_words(
        words,
        max_chars=80,
        max_words=8,
        max_gap=1.0,
        extend_seconds=0.0,
        split_on_punctuation=False,
    )
    assert len(joined) == 1


def test_chunking_extend_never_into_next():
    words = _words((0.0, 0.3, "one"), (0.45, 0.6, "two"))
    chunks = chunk_words(words, max_chars=80, max_words=1, max_gap=0.1, extend_seconds=0.35)
    assert chunks[0]["end_seconds"] <= chunks[1]["start_seconds"] + 1e-9
    assert chunks[0]["end_seconds"] <= 0.45 + 1e-9
    assert chunks[0]["raw_end_seconds"] == 0.3


def test_fix_map_and_keep_windows_before_chunking(tmp_path):
    words = _words(
        (0.0, 0.2, "teh"),
        (0.2, 0.4, "cat"),
        (1.0, 1.2, "outside"),
    )
    fixed = apply_fix_map(words, {"teh": "the"})
    assert _word(fixed[0]) == "the"
    kept = apply_keep_windows(fixed, [{"start": 0.0, "end": 0.5}])
    assert [w["word"] for w in kept] == ["the", "cat"]
    chunks = chunk_words(kept, max_chars=18, max_words=4, extend_seconds=0.0)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "the cat"
    result = generate_captions(
        words=words,
        output_dir=str(tmp_path),
        stem="pre-chunk",
        style="default",
        fix_map={"teh": "the"},
        keep_windows=[{"start": 0.0, "end": 0.5}],
        uppercase=False,
        extend_seconds=0.0,
    )
    assert result["ok"] is True
    assert "outside" not in " ".join(c["text"] for c in result["cues"])
    assert any("the" in c["text"] for c in result["cues"])


def _word(item: dict) -> str:
    return str(item.get("word") or "")


def test_mask_words_returns_spans(tmp_path):
    words = _words((0.40, 0.70, "hello"), (0.70, 1.00, "darn"), (1.00, 1.30, "world"))
    result = generate_captions(
        words=words,
        output_dir=str(tmp_path),
        stem="mask",
        style="default",
        mask_words=["darn"],
        uppercase=False,
        extend_seconds=0.0,
    )
    assert result["ok"] is True
    spans = result["masked_spans"]
    assert len(spans) == 1
    assert spans[0]["word"] == "darn"
    assert abs(spans[0]["start"] - 0.70) < 1e-6
    assert abs(spans[0]["end"] - 1.00) < 1e-6
    joined = " ".join(c["text"] for c in result["cues"])
    assert "darn" not in joined.lower()
    assert "*" in joined
    srt = (tmp_path / "mask.srt").read_text(encoding="utf-8")
    assert "darn" not in srt.lower()


def test_caption_timing_within_80ms(tmp_path):
    words = _words(
        (1.000, 1.180, "alpha"),
        (1.180, 1.360, "beta"),
        (2.000, 2.200, "gamma"),
    )
    result = generate_captions(
        words=words,
        output_dir=str(tmp_path),
        stem="time",
        style="default",
        uppercase=False,
        extend_seconds=0.35,
    )
    assert result["ok"] is True
    first = result["cues"][0]
    assert abs(first["start_seconds"] - 1.000) <= 0.080
    assert abs(first["raw_end_seconds"] - 1.360) <= 0.080
    last = result["cues"][-1]
    assert abs(last["start_seconds"] - 2.000) <= 0.080
    assert abs(last["raw_end_seconds"] - 2.200) <= 0.080


def test_style_presets_16x9_and_9x16():
    land = STYLE_PRESETS["default"]
    vert = STYLE_PRESETS["vertical"]
    assert land["aspect"] == "16:9"
    assert land["play_x"] == 1920 and land["play_y"] == 1080
    assert land["margin_v"] == 40
    assert vert["aspect"] == "9:16"
    assert vert["play_x"] == 1080 and vert["play_y"] == 1920
    assert vert["margin_v"] == 160
    assert vert["outline"] >= 4
    assert vert["shadow"] >= 1


def test_generate_captions_writes_preset_margins(tmp_path):
    words = _words((0.0, 0.4, "hi"))
    land = generate_captions(
        words=words, output_dir=str(tmp_path), stem="land", style="default", uppercase=False
    )
    vert = generate_captions(
        words=words, output_dir=str(tmp_path), stem="vert", style="vertical"
    )
    land_ass = (tmp_path / "land.ass").read_text(encoding="utf-8")
    vert_ass = (tmp_path / "vert.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1920" in land_ass and "PlayResY: 1080" in land_ass
    assert ",40,1" in land_ass  # MarginV 40
    assert "PlayResX: 1080" in vert_ass and "PlayResY: 1920" in vert_ass
    assert ",160,1" in vert_ass  # MarginV 160
    assert land["preset"]["aspect"] == "16:9"
    assert vert["preset"]["aspect"] == "9:16"


def _fontsdir() -> str | None:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts/truetype/freefont"),
    ]
    for folder in candidates:
        if folder.is_dir() and any(folder.glob("*.ttf")):
            return str(folder)
    return None


def test_active_word_libass_golden_frame(tmp_path, ffmpeg_bin):
    """Karaoke-style \\k preset: the active word is yellow in a libass frame."""
    video = tmp_path / "black.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=25:d=1.2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    words = _words((0.00, 0.50, "HELLO"), (0.50, 1.00, "WORLD"))
    caps = generate_captions(
        words=words,
        output_dir=str(tmp_path),
        stem="kara",
        style="default",
        active_word=True,
        uppercase=True,
        extend_seconds=0.0,
        outline=4,
        shadow=0,
        margin_v=80,
    )
    assert caps["ok"] is True
    ass_path = tmp_path / "kara.ass"
    ass = ass_path.read_text(encoding="utf-8")
    assert "\\k" in ass
    assert "&H0000FFFF" in ass  # yellow primary for the sung word
    fonts = _fontsdir()
    if fonts and "dejavu" in fonts:
        ass_path.write_text(ass.replace("Arial", "DejaVu Sans"), encoding="utf-8")
    burned = tmp_path / "burned.mp4"
    result = burn_captions(str(video), caps["ass_path"], str(burned), fontsdir=_fontsdir())
    assert result["ok"] is True, result
    frame = tmp_path / "frame.rgb"
    # Mid-first-word: HELLO should be the active (yellow) word.
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-ss",
            "0.25",
            "-i",
            str(burned),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            str(frame),
        ],
        check=True,
        capture_output=True,
    )
    raw = frame.read_bytes()
    assert len(raw) >= 640 * 360 * 3
    yellow = 0
    white = 0
    for i in range(0, len(raw) - 2, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r > 180 and g > 180 and b < 80:
            yellow += 1
        elif r > 200 and g > 200 and b > 200:
            white += 1
    # libass must have painted the karaoke fill. Yellow (active) should show.
    assert yellow > 40, f"expected yellow karaoke pixels, got yellow={yellow} white={white}"


def test_mask_word_list_helper():
    words = _words((0.0, 0.2, "clean"), (0.2, 0.4, "Darn!"))
    masked, spans = mask_word_list(words, ["darn"])
    assert len(spans) == 1
    assert spans[0]["start"] == 0.2
    assert "*" in masked[1]["word"]
