from __future__ import annotations

import json

import pytest

from rushcut.cli import main as cli_main
from rushcut.profile import load_profile, profile_from_data, validate_profile_file


def test_required_features_and_source_preference_roundtrip(tmp_path):
    path = tmp_path / "edit_profile.yaml"
    path.write_text(
        """
schema: rushcut.profile/v1
id: example
required_features:
  - captions
  - loudness_normalize
source_preference:
  order: [local_recording, platform_vod]
  on_mismatch: fail
quality:
  speech_boundary:
    pad_seconds: 0.2
    mode: fail
  turn_completeness:
    reply_window_seconds: 2.0
    mode: warn
  gate: strict
intent: "tighten pauses; keep replies"
""",
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile.id == "example"
    assert profile.required_features == ["captions", "loudness_normalize"]
    assert profile.source_preference.order[0] == "local_recording"
    assert profile.quality.speech_boundary.pad_seconds == 0.2
    # Unknown keys from the rest of #17 are kept.
    dumped = profile.model_dump(by_alias=True)
    assert dumped["schema"] == "rushcut.profile/v1"
    assert dumped.get("intent")


def test_shorts_and_output_roots_roundtrip(tmp_path):
    path = tmp_path / "edit_profile.yaml"
    path.write_text(
        """
schema: rushcut.profile/v1
id: shorts
shorts:
  max_seconds: 30.0
  loop: true
output_roots:
  - D:\\\\MediaOut
""",
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile.shorts is not None
    assert profile.shorts.max_seconds == 30.0
    assert profile.shorts.loop is True
    assert profile.output_roots
    checked = validate_profile_file(path)
    assert checked["ok"] is True
    assert checked["shorts"]["max_seconds"] == 30.0
    assert checked["output_roots"]


def test_secret_keys_rejected():
    with pytest.raises(ValueError, match="secrets are not allowed"):
        profile_from_data({"id": "x", "openai_api_key": "sk-nope"})


def test_negative_pad_named_path():
    with pytest.raises(ValueError, match="pad_seconds"):
        profile_from_data({"quality": {"speech_boundary": {"pad_seconds": -1}}})


def test_yaml_off_is_not_boolean(tmp_path):
    path = tmp_path / "edit_profile.yaml"
    path.write_text(
        "quality:\n  gate: off\n  speech_boundary:\n    mode: off\n",
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile.quality.gate == "off"
    assert profile.quality.speech_boundary.mode == "off"


def test_json_profile(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"id": "j", "required_features": ["captions"]}))
    profile = load_profile(path)
    assert profile.required_features == ["captions"]


def test_validate_profile_cli(tmp_path):
    path = tmp_path / "edit_profile.yaml"
    path.write_text("schema: rushcut.profile/v1\nid: cli\n", encoding="utf-8")
    assert cli_main(["profile", "validate", str(path)]) == 0
    missing = validate_profile_file(tmp_path / "nope.yaml")
    assert missing["ok"] is False
