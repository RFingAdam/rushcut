"""Host-tracked dirty / discard guards (#33)."""
from __future__ import annotations

from rushcut import tools
from rushcut.backends import reset_backend
from rushcut.backends.otio import OtioBackend
from rushcut.backends.protocol import refuse_discard


def test_refuse_discard_messages():
    assert refuse_discard(
        track_count=0,
        event_count=0,
        untitled=True,
        created_by_rushcut=False,
        project_id="abc",
        discard_unsaved=False,
        save_current_to=None,
        requested_project_id=None,
    )
    err = refuse_discard(
        track_count=2,
        event_count=3,
        untitled=True,
        created_by_rushcut=False,
        project_id="abc",
        discard_unsaved=False,
        save_current_to=None,
        requested_project_id=None,
    )
    assert err is not None
    assert "Untitled project" in err
    assert "tracks=2" in err and "events=3" in err
    assert refuse_discard(
        track_count=2,
        event_count=3,
        untitled=True,
        created_by_rushcut=True,
        project_id="abc",
        discard_unsaved=True,
        save_current_to=None,
        requested_project_id=None,
    ) is None
    assert refuse_discard(
        track_count=2,
        event_count=3,
        untitled=True,
        created_by_rushcut=False,
        project_id="abc",
        discard_unsaved=True,
        save_current_to=None,
        requested_project_id="abc",
    ) is None
    blocked = refuse_discard(
        track_count=2,
        event_count=3,
        untitled=True,
        created_by_rushcut=False,
        project_id="abc",
        discard_unsaved=True,
        save_current_to=None,
        requested_project_id=None,
    )
    assert blocked is not None
    assert "not created by rushcut" in blocked


def test_new_project_refuses_untitled_host_edits():
    backend = OtioBackend()
    reset_backend(backend)
    tools.add_track("video", "V1")
    tools.add_video_event(0, r"D:\clip.mp4", 0.0, 4.0)
    state = tools.get_project_state()
    assert state["dirty"] is True
    assert state["event_count"] if "event_count" in state else len(state["events"]) >= 1
    refused = tools.new_project()
    assert refused["ok"] is False
    assert "tracks=" in refused["error"]
    assert "events=" in refused["error"]
    still = tools.new_project(discard_unsaved=True)
    assert still["ok"] is False
    assert "project_id" in still["error"]
    ok = tools.new_project(discard_unsaved=True, project_id=state["project_id"])
    assert ok["ok"] is True


def test_rushcut_created_project_can_discard():
    backend = OtioBackend()
    reset_backend(backend)
    created = tools.new_project(discard_unsaved=True)
    assert created["ok"] is True
    tools.add_track("video", "V1")
    tools.add_video_event(0, r"D:\clip.mp4", 0.0, 3.0)
    again = tools.new_project(discard_unsaved=True)
    assert again["ok"] is True
