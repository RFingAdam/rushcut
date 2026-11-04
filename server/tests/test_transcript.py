from __future__ import annotations

from rushcut.analysis import whisper as whisper_mod
from rushcut.analysis.transcript import (
    analyze_transcript,
    attach_speakers,
    diarize_media,
    speech_spans_from_words,
)
from rushcut.analysis.whisper import offset_transcript, transcribe_media

WORDS = [
    {"start_seconds": 1.0, "end_seconds": 1.2, "word": "hello"},
    {"start_seconds": 1.25, "end_seconds": 1.5, "word": "there"},
    {"start_seconds": 3.0, "end_seconds": 3.4, "word": "later"},
]


def test_speech_spans_merge_nearby_words():
    spans = speech_spans_from_words(WORDS, merge_gap_seconds=0.3)
    assert len(spans) == 2
    assert spans[0]["start_seconds"] == 1.0
    assert spans[0]["end_seconds"] == 1.5
    assert "hello" in spans[0]["text"] and "there" in spans[0]["text"]
    assert spans[1]["text"] == "later"


def test_attach_speakers_by_midpoint():
    turns = [
        {"start_seconds": 0.0, "end_seconds": 2.0, "speaker": "SPEAKER_00"},
        {"start_seconds": 2.0, "end_seconds": 4.0, "speaker": "SPEAKER_01"},
    ]
    labeled = attach_speakers(WORDS, turns)
    assert labeled[0]["speaker"] == "SPEAKER_00"
    assert labeled[1]["speaker"] == "SPEAKER_00"
    assert labeled[2]["speaker"] == "SPEAKER_01"


def test_analyze_transcript_from_words_no_whisper():
    result = analyze_transcript(words=WORDS, diarize=False)
    assert result["ok"] is True
    assert len(result["words"]) == 3
    assert result["speech_spans"]
    assert result["diarization"]["available"] is False
    assert result["diarization"]["method"] == "off"


def test_analyze_transcript_with_injected_turns():
    turns = [{"start_seconds": 0.0, "end_seconds": 5.0, "speaker": "A"}]
    result = analyze_transcript(words=WORDS, turns=turns, diarize=True)
    assert result["ok"] is True
    assert result["diarization"]["available"] is True
    assert result["diarization"]["method"] == "turns"
    assert result["speakers"] == ["A"]
    assert all(w.get("speaker") == "A" for w in result["words"])


def test_offset_transcript_is_source_absolute():
    raw = {
        "ok": True,
        "words": [{"start_seconds": 0.1, "end_seconds": 0.3, "word": "hi"}],
        "segments": [{"start_seconds": 0.1, "end_seconds": 0.3, "text": "hi"}],
        "duration_seconds": 0.5,
    }
    shifted = offset_transcript(raw, 12.0)
    assert shifted["words"][0]["start_seconds"] == 12.1
    assert shifted["segments"][0]["end_seconds"] == 12.3
    assert shifted["slice_offset_seconds"] == 12.0


def test_transcribe_media_missing_file():
    result = transcribe_media("/no/such/file.mp4")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_diarize_media_off():
    result = diarize_media(words=WORDS, method=False)
    assert result["available"] is False
    assert result["method"] == "off"


def test_stereo_diarize_left_then_right(tmp_path, ffmpeg_bin):
    import subprocess

    out = tmp_path / "lr.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1",
            "-filter_complex",
            (
                "[0:a][1:a]concat=n=2:v=0:a=1[l];"
                "[2:a][3:a]concat=n=2:v=0:a=1[r];"
                "[l][r]join=inputs=2:channel_layout=stereo[a]"
            ),
            "-map",
            "[a]",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    words = [
        {"start_seconds": 0.2, "end_seconds": 0.6, "word": "left"},
        {"start_seconds": 1.2, "end_seconds": 1.6, "word": "right"},
    ]
    result = analyze_transcript(str(out), words=words, diarize="stereo")
    assert result["ok"] is True
    assert result["diarization"]["available"] is True
    assert result["words"][0]["speaker"] == "SPEAKER_L"
    assert result["words"][1]["speaker"] == "SPEAKER_R"


