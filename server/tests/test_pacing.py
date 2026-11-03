"""Pacing metrics. Whisper is always mocked: no model downloads."""
from __future__ import annotations

import json
from importlib import resources

from rushcut.analysis import pacing
from rushcut.analysis.pacing import measure_pacing, pacing_from_words


def _words(*spans):
    return [{"start_seconds": s, "end_seconds": e, "word": w} for s, e, w in spans]


WORDS = _words(
    (2.0, 2.4, "I"),
    (2.4, 2.8, "made"),
    (2.8, 3.4, "a"),
    (3.4, 4.0, "million."),
    (4.0, 4.2, ","),  # punctuation-only: not a word
    (14.0, 14.5, "Then"),
    (30.0, 30.6, "it"),
    (30.6, 31.0, "broke."),
)


def test_pacing_from_words_counts_gaps_and_wpm():
    result = pacing_from_words(WORDS, 60.0)
    assert result["ok"] is True
    assert result["word_count"] == 7
    assert result["wpm"] == 7.0
    # 4.0 -> 14.0 (10 s), 14.5 -> 30.0 (15.5 s) and the 31 -> 60 tail (29 s).
    stretches = [(d["start_s"], d["end_s"]) for d in result["dead_stretches"]]
    assert stretches == [(4.0, 14.0), (14.5, 30.0), (31.0, 60.0)]
    assert result["dead_stretch_count"] == 3
    assert result["dead_seconds_total"] == 54.5
    assert result["longest_gap"] == {"start_s": 31.0, "end_s": 60.0, "seconds": 29.0}
    assert result["first_15s"]["word_count"] == 5
    assert result["first_15s"]["text"] == "I made a million. Then"
    assert result["first_window"]["words"][:4] == ["I", "made", "a", "million."]
    assert result["gate"]["passed"] is False
    assert result["gate"]["reasons"]
    assert result["gate"]["no_dead_stretches"]["pass"] is False
    assert result["gate"]["wpm"]["pass"] is False
    assert result["gate_passed"] is False


def test_pacing_lead_in_counts_and_threshold_is_inclusive():
    words = _words((8.0, 8.5, "late"), (9.0, 9.5, "start"))
    result = pacing_from_words(words, 10.0, dead_threshold_s=8)
    assert [(d["start_s"], d["end_s"]) for d in result["dead_stretches"]] == [(0.0, 8.0)]


def test_pacing_accepts_short_word_keys_and_promise():
    words = [{"s": 1.0, "e": 1.3, "w": "zero"}, {"s": 1.3, "e": 1.6, "w": "to"}, {"s": 1.6, "e": 2.2, "w": "$218K"},
             {"s": 9.0, "e": 9.5, "w": "done"}]
    result = pacing_from_words(words, 12.0, promise_keywords=["218K", "never said"], wpm_min=None)
    assert result["promise"]["at_s"] == 1.6
    assert result["promise"]["matched"] == "218K"
    assert result["gate"]["promise_by_15s"]["pass"] is True
    assert "wpm" not in result["gate"]
    assert result["gate_passed"] is True


def test_pacing_promise_phrase_and_miss():
    words = _words((20.0, 20.3, "the"), (20.3, 20.8, "best"), (20.8, 21.4, "sniper"))
    hit = pacing_from_words(words, 30.0, promise_keywords=["best sniper"])
    assert hit["promise"]["at_s"] == 20.3
    assert hit["gate"]["promise_by_15s"]["pass"] is False
    miss = pacing_from_words(words, 30.0, promise_keywords=["million"])
    assert miss["promise"]["at_s"] is None


def test_pacing_rejects_bad_duration():
    assert pacing_from_words(WORDS, 0)["ok"] is False


def test_gate_defaults_match_library():
    gate = json.loads(resources.files("rushcut.formats").joinpath("library.json").read_text(encoding="utf-8"))[
        "pacing_gate"
    ]
    assert gate["dead_threshold_s"] == pacing.DEAD_THRESHOLD_S
    assert gate["overall_wpm_min_narrated"] == pacing.WPM_MIN_NARRATED
    assert gate["promise_by_s"] == pacing.PROMISE_BY_S


def test_measure_pacing_uses_mocked_whisper(tmp_path, monkeypatch):
    media = tmp_path / "vo.mp4"
    media.write_bytes(b"not really media")
    seen = {}

    def fake_transcribe(path, model_size="base", language="en"):
        seen.update(path=path, model_size=model_size, language=language)
        return {"ok": True, "available": True, "path": path, "model": model_size, "device": "cpu",
                "language": "en", "duration_seconds": 59.0, "words": WORDS}

    monkeypatch.setattr(pacing, "transcribe_media", fake_transcribe)
    monkeypatch.setattr(pacing, "probe_media", lambda p: {"ok": True, "duration_seconds": 60.0})
    result = measure_pacing(str(media), dead_threshold_s=8, promise_keywords=["million"], model_size="large-v3")
    assert seen["model_size"] == "large-v3"
    assert result["ok"] is True
    assert result["duration_seconds"] == 60.0
    assert result["duration_source"] == "ffprobe"
    assert result["dead_stretch_count"] == 3
    assert result["promise"]["at_s"] == 3.4
    assert result["model"] == "large-v3"


def test_measure_pacing_falls_back_to_whisper_duration(tmp_path, monkeypatch):
    media = tmp_path / "vo.wav"
    media.write_bytes(b"x")
    monkeypatch.setattr(pacing, "transcribe_media",
                        lambda p, model_size="base", language="en": {"ok": True, "duration_seconds": 40.0, "words": WORDS})
    monkeypatch.setattr(pacing, "probe_media", lambda p: {"ok": False, "error": "no ffprobe"})
    result = measure_pacing(str(media))
    assert result["duration_source"] == "whisper"
    assert result["duration_seconds"] == 40.0


def test_measure_pacing_soft_fails(tmp_path, monkeypatch):
    assert measure_pacing("")["ok"] is False
    assert "not found" in measure_pacing(str(tmp_path / "missing.mp4"))["error"]
    media = tmp_path / "vo.mp4"
    media.write_bytes(b"x")
    assert measure_pacing(str(media), dead_threshold_s=0)["ok"] is False
    monkeypatch.setattr(
        pacing,
        "transcribe_media",
        lambda p, model_size="base", language="en": {"ok": False, "available": False, "error": "faster-whisper is not installed."},
    )
    result = measure_pacing(str(media))
    assert result["ok"] is False
    assert result["available"] is False
    assert result["stage"] == "transcribe"


def test_measure_pacing_registered_on_server(tmp_path):
    import rushcut

    assert rushcut.measure_pacing(str(tmp_path / "missing.mp4"))["ok"] is False
