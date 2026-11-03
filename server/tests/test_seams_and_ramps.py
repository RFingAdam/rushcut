"""Acceptance tests for check_seams / snap_cuts (#37) and speed-ramp guard (#39)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from rushcut.cli import main as cli_main
from rushcut.quality import (
    check_seams,
    check_speed_ramps,
    lf_352_boundary_spec,
    run_checks,
    snap_cuts,
)
from rushcut.quality.seams import total_kept_seconds


def _tone_bursts(tmp_path: Path) -> Path:
    """1 kHz tone bursts with −40 dB gaps (issue #37 fixture).

    Timeline: gap 0.00–0.40, burst 0.40–1.40, gap 1.40–1.80, burst 1.80–2.80,
    gap 2.80–3.20. Burst amplitude is full-scale; gaps are 0.01 linear (−40 dB).
    """
    import math
    import wave

    dest = tmp_path / "bursts.wav"
    sr = 48000
    duration = 3.2
    n = int(sr * duration)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        in_burst = (0.40 <= t < 1.40) or (1.80 <= t < 2.80)
        amp = 0.9 if in_burst else 0.01  # 0.01 linear ≈ −40 dBFS
        sample = int(max(-32767, min(32767, amp * 32767 * math.sin(2 * math.pi * 1000 * t))))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(dest), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(bytes(frames))
    return dest


def test_tone_burst_cuts_flagged_voiced_and_snapped_into_gap(tmp_path):
    mic = _tone_bursts(tmp_path)
    cuts = [
        {"start_seconds": 0.45, "end_seconds": 1.35},  # 50 ms inside each burst
        {"start_seconds": 1.60, "end_seconds": 1.70},  # already in a gap
    ]
    scored = check_seams(cuts, mic_audio=str(mic), window_ms=300)
    by_t = {round(s["at_seconds"], 2): s for s in scored["seams"]}
    assert by_t[0.45]["voiced"] is True
    assert by_t[1.35]["voiced"] is True
    assert by_t[1.6]["voiced"] is False
    assert by_t[1.7]["voiced"] is False
    assert scored["markdown"]
    assert "| role |" in scored["markdown"]

    snapped = snap_cuts(cuts, mic_audio=str(mic), window_ms=300, keep_words=True)
    assert snapped["applied"] is True
    new_cuts = snapped["cuts"]
    # First segment moved into the surrounding gaps.
    assert new_cuts[0]["start_seconds"] < 0.40
    assert new_cuts[0]["end_seconds"] > 1.40
    # Gap cuts stay put.
    assert abs(new_cuts[1]["start_seconds"] - 1.60) < 1e-3
    assert abs(new_cuts[1]["end_seconds"] - 1.70) < 1e-3
    again = check_seams(new_cuts, mic_audio=str(mic), window_ms=300)
    quiet = {round(s["at_seconds"], 2): s for s in again["seams"]}
    assert quiet[round(new_cuts[0]["start_seconds"], 2)]["voiced"] is False
    assert quiet[round(new_cuts[0]["end_seconds"], 2)]["voiced"] is False


def test_snap_never_removes_covered_word():
    cuts = [{"start_seconds": 1.0, "end_seconds": 3.0}]
    words = [{"start_seconds": 1.10, "end_seconds": 1.40, "word": "keep"}]
    envelope = [
        {"at_seconds": 0.5, "dbfs": -50.0},
        {"at_seconds": 1.0, "dbfs": -12.0},
        {"at_seconds": 1.5, "dbfs": -50.0},
        {"at_seconds": 3.0, "dbfs": -50.0},
    ]
    result = snap_cuts(
        cuts,
        envelope=envelope,
        words=words,
        window_ms=300,
        keep_words=True,
    )
    snapped = result["cuts"][0]
    # Local min is 1.5, which would drop the word at 1.10. Must not.
    assert snapped["start_seconds"] <= 1.10 + 1e-6
    assert 1.10 < snapped["end_seconds"]
    kept = result["report"]["seams"][0]
    assert kept["role"] == "in"
    assert kept["voiced"] is True
    # Either clamped in front of the word or left unapplied.
    assert snapped["start_seconds"] <= 1.10 or not (kept["suggestion"] or {}).get("apply")


def test_snap_respects_max_total_seconds():
    cuts = [
        {"start_seconds": 0.0, "end_seconds": 14.0},
        {"start_seconds": 20.0, "end_seconds": 34.0},
    ]
    assert abs(total_kept_seconds(cuts) - 28.0) < 1e-6
    envelope = [
        {"at_seconds": 14.0, "dbfs": -10.0},
        {"at_seconds": 16.0, "dbfs": -50.0},
        {"at_seconds": 34.0, "dbfs": -10.0},
        {"at_seconds": 36.0, "dbfs": -50.0},
    ]
    result = snap_cuts(
        cuts,
        envelope=envelope,
        window_ms=2500,
        max_total_seconds=30.0,
    )
    assert result["applied"] is False
    assert result["blocked_reason"] == "max_total_seconds"
    assert result["cuts"] == cuts
    assert result["snapped_total_seconds"] > 30.0
    assert "max_total_seconds" in result["markdown"]


def test_seams_report_json_markdown_and_listen_here():
    cuts = [{"start_seconds": 10.0, "end_seconds": 12.0}]
    envelope = [{"at_seconds": 10.0, "dbfs": -8.0}, {"at_seconds": 12.0, "dbfs": -50.0}]
    result = check_seams(cuts, envelope=envelope, window_ms=200)
    assert result["seams"]
    seam = result["seams"][0]
    assert seam["voiced"] is True
    assert "dbfs" in seam
    assert "local_min_seconds" in seam
    assert "mid_word" in seam
    assert "fade_seconds" in seam
    assert "suggestion" in seam
    assert "listen here" in result["markdown"].lower()
    assert result["listen_here"]
    assert result["listen_here"][0]["at_seconds"] == 10.0
    assert "10.000" in (result["listen_here"][0].get("listen_here") or result["markdown"])


def test_lf_352_spec_under_ten_seconds_flags_known_seams():
    spec = lf_352_boundary_spec()
    assert spec["boundary_count"] == 352
    t0 = time.perf_counter()
    result = check_seams(
        spec["cuts"],
        envelope=spec["envelope"],
        words=spec["words"],
        window_ms=300,
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0, f"352-boundary spec took {elapsed:.2f}s"
    flagged = {
        (s.get("role"), round(float(s["at_seconds"]), 2))
        for s in result["seams"]
        if s.get("voiced")
    }
    known = {("in", 5795.30), ("out", 6054.18), ("out", 4592.48)}
    assert known <= flagged
    assert result["voiced_count"] >= 3


def test_speed_ramp_covering_word_fails_then_shift_passes():
    spec = {
        "segments": [{"start_seconds": 0.0, "end_seconds": 10.0}],
        "speed_ramps": [{"start_seconds": 2.0, "end_seconds": 8.0, "speed": 4}],
    }
    words = [{"start_seconds": 5.00, "end_seconds": 5.40, "word": "Three", "confidence": 0.9}]
    failed = check_speed_ramps(spec, words, pad_seconds=0.08)
    assert failed["pass"] is False
    viol = failed["violations"][0]
    assert viol["word"] == "Three"
    assert abs(float(viol["word_start_seconds"]) - 5.00) < 1e-6
    assert viol["speed"] == 4

    shifted = check_speed_ramps(spec, words, pad_seconds=0.08, shift=True)
    assert shifted["pass"] is True
    assert shifted["spec"] is not None
    ramp = shifted["spec"]["speed_ramps"][0]
    assert ramp["end_seconds"] <= 5.00 - 0.08 + 1e-6


def test_ramp_pad_configurable_and_bleeped_words_count():
    spec = {"speed_ramps": [{"start_seconds": 1.0, "end_seconds": 2.0, "speed": 4}]}
    # Ramp ends at 2.00; word starts at 2.05. Default 80 ms pad → 2.00 > 2.05-0.08, fail.
    # 20 ms pad → 2.00 == 2.05-0.05, allowed (end is not strictly past word.start - pad).
    words = [{"start_seconds": 2.05, "end_seconds": 2.30, "word": "[bleep]", "bleeped": True, "confidence": 0.0}]
    tight = check_speed_ramps(spec, words, pad_seconds=0.08)
    assert tight["pass"] is False
    assert tight["violations"][0]["bleeped"] is True
    loose = check_speed_ramps(spec, words, pad_seconds=0.02)
    assert loose["pass"] is True
    # Plain asterisks also count as a bleeped word.
    stars = [{"start_seconds": 1.50, "end_seconds": 1.70, "word": "****", "confidence": 0.01}]
    assert check_speed_ramps(spec, stars, pad_seconds=0.08, confidence_min=0.5)["pass"] is False


def test_stepped_ramps_report_each_step():
    spec = {
        "segments": [
            {
                "start_seconds": 0.0,
                "end_seconds": 8.0,
                "speed_ramp": [
                    {"start_seconds": 0.0, "end_seconds": 2.0, "speed": 1},
                    {"start_seconds": 2.0, "end_seconds": 4.0, "speed": 2},
                    {"start_seconds": 4.0, "end_seconds": 6.0, "speed": 4},
                    {"start_seconds": 6.0, "end_seconds": 8.0, "speed": 6},
                ],
            }
        ]
    }
    words = [{"start_seconds": 5.10, "end_seconds": 5.40, "word": "go"}]
    result = check_speed_ramps(spec, words)
    speeds = [s["speed"] for s in result["steps"]]
    assert speeds == [1, 2, 4, 6]
    assert result["pass"] is False
    assert result["violations"][0]["speed"] == 4
    assert result["violations"][0]["word"] == "go"
    # 1x step covering a word is reported but does not fail.
    only_1x = check_speed_ramps(
        {"speed_ramps": [{"start_seconds": 5.0, "end_seconds": 6.0, "speed": 1}]},
        words,
    )
    assert only_1x["pass"] is True
    assert only_1x["steps"][0]["speed"] == 1


def test_check_output_wires_seams_and_ramps():
    envelope = [
        {"at_seconds": 1.2, "dbfs": -10.0},
        {"at_seconds": 1.5, "dbfs": -50.0},
        {"at_seconds": 2.0, "dbfs": -50.0},
    ]
    result = run_checks(
        cuts=[{"start_seconds": 1.2, "end_seconds": 2.0, "speed": 4}],
        transcript={"words": [{"start_seconds": 1.5, "end_seconds": 1.8, "word": "hi"}]},
        envelope=envelope,
        mode="strict",
        pad_seconds=0.0,
    )
    ids = {c["id"]: c for c in result["checks"]}
    assert "seams" in ids
    assert ids["seams"]["voiced_count"] >= 1
    assert "speed_ramps" in ids
    assert ids["speed_ramps"]["pass"] is False
    assert result["ok"] is False


def test_cli_check_seams_and_snap(tmp_path):
    cuts = tmp_path / "cuts.json"
    cuts.write_text(
        json.dumps([{"start_seconds": 1.0, "end_seconds": 2.0}]),
        encoding="utf-8",
    )
    envelope = tmp_path / "envelope.json"
    envelope.write_text(
        json.dumps(
            [
                {"at_seconds": 1.0, "dbfs": -12.0},
                {"at_seconds": 1.2, "dbfs": -50.0},
                {"at_seconds": 2.0, "dbfs": -50.0},
            ]
        ),
        encoding="utf-8",
    )
    code = cli_main(["check-seams", "--cuts", str(cuts), "--envelope", str(envelope), "--json"])
    assert code == 2
    snap_code = cli_main(
        ["snap-cuts", "--cuts", str(cuts), "--envelope", str(envelope), "--max-total-seconds", "30"]
    )
    assert snap_code == 0
