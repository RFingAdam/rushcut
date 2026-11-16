"""score_hook (#64): hook-strength primitive for any director."""
from __future__ import annotations

import inspect
import time

import rushcut
from rushcut.analysis.hook import score_hook
from rushcut.cli import main as cli_main
from tests.fixtures.hook import approved_short, menu_open_clip


REQUIRED_FIELDS = (
    "motion_energy",
    "audio_loudness_rise",
    "speech_first_second",
    "text_card_present",
    "face_present",
    "hook_score",
    "breakdown",
)


def test_score_hook_missing_file(tmp_path):
    result = score_hook(str(tmp_path / "missing.mp4"))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_score_hook_rejects_non_positive_window(color_clip):
    result = score_hook(str(color_clip), window_s=0)
    assert result["ok"] is False
    result = score_hook(str(color_clip), window_s=-1)
    assert result["ok"] is False


def test_score_hook_interface_is_path_and_window():
    sig = inspect.signature(rushcut.score_hook)
    assert list(sig.parameters) == ["path", "window_s"]
    assert sig.parameters["window_s"].default == 3.0


def test_score_hook_returns_signals_and_breakdown(tmp_path, ffmpeg_bin):
    clip = approved_short(ffmpeg_bin, tmp_path / "hook.mp4")
    result = score_hook(str(clip), window_s=3)
    assert result["ok"] is True
    for key in REQUIRED_FIELDS:
        assert key in result
    assert 0.0 <= result["motion_energy"] <= 1.0
    assert 0.0 <= result["audio_loudness_rise"] <= 1.0
    assert result["speech_first_second"] in (True, False)
    assert result["text_card_present"] in (True, False)
    assert result["face_present"] in (True, False)
    assert 0.0 <= result["hook_score"] <= 1.0
    breakdown = result["breakdown"]
    for key in (
        "motion_energy",
        "audio_loudness_rise",
        "speech_first_second",
        "text_card_present",
        "face_present",
    ):
        row = breakdown[key]
        assert "value" in row and "weight" in row and "contribution" in row
        assert 0.0 <= row["value"] <= 1.0
        assert 0.0 <= row["weight"] <= 1.0
        assert abs(row["contribution"] - round(row["value"] * row["weight"], 4)) < 1e-9
    weight_sum = sum(row["weight"] for row in breakdown.values())
    assert abs(weight_sum - 1.0) < 1e-6
    score_sum = sum(row["contribution"] for row in breakdown.values())
    assert abs(result["hook_score"] - round(score_sum, 4)) < 1e-6


def test_score_hook_is_deterministic(tmp_path, ffmpeg_bin):
    clip = approved_short(ffmpeg_bin, tmp_path / "same.mp4")
    first = score_hook(str(clip), window_s=3)
    second = score_hook(str(clip), window_s=3)
    assert first["ok"] is True
    assert first == second


def test_approved_short_scores_higher_than_menu_open(tmp_path, ffmpeg_bin):
    approved = approved_short(ffmpeg_bin, tmp_path / "approved_short.mp4")
    menu = menu_open_clip(ffmpeg_bin, tmp_path / "menu_open.mp4")
    good = score_hook(str(approved), window_s=3)
    bad = score_hook(str(menu), window_s=3)
    assert good["ok"] is True
    assert bad["ok"] is True
    assert good["hook_score"] > bad["hook_score"]
    assert good["hook_score"] >= 0.4
    assert bad["hook_score"] < 0.15
    assert good["text_card_present"] is True
    assert good["speech_first_second"] is True
    assert good["motion_energy"] > bad["motion_energy"]
    assert good["audio_loudness_rise"] > bad["audio_loudness_rise"]
    assert bad["speech_first_second"] is False
    assert bad["text_card_present"] is False


def test_score_hook_registered_on_server(tmp_path):
    assert rushcut.score_hook(str(tmp_path / "missing.mp4"))["ok"] is False


def test_score_hook_cli(tmp_path, ffmpeg_bin, capsys):
    clip = menu_open_clip(ffmpeg_bin, tmp_path / "cli.mp4")
    code = cli_main(["score-hook", str(clip), "--window", "3"])
    captured = capsys.readouterr()
    assert code == 0
    assert "hook_score" in captured.out


def test_score_hook_under_five_seconds_per_minute(tmp_path, ffmpeg_bin):
    clip = approved_short(ffmpeg_bin, tmp_path / "budget.mp4", duration=4.0)
    t0 = time.perf_counter()
    result = score_hook(str(clip), window_s=3)
    elapsed = time.perf_counter() - t0
    assert result["ok"] is True
    # 5 s per minute of video. A 4 s clip must finish well under 5 s.
    assert elapsed < 5.0
