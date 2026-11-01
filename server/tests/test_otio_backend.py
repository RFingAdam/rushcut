from __future__ import annotations

import json
from pathlib import Path

from rushcut.backends import reset_backend
from rushcut.backends.otio import OtioBackend
from rushcut import tools


def _otio() -> OtioBackend:
    backend = OtioBackend()
    reset_backend(backend)
    return backend


def test_in_memory_edit_and_otio_roundtrip(tmp_path: Path):
    _otio()
    assert tools.ping()["mode"] == "memory"
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(1080, 1920, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(tmp_path / "clip.mp4"), 0.0, 8.0)
    tools.trim_event(0, 0, take_offset_seconds=2.0)
    tools.add_marker(1.5, "hook")
    dest = tmp_path / "short.otio"
    saved = tools.save_project(str(dest))
    assert saved["ok"] is True
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["OTIO_SCHEMA"] == "Timeline.1"
    assert data["tracks"]["children"][0]["kind"] == "Video"
    opened = tools.open_project(str(dest), discard_unsaved=True)
    assert opened["ok"] is True
    state = tools.get_project_state()
    assert state["events"][0]["length_seconds"] == 8.0
    assert state["width"] == 1080


def test_fcpxml_and_mlt_export(tmp_path: Path):
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(tmp_path / "a.mp4"), 0.0, 4.0)
    fcp = tmp_path / "seq.fcpxml"
    assert tools.save_project(str(fcp))["ok"] is True
    text = fcp.read_text(encoding="utf-8")
    assert "<fcpxml" in text
    assert 'version="1.9"' in text
    assert "asset-clip" in text
    mlt = tmp_path / "seq.mlt"
    assert tools.save_project(str(mlt))["ok"] is True
    mlt_text = mlt.read_text(encoding="utf-8")
    assert "<mlt" in mlt_text
    assert "producer" in mlt_text


def test_helpers_work_on_otio_backend():
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, r"D:\a.mp4", 0.0, 4.0)
    tools.add_video_event(0, r"D:\b.mp4", 4.0, 4.0)
    from rushcut.edit.helpers import apply_crossfade

    result = apply_crossfade(0, 0, 1, seconds=0.5)
    assert result["ok"] is True
    assert abs(result["right_start_seconds"] - 3.5) < 1e-6