def _truth_words(step: float = 300.0, until: float = 5400.0) -> list[dict]:
    words = []
    t = 0.0
    while t < until:
        words.append(
            {
                "start_seconds": round(t, 3),
                "end_seconds": round(t + 0.2, 3),
                "word": f"w{int(t)}",
            }
        )
        t += step
    return words


def test_chunked_long_file_drift_cache_and_slice(tmp_path, color_clip, monkeypatch):
    truth = _truth_words()
    calls: list[tuple[float | None, float | None]] = []
    chunk_runs: dict[tuple[float, float], int] = {}

    def hook(
        path,
        start_seconds=None,
        end_seconds=None,
        **_kwargs,
    ):
        start = 0.0 if start_seconds is None else float(start_seconds)
        end = 5400.0 if end_seconds is None else float(end_seconds)
        calls.append((start_seconds, end_seconds))
        key = (round(start, 1), round(end, 1))
        picked = [dict(w) for w in truth if start - 0.01 <= w["start_seconds"] < end]
        is_chunk = (end - start) > 60
        if is_chunk and 590 <= start < 1210:
            chunk_runs[key] = chunk_runs.get(key, 0) + 1
            if chunk_runs[key] == 1:
                picked = [
                    {
                        **w,
                        "start_seconds": round(w["start_seconds"] + 5.0, 3),
                        "end_seconds": round(w["end_seconds"] + 5.0, 3),
                    }
                    for w in picked
                ]
        return {
            "ok": True,
            "available": True,
            "words": picked,
            "segments": [],
            "text": " ".join(w["word"] for w in picked),
            "device": "cpu",
            "compute_type": "int8",
            "model": "small",
            "language": "en",
        }

    def fake_probe(path, speech_ratio=True):
        return {
            "ok": True,
            "path": path,
            "duration_seconds": 5400.0,
            "unfinalized_container": False,
            "header_duration_missing": False,
            "warnings": [],
        }

    monkeypatch.setattr(whisper_mod, "_TRANSCRIBE_HOOK", hook)
    monkeypatch.setattr(whisper_mod, "probe_media", fake_probe)

    cache = tmp_path / "cache"
    full = transcribe_media(
        str(color_clip),
        model_size="small",
        chunk_seconds=600,
        overlap_seconds=30,
        drift_threshold_s=0.25,
        cache_dir=str(cache),
        json_path=str(tmp_path / "transcript.json"),
    )
    assert full["ok"] is True
    assert full["cache_hit"] is False
    assert full["chunking"]["chunk_count"] >= 8
    drifted = [c for c in full["chunking"]["chunks"] if c.get("rerun")]
    assert drifted, full["chunking"]
    assert any(float(c.get("max_drift_s") or 0) >= 4.0 for c in full["chunking"]["chunks"])
    for word in full["words"]:
        expected = next(w for w in truth if w["word"] == word["word"])
        assert abs(word["start_seconds"] - expected["start_seconds"]) <= 0.2
    report = (tmp_path / "transcript.json").read_text(encoding="utf-8")
    assert "chunking" in report and "max_drift_s" in report

    first_calls = len(calls)
    cached = transcribe_media(
        str(color_clip),
        model_size="small",
        chunk_seconds=600,
        overlap_seconds=30,
        drift_threshold_s=0.25,
        cache_dir=str(cache),
    )
    assert cached["cache_hit"] is True
    assert len(calls) == first_calls

    forced = transcribe_media(
        str(color_clip),
        model_size="small",
        chunk_seconds=600,
        overlap_seconds=30,
        drift_threshold_s=0.25,
        cache_dir=str(cache),
        no_cache=True,
    )
    assert forced["cache_hit"] is False
    assert len(calls) > first_calls

    sl = transcribe_media(
        str(color_clip),
        model_size="small",
        start_seconds=600,
        end_seconds=1200,
        chunk_seconds=600,
        overlap_seconds=30,
        cache_dir=str(tmp_path / "slice-cache"),
    )
    assert sl["ok"] is True
    full_mid = [w for w in full["words"] if 600 <= w["start_seconds"] < 1200]
    for word in sl["words"]:
        match = next((w for w in full_mid if w["word"] == word["word"]), None)
        if match is None:
            continue
        assert abs(word["start_seconds"] - match["start_seconds"]) <= 0.1
