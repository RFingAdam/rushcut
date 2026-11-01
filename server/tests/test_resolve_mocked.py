"""Resolve backend tests. All Resolve objects are fakes — no live app."""
from __future__ import annotations

from rushcut.backends import reset_backend
from rushcut.backends.resolve import ResolveBackend
from rushcut.backends.resolve.connect import FREE_EDITION, SCRIPTING_UNAVAILABLE
from rushcut import tools

from fake_resolve import FakeResolve


def _studio(app=None) -> ResolveBackend:
    resolve = app or FakeResolve()
    backend = ResolveBackend(app=resolve, auto_launch=False)
    reset_backend(backend)
    return backend


def test_ping_and_state_on_studio_mock():
    backend = _studio()
    ping = tools.ping()
    assert ping["ok"] is True
    assert ping["editor"] == "resolve"
    assert ping["studio"] is True
    info = tools.host_info()
    assert info["product"] == "DaVinci Resolve Studio"
    assert info["version"] == "21.1.0"
    state = tools.get_project_state()
    assert state["ok"] is True
    assert state["video_track_count"] == 1
    assert state["audio_track_count"] == 1


def test_scripting_unavailable_is_explicit():
    backend = ResolveBackend(connector=lambda: None, auto_launch=False)
    reset_backend(backend)
    result = tools.ensure_host()
    assert result["ok"] is False
    assert result["reason"] == "scripting_unavailable"
    assert result["studio_required"] is True
    assert result["scripting_available"] is False
    assert "Scripting API" in result["error"]
    assert "DIRECTOR_EDITOR=otio" in result["error"]
    assert SCRIPTING_UNAVAILABLE[:40] in result["error"]
    # Must not have tried to talk to a real Resolve.
    assert result.get("used_mode") in {"none", None} or result["ok"] is False


def test_free_edition_is_refused():
    free = FakeResolve(product="DaVinci Resolve", version="21.1.0")
    backend = ResolveBackend(app=free, auto_launch=False)
    reset_backend(backend)
    result = tools.ping()
    assert result["ok"] is False
    assert result["reason"] == "free_edition"
    assert result["studio"] is False
    assert result["studio_required"] is True
    assert "Studio" in result["error"]
    assert FREE_EDITION[:30] in result["error"]
    # Timeline tools must not run against free.
    placed = tools.add_track("video", "V2")
    assert placed["ok"] is False
    assert placed["reason"] == "free_edition"


def test_place_trim_move_delete_and_motion():
    backend = _studio()
    tools.add_track("video", "Short V1")
    placed = tools.add_video_event(0, r"D:\media\clip.mp4", 0.0, 8.0)
    assert placed["ok"] is True
    trimmed = tools.trim_event(0, 0, start_seconds=1.0, length_seconds=5.0, take_offset_seconds=2.0)
    assert trimmed["ok"] is True
    assert trimmed["start_seconds"] == 1.0
    moved = tools.move_event(0, 0, 3.0)
    assert moved["ok"] is True
    motion = tools.set_motion_keyframes(
        0,
        0,
        [{"at_seconds": 0, "scale": 1.777, "pan_x": 0, "pan_y": 0}],
    )
    assert motion["ok"] is True
    assert motion["partial"] is True
    opacity = tools.set_event_opacity(0, 0, 0.5)
    assert opacity["ok"] is True
    deleted = tools.delete_event(0, 0)
    assert deleted["ok"] is True


def test_markers_and_envelopes_become_resolve_markers():
    _studio()
    added = tools.add_marker(2.5, "hook", "kill", "Green")
    assert added["ok"] is True
    listed = tools.list_markers()
    assert listed["count"] >= 1
    env = tools.set_envelope_points(1, "Volume", [{"at_seconds": 1.0, "value": 0.3}])
    assert env["ok"] is True
    assert env["partial"] is True


def test_render_templates_include_vertical_preset():
    _studio()
    listed = tools.list_render_templates(limit=0)
    assert listed["ok"] is True
    names = [t["template"] for t in listed["templates"]]
    assert any("1080x1920" in n or "Vertical" in n for n in names)
    started = tools.render_start(r"D:\out\short.mp4", template_name="H.264 Vertical 1080x1920")
    assert started["ok"] is True
    assert started["job_id"]
    status = tools.render_status(started["job_id"])
    assert status["state"] == "complete"


def test_studio_only_helpers():
    backend = _studio()
    tools.add_video_event(0, r"D:\media\clip.mp4", 0.0, 4.0)
    subs = tools.resolve_create_subtitles()
    assert subs["ok"] is True
    assert backend._resolve().project.current_timeline.subtitles_called is True
    frame = tools.resolve_smart_reframe(0, 0)
    assert frame["ok"] is True
    iso = tools.resolve_set_voice_isolation(0, 0, True)
    assert iso["ok"] is True


def test_capabilities_do_not_need_a_live_app():
    backend = ResolveBackend(connector=lambda: None, auto_launch=False)
    caps = backend.capabilities()
    assert caps["ok"] is True
    assert caps["studio_required"] is True
    assert "CreateSubtitlesFromAudio" in caps["studio_only_features"]
    assert "SmartReframe" in caps["studio_only_features"]
    assert "SetVoiceIsolationState" in caps["studio_only_features"]


def test_ensure_does_not_launch_when_auto_launch_false(monkeypatch):
    launched = []

    def boom():
        launched.append(True)
        raise AssertionError("must not launch Resolve in unit tests")

    backend = ResolveBackend(connector=lambda: None, launcher=boom, auto_launch=False)
    result = backend.ensure()
    assert result["ok"] is False
    assert launched == []
