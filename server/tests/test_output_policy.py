"""Output-root policy (#36) — Windows path semantics, work roots, profile CLI."""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest

from rushcut.cli import main as cli_main
from rushcut.output_policy import (
    EXAMPLE_WINDOWS_OUTPUT_ROOT,
    OUTSIDE_ROOTS,
    check_output_path,
    current_policy,
    is_under_root,
    make_work_dir,
    report_stale_partials,
    reset_policy_state,
    resolve_policy_path,
    set_active_profile,
)
from rushcut.profile import profile_from_data
from rushcut.tools import render_start

# Issue #36 example root (a D: tree). C: writes must be refused against it.
ISSUE_EXAMPLE_ROOT = r"D:\Media\Shows"
WINDOWS_EDITS = EXAMPLE_WINDOWS_OUTPUT_ROOT


def test_windows_drive_and_dotdot_semantics():
    root = ISSUE_EXAMPLE_ROOT
    assert is_under_root(r"D:\Media\Shows\edits\s.mp4", root)
    assert is_under_root(r"D:/Media/Shows/edits/s.mp4", root)
    assert not is_under_root(r"C:\out\s.mp4", root)
    assert not is_under_root(r"C:\Media\Shows\s.mp4", root)
    assert not is_under_root(r"D:\Media\Shows\..\Windows\s.mp4", root)
    assert not is_under_root(r"D:\Media\Shows\edits\..\..\other\s.mp4", root)
    assert is_under_root(r"D:\Media\Shows\edits\..\s.mp4", root)
    assert not is_under_root(r"D:\Media\Shows-other\s.mp4", root)
    assert is_under_root(r"c:\out\s.mp4", r"C:")
    assert not is_under_root(r"D:\out\s.mp4", r"C:")


def test_unc_paths():
    share = r"\\nas\edits"
    assert is_under_root(r"\\nas\edits\s.mp4", share)
    assert is_under_root(r"\\NAS\edits\sub\s.mp4", share)
    assert not is_under_root(r"\\nas\edits\..\other\s.mp4", share)
    assert not is_under_root(r"\\evil\share\s.mp4", share)
    assert not is_under_root(r"D:\edits\s.mp4", share)


def test_symlink_and_junction_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    hole = allowed / "hole"
    hole.symlink_to(outside, target_is_directory=True)
    escaped = hole / "s.mp4"
    assert not is_under_root(str(escaped), str(allowed))
    assert is_under_root(str(allowed / "ok.mp4"), str(allowed))


def test_windows_edits_root_refuses_c_and_other_d():
    assert is_under_root(WINDOWS_EDITS + r"\short.mp4", WINDOWS_EDITS)
    assert not is_under_root(r"C:\out\s.mp4", WINDOWS_EDITS)
    assert not is_under_root(r"C:\Users\Public\preview\parts\a.mp4", WINDOWS_EDITS)
    assert not is_under_root(r"D:\ChannelExports\s.mp4", WINDOWS_EDITS)
    assert not is_under_root(ISSUE_EXAMPLE_ROOT + r"\s.mp4", WINDOWS_EDITS)


def test_render_start_refuses_c_drive_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("RUSHCUT_OUTPUT_ROOTS", ISSUE_EXAMPLE_ROOT)
    reset_policy_state()
    outside = tmp_path / "outside" / "s.mp4"
    # Lexical Windows path: refused before any mkdir.
    result = render_start(r"C:\out\s.mp4")
    assert result["ok"] is False
    assert result["error"] == OUTSIDE_ROOTS
    assert not Path(r"C:\out\s.mp4").exists()
    assert not Path(r"C:\out").exists()

    monkeypatch.setenv("RUSHCUT_OUTPUT_ROOTS", str(tmp_path / "allowed"))
    reset_policy_state()
    (tmp_path / "allowed").mkdir()
    result = render_start(str(outside))
    assert result["ok"] is False
    assert result["error"] == OUTSIDE_ROOTS
    assert not outside.exists()
    assert not outside.parent.exists()


def test_profile_pins_windows_edits_root():
    profile = profile_from_data(
        {"id": "windows-edits", "output_roots": [WINDOWS_EDITS]}
    )
    policy = current_policy(profile=profile)
    assert policy.output_roots == (WINDOWS_EDITS,)
    set_active_profile(profile)
    assert check_output_path(ISSUE_EXAMPLE_ROOT + r"\x.mp4")["error"] == OUTSIDE_ROOTS
    assert check_output_path(r"C:\out\x.mp4")["error"] == OUTSIDE_ROOTS
    assert check_output_path(WINDOWS_EDITS + r"\x.mp4") is None


def test_unrestricted_warns_once(caplog, tmp_path):
    reset_policy_state()
    with caplog.at_level(logging.WARNING, logger="rushcut"):
        assert check_output_path(str(tmp_path / "a.mp4")) is None
        assert check_output_path(str(tmp_path / "b.mp4")) is None
    warnings = [r.message for r in caplog.records if "unrestricted" in r.message]
    assert len(warnings) == 1


