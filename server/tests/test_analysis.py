from __future__ import annotations

from rushcut.analysis.audio import analyze_audio
from rushcut.analysis.captions import generate_captions
from rushcut.analysis.probe import probe_media
from rushcut.analysis.scenes import detect_scenes
from rushcut.analysis.whisper import transcribe_media
from rushcut import find_highlights


def test_probe_color_clip(color_clip):
    info = probe_media(str(color_clip))
    assert info["ok"] is True
    assert info["has_video"] is True
    assert info["has_audio"] is True
    assert info["width"] == 320
    assert info["height"] == 180
    assert 1.8 <= info["duration_seconds"] <= 2.6


def test_probe_missing():
    info = probe_media("/no/such/file.mp4")
    assert info["ok"] is False


def test_detect_scenes(scene_clip):
    result = detect_scenes(str(scene_clip), threshold=0.2)
    assert result["ok"] is True
    assert result["duration_seconds"] > 1.5
    # Hard color cut should register near 1s.
    cuts = [c["at_seconds"] for c in result["cuts"]]
    assert any(0.7 <= t <= 1.3 for t in cuts) or any(
        0.7 <= s["start_seconds"] <= 1.3 or 0.7 <= s["end_seconds"] <= 1.3
        for s in result["scenes"]
    )


def test_analyze_audio_finds_spike(spike_clip):
    result = analyze_audio(str(spike_clip), spike_ratio=1.8)
    assert result["ok"] is True
    assert result["has_audio"] is True
    assert result["spike_count"] >= 1
    times = [s["at_seconds"] for s in result["spikes"]]
    assert any(0.8 <= t <= 1.8 for t in times)
    kinds = {s.get("kind") for s in result["spikes"]}
    assert kinds <= {"peak", "loud", "rise"}


def test_captions_srt_ass_default(tmp_path, color_clip):
    cues = [
        {"start_seconds": 0.0, "end_seconds": 0.8, "text": "hook"},
        {"start_seconds": 0.8, "end_seconds": 1.6, "text": "payoff"},
    ]
    result = generate_captions(
        path=str(color_clip),
        segments=cues,
        output_dir=str(tmp_path),
        style="default",
        group_words=False,
    )
    assert result["ok"] is True
    srt = (tmp_path / "bars.srt").read_text(encoding="utf-8")
    ass = (tmp_path / "bars.ass").read_text(encoding="utf-8")
    assert "hook" in srt and "payoff" in srt
    assert "-->" in srt
    assert "PlayResX: 1920" in ass
    assert "PlayResY: 1080" in ass
    assert "Arial" in ass
    assert "Arial Black" not in ass
    assert "Dialogue:" in ass


def test_captions_vertical_style(tmp_path):
    cues = [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "hello"}]
    result = generate_captions(
        segments=cues,
        output_dir=str(tmp_path),
        style="vertical",
        group_words=False,
        stem="vert",
    )
    assert result["ok"] is True
    ass = (tmp_path / "vert.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "Arial Black" not in ass


def test_word_grouped_captions(tmp_path):
    words = [
        {"start_seconds": 0.0, "end_seconds": 0.2, "word": "one"},
        {"start_seconds": 0.2, "end_seconds": 0.4, "word": "two"},
        {"start_seconds": 0.4, "end_seconds": 0.6, "word": "three"},
        {"start_seconds": 0.6, "end_seconds": 0.8, "word": "four"},
        {"start_seconds": 2.0, "end_seconds": 2.2, "word": "five"},
    ]
    result = generate_captions(words=words, output_dir=str(tmp_path), stem="grouped")
    assert result["ok"] is True
    assert result["cue_count"] >= 2
    joined = " ".join(c["text"] for c in result["cues"])
    assert "one" in joined and "five" in joined


def test_find_highlights_removed():
    result = find_highlights("/tmp/unused.mp4")
    assert result["ok"] is False
    assert result["removed"] is True
    assert result["tool"] == "find_highlights"


def test_whisper_soft_fail_or_runs(color_clip):
    result = transcribe_media(str(color_clip), model_size="tiny")
    if result.get("available") is False:
        assert result["ok"] is False
        assert "faster-whisper" in result["error"]
    else:
        # Installed: must not crash. Empty speech on a sine wave is fine.
        assert "error" in result or result.get("ok") is True
