from __future__ import annotations

import json

from rushcut.cli import main as cli_main
from rushcut.quality import (
    check_required_features,
    check_source_preference,
    check_speech_boundaries,
    check_turn_completeness,
    classify_source,
    run_checks,
)

WORDS = [
    {"start_seconds": 1.00, "end_seconds": 1.40, "word": "hello", "speaker": "A"},
    {"start_seconds": 1.45, "end_seconds": 1.80, "word": "there", "speaker": "A"},
    {"start_seconds": 2.00, "end_seconds": 2.50, "word": "reply", "speaker": "B"},
    {"start_seconds": 5.00, "end_seconds": 5.40, "word": "later", "speaker": "A"},
]


def test_cut_inside_word_fails():
    result = check_speech_boundaries(
        [{"start_seconds": 1.20, "end_seconds": 3.0}],
        WORDS,
        pad_seconds=0.12,
        mode="fail",
    )
    assert result["pass"] is False
    assert result["violations"][0]["role"] == "in"
    assert result["violations"][0]["at_seconds"] == 1.2


def test_padded_silence_cut_passes():
    # Word starts at 1.00; pad 0.12 → in-point 0.88 sits on the allowed edge.
    result = check_speech_boundaries(
        [{"start_seconds": 0.88, "end_seconds": 1.92}],
        WORDS[:2],
        pad_seconds=0.12,
        mode="fail",
    )
    assert result["pass"] is True
    assert result["violations"] == []


def test_cut_inside_pad_fails():
    result = check_speech_boundaries(
        [0.90],
        WORDS[:1],
        pad_seconds=0.12,
        mode="fail",
    )
    assert result["pass"] is False


def test_speech_boundary_off():
    result = check_speech_boundaries([1.2], WORDS, mode="off")
    assert result["pass"] is True
    assert result["skipped"] is True


def test_turn_completeness_drops_reply():
    # Keep A's first turn (1.00-1.80) but cut before B's reply at 2.00.
    result = check_turn_completeness(
        [{"start_seconds": 0.8, "end_seconds": 1.9}],
        WORDS,
        reply_window_seconds=2.5,
        mode="fail",
    )
    assert result["pass"] is False
    assert result["violations"][0]["speaker"] == "B"
    assert result["violations"][0]["in_reply_to"] == "A"


def test_turn_completeness_keeps_reply():
    result = check_turn_completeness(
        [{"start_seconds": 0.8, "end_seconds": 2.6}],
        WORDS[:3],
        reply_window_seconds=2.5,
        mode="fail",
    )
    assert result["pass"] is True


def test_required_features_missing():
    result = check_required_features(
        ["captions", "loudness_normalize"],
        applied=["loudness_normalize"],
    )
    assert result["pass"] is False
    assert result["missing"] == ["captions"]


def test_required_features_inferred_from_edit():
    result = check_required_features(
        ["captions"],
        edit={"captions": {"style": "default"}},
    )
    assert result["pass"] is True


def test_source_preference_rejects_vod_when_local_exists():
    result = check_source_preference(
        used=[{"path": "media/twitch-vod.mp4", "kind": "platform_vod"}],
        available=[
            {"path": "recordings/obs-take.mkv", "kind": "local_recording"},
            {"path": "media/twitch-vod.mp4", "kind": "platform_vod"},
        ],
    )
    assert result["pass"] is False
    assert result["violations"]


def test_source_preference_ok_when_local_used():
    result = check_source_preference(
        used=["recordings/obs-take.mkv:local_recording"],
        available=["recordings/obs-take.mkv:local_recording", "media/twitch-vod.mp4:platform_vod"],
    )
    assert result["pass"] is True


def test_classify_source_hints():
    assert classify_source("C:/captures/obs-2026.mkv") == "local_recording"
    assert classify_source("downloads/twitch-vod-123.mp4") == "platform_vod"
    assert classify_source("clip.mp4") == "unknown"


def test_run_checks_and_cli(tmp_path):
    profile = tmp_path / "edit_profile.yaml"
    profile.write_text(
        """
schema: rushcut.profile/v1
id: gate
required_features: [captions]
source_preference:
  order: [local_recording, platform_vod]
  on_mismatch: fail
quality:
  speech_boundary: {pad_seconds: 0.12, mode: fail}
  turn_completeness: {reply_window_seconds: 2.5, mode: warn}
  gate: strict
""",
        encoding="utf-8",
    )
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"words": WORDS}), encoding="utf-8")
    edit = tmp_path / "edit.json"
    edit.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "path": "media/twitch-vod.mp4",
                        "kind": "platform_vod",
                        "start_seconds": 1.2,
                        "end_seconds": 1.9,
                    }
                ],
                "applied_features": [],
                "sources": [
                    {"path": "media/twitch-vod.mp4", "kind": "platform_vod"},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = run_checks(
        target=str(edit),
        profile=str(profile),
        transcript=str(transcript),
        available_sources=[
            {"path": "recordings/obs.mkv", "kind": "local_recording"},
            {"path": "media/twitch-vod.mp4", "kind": "platform_vod"},
        ],
    )
    assert result["ok"] is False
    ids = {c["id"]: c for c in result["checks"]}
    assert ids["speech_boundaries"]["pass"] is False
    assert ids["required_features"]["missing"] == ["captions"]
    assert ids["source_preference"]["pass"] is False
    # Turn completeness is warn-mode in the profile, so it does not fail the gate
    # by itself, but it should still be reported.
    assert ids["turn_completeness"]["warned"] is True or ids["turn_completeness"]["pass"] is True

    code = cli_main(
        [
            "check",
            str(edit),
            "--profile",
            str(profile),
            "--transcript",
            str(transcript),
            "--available-sources",
            "recordings/obs.mkv:local_recording,media/twitch-vod.mp4:platform_vod",
        ]
    )
    assert code == 2


def test_cli_pass_with_applied_features(tmp_path):
    profile = tmp_path / "edit_profile.yaml"
    profile.write_text(
            "schema: rushcut.profile/v1\nrequired_features: [captions]\n"
            "quality:\n  gate: strict\n  speech_boundary:\n    mode: \"off\"\n",
        encoding="utf-8",
    )
    edit = tmp_path / "edit.json"
    edit.write_text(
        json.dumps(
            {
                "segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "path": "local.mp4"}],
                "applied_features": ["captions"],
            }
        ),
        encoding="utf-8",
    )
    assert cli_main(["check", str(edit), "--profile", str(profile), "--mode", "strict"]) == 0


def test_check_output_mcp_shape():
    from rushcut.cli import check_output

    result = check_output(
        cuts=[{"start_seconds": 1.2, "end_seconds": 2.0}],
        transcript={"words": WORDS[:2]},
        pad_seconds=0.0,
        mode="strict",
    )
    assert "checks" in result
    assert result["ok"] is False