def test_work_dir_not_in_temp_or_c(monkeypatch, tmp_path, color_clip):
    out_root = tmp_path / "out"
    work_root = tmp_path / "work"
    out_root.mkdir()
    work_root.mkdir()
    monkeypatch.setenv("RUSHCUT_OUTPUT_ROOTS", str(out_root))
    monkeypatch.setenv("RUSHCUT_WORK_ROOTS", str(work_root))
    reset_policy_state()

    watched_temp = Path(tempfile.gettempdir())
    before = set(watched_temp.glob("nle-ffmpeg-*")) | set(watched_temp.glob("rushcut-*"))
    scratch = make_work_dir("nle-ffmpeg-")
    assert Path(scratch).is_relative_to(work_root)
    after = set(watched_temp.glob("nle-ffmpeg-*")) | set(watched_temp.glob("rushcut-*"))
    assert after <= before

    from rushcut.analysis.edit_ops import cut_media
    from rushcut.backends import reset_backend
    from rushcut.backends.ffmpeg import FfmpegBackend

    monkeypatch.setenv("DIRECTOR_EDITOR", "ffmpeg")
    reset_backend(FfmpegBackend())
    dest = out_root / "cut.mp4"
    result = cut_media(str(color_clip), 0.0, 0.6, str(dest))
    assert result["ok"] is True, result
    after_cut = set(watched_temp.glob("nle-ffmpeg-*")) | set(watched_temp.glob("rushcut-*"))
    assert after_cut <= before
    assert dest.is_file()
    assert not (Path("C:\\") / "out" / "s.mp4").exists()


def test_profile_validate_shows_effective_roots(tmp_path, capsys):
    path = tmp_path / "edit_profile.yaml"
    path.write_text(
        "schema: rushcut.profile/v1\n"
        "id: windows-edits\n"
        "output_roots:\n"
        f"  - '{WINDOWS_EDITS}'\n",
        encoding="utf-8",
    )
    assert cli_main(["profile", "validate", str(path)]) == 0
    printed = capsys.readouterr().out
    assert WINDOWS_EDITS.replace("\\", "\\\\") in printed or WINDOWS_EDITS in printed
    assert "output_roots" in printed
    assert "work_roots" in printed
    assert "restricted" in printed


def test_stale_partials_are_reported_not_deleted(tmp_path, caplog):
    leftover = tmp_path / "clip.partial.mp4"
    leftover.write_bytes(b"truncated")
    with caplog.at_level(logging.WARNING, logger="rushcut"):
        found = report_stale_partials([str(tmp_path)])
    assert leftover.is_file()
    assert any(str(leftover) == p or leftover.name in p for p in found)
    assert any("stale partial" in r.message for r in caplog.records)


def test_resolve_policy_path_collapses_dotdot():
    assert resolve_policy_path(r"D:\Media\Shows\..\Windows\s.mp4") == r"D:\Media\Windows\s.mp4"
    assert resolve_policy_path(r"\\nas\edits\sub\..\file.mp4") == r"\\nas\edits\file.mp4"


def test_dotdot_after_a_symlink_climbs_from_where_the_link_points(tmp_path):
    allowed = tmp_path / "allowed"
    target = tmp_path / "elsewhere" / "deep"
    allowed.mkdir()
    target.mkdir(parents=True)
    (allowed / "hop").symlink_to(target, target_is_directory=True)
    sneaky = os.path.join(str(allowed), "hop", "..", "x.json")
    # What the OS does with it: ".." leaves the link's target, not the link.
    Path(sneaky).write_text("{}")
    landed = (tmp_path / "elsewhere" / "x.json").resolve()
    assert landed.exists()
    assert not (allowed / "x.json").exists()
    assert resolve_policy_path(sneaky) == str(landed)
    assert not is_under_root(sneaky, str(allowed))


def _outside_roots(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("RUSHCUT_OUTPUT_ROOTS", str(allowed))
    reset_policy_state()
    return allowed, tmp_path / "outside"


def test_transcribe_refuses_a_transcript_or_cache_outside_the_roots(monkeypatch, tmp_path):
    from rushcut.analysis.transcript import analyze_transcript

    allowed, outside = _outside_roots(monkeypatch, tmp_path)
    media = allowed / "in.wav"
    media.write_bytes(b"")
    for kwargs in (
        {"json_path": str(outside / "t.json")},
        {"run_dir": str(outside)},
        {"json_path": str(allowed / "t.json"), "run_dir": str(outside)},
        {"cache_dir": str(outside)},
    ):
        result = analyze_transcript(str(media), **kwargs)
        assert result["ok"] is False, (kwargs, result)
        assert result["error"] == OUTSIDE_ROOTS, (kwargs, result)
    assert not outside.exists()
    assert not (allowed / "t.json").exists()


def test_check_parity_refuses_json_outside_the_roots(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    from rushcut.quality.parity import check_parity

    allowed, outside = _outside_roots(monkeypatch, tmp_path)
    approved, candidate = allowed / "a.mp4", allowed / "b.mp4"
    approved.write_bytes(b"")
    candidate.write_bytes(b"")
    result = check_parity(str(approved), str(candidate), json_path=str(outside / "parity.json"))
    assert result["ok"] is False
    assert result["error"] == OUTSIDE_ROOTS
    assert result["id"] == "parity"
    assert not outside.exists()
