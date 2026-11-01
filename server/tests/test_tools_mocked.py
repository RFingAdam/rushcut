from __future__ import annotations

from rushcut import tools
from rushcut.render.presets import match_preset
from rushcut.render.sidecar import verify_output, write_sidecar


def test_ping_and_host_info(fake_session):
    def handler(method, params):
        if method == "ping":
            return {"ok": True, "host": "vegas-director", "mode": "extension"}
        if method == "host.info":
            return {"ok": True, "version": 2, "mode": "extension", "requests": 3}
        raise AssertionError(method)

    fake_session(handler)
    assert tools.ping()["mode"] == "extension"
    info = tools.host_info()
    assert info["version"] == 2
    assert info["used_mode"] == "live"


def test_new_project_and_add_track(fake_session):
    def handler(method, params):
        if method == "project.new":
            assert params.get("discard_unsaved") is True
            return {"ok": True}
        if method == "track.add":
            return {"ok": True, "track_index": params.get("index", 0), "type": params["type"]}
        raise AssertionError(method)

    fake_session(handler)
    assert tools.new_project(discard_unsaved=True)["ok"] is True
    added = tools.add_track("video", "V1", index=0)
    assert added["track_index"] == 0


def test_crossfade_moves_and_fades(fake_session):
    state = {
        "ok": True,
        "events": [
            {"track_index": 0, "event_index": 0, "start_seconds": 0.0, "length_seconds": 4.0},
            {"track_index": 0, "event_index": 1, "start_seconds": 4.0, "length_seconds": 4.0},
        ],
    }

    def handler(method, params):
        if method == "project.get_state":
            return state
        if method == "event.move":
            return {"ok": True, **params}
        if method == "event.set_fades":
            return {"ok": True, **params}
        raise AssertionError(method)

    host, _ = fake_session(handler)
    from rushcut.edit.helpers import apply_crossfade

    result = apply_crossfade(0, 0, 1, seconds=0.5)
    assert result["ok"] is True
    assert abs(result["right_start_seconds"] - 3.5) < 1e-6
    methods = [m for m, _ in host.calls]
    assert methods.count("event.set_fades") == 2


def test_ducking_writes_volume_points(fake_session):
    def handler(method, params):
        if method == "project.get_state":
            return {
                "ok": True,
                "events": [
                    {"track_index": 1, "event_index": 0, "start_seconds": 2.0, "length_seconds": 3.0},
                ],
            }
        if method == "envelope.set_points":
            assert params["envelope_type"] == "Volume"
            assert params["track_index"] == 0
            assert len(params["points"]) >= 4
            return {"ok": True, "points_applied": len(params["points"])}
        raise AssertionError(method)

    fake_session(handler)
    from rushcut.edit.helpers import apply_ducking

    result = apply_ducking(0, 1, reduction_db=-9)
    assert result["ok"] is True
    assert result["voice_events"] == 1


def test_render_with_preset_async_verify(fake_session, color_clip, tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(color_clip.read_bytes())

    def handler(method, params):
        if method == "render.list_templates":
            return {
                "ok": True,
                "templates": [
                    {
                        "renderer": "MAGIX AVC/AAC MP4",
                        "renderer_name": "MAGIX AVC/AAC MP4",
                        "template": "Internet HD 1080p",
                        "width": 1920,
                        "height": 1080,
                    },
                    {
                        "renderer": "MAGIX AVC/AAC MP4",
                        "renderer_name": "MAGIX AVC/AAC MP4",
                        "template": "Vertical 1080x1920",
                        "width": 1080,
                        "height": 1920,
                    },
                ],
            }
        if method == "render.start":
            assert params.get("async") is True
            assert params["template_name"] == "Vertical 1080x1920"
            return {"ok": True, "job_id": "abc123", "state": "running", "async": True}
        if method == "render.status":
            return {"ok": True, "job_id": "abc123", "state": "complete", "percent": 100}
        raise AssertionError(method)

    fake_session(handler)
    # verify_output will ffprobe the copied clip (320x180) — expect mismatch on vertical size.
    result = tools.render_with_preset(
        str(out),
        "vertical",
        edit_meta={"length": 2.2, "in_seconds": 0.1, "caption_style": "default", "zoom_count": 2, "music": False, "template": "timeline"},
        wait=True,
    )
    assert result["started"]["job_id"] == "abc123"
    assert result["status"]["state"] == "complete"
    assert result["sidecar"]["sidecar_path"].endswith(".edit.json")
    assert result["verify"]["ok"] is False  # 320x180 != 1080x1920
    assert result["verify"]["problems"]


def test_match_preset_picks_vertical():
    templates = [
        {"renderer": "MP4", "template": "1080p", "width": 1920, "height": 1080},
        {"renderer": "MAGIX AVC/AAC MP4", "template": "Vertical 1080x1920", "width": 1080, "height": 1920},
    ]
    matched = match_preset("vertical", templates)
    assert matched["ok"] is True
    assert matched["template_name"] == "Vertical 1080x1920"


def test_match_preset_finds_vertical_after_400_avi_templates():
    """Some installs list 500+ templates; MAGIX AVC/AAC MP4 sits after the old 400 cap."""
    templates = [
        {"renderer": "Video for Windows", "template": f"avi-{i}", "width": 1920, "height": 1080}
        for i in range(450)
    ]
    templates.append(
        {
            "renderer": "MAGIX AVC/AAC MP4",
            "renderer_name": "MAGIX AVC/AAC MP4",
            "template": "Internet HD 1080p 9:16",
            "width": 1080,
            "height": 1920,
        }
    )
    matched = match_preset("vertical", templates)
    assert matched["ok"] is True
    assert matched["template_name"] == "Internet HD 1080p 9:16"
    assert matched["listed_count"] == 451


def test_match_preset_avc_alias_without_mp4_in_renderer_name():
    templates = [
        {
            "renderer": "MAGIX AVC/AAC",
            "renderer_name": "MAGIX AVC/AAC",
            "template": "Portrait 1080x1920",
            "width": 1080,
            "height": 1920,
        }
    ]
    matched = match_preset("vertical", templates)
    assert matched["ok"] is True
    assert matched["template_name"] == "Portrait 1080x1920"


def test_render_with_preset_paginates_past_400(fake_session, color_clip, tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(color_clip.read_bytes())
    all_templates = [
        {"renderer": "Video for Windows", "template": f"avi-{i}", "width": 1920, "height": 1080}
        for i in range(450)
    ]
    vertical = {
        "renderer": "MAGIX AVC/AAC MP4",
        "renderer_name": "MAGIX AVC/AAC MP4",
        "template": "Vertical 1080x1920",
        "width": 1080,
        "height": 1920,
    }
    all_templates.append(vertical)
    calls = []

    def handler(method, params):
        if method == "render.list_templates":
            calls.append(dict(params))
            rf = (params.get("renderer_contains") or "").lower()
            items = all_templates
            if rf:
                items = [
                    t
                    for t in items
                    if rf in (t.get("renderer") or "").lower()
                    or rf in (t.get("renderer_name") or "").lower()
                    or rf in (t.get("template") or "").lower()
                ]
            limit = int(params.get("limit") or 0)
            offset = int(params.get("offset") or 0)
            if limit <= 0:
                page = items[offset:]
                truncated = False
            else:
                page = items[offset : offset + limit]
                truncated = offset + limit < len(items)
            # Old 400 cap would have dropped MAGIX AVC if we listed unfiltered.
            assert not (limit == 400 and not rf)
            return {
                "ok": True,
                "templates": page,
                "count": len(page),
                "truncated": truncated,
                "offset": offset,
                "limit": limit,
            }
        if method == "render.start":
            assert params["template_name"] == "Vertical 1080x1920"
            return {"ok": True, "job_id": "v9", "state": "running", "async": True}
        if method == "render.status":
            return {"ok": True, "job_id": "v9", "state": "complete", "percent": 100}
        raise AssertionError(method)

    fake_session(handler)
    result = tools.render_with_preset(str(out), "vertical", wait=True)
    assert result["started"]["job_id"] == "v9"
    assert result["preset"]["template_name"] == "Vertical 1080x1920"
    assert any(int(c.get("limit") or 0) != 400 for c in calls)


def test_render_with_preset_retries_old_host_limit_zero(fake_session, color_clip, tmp_path):
    """Pre-fix host: ``if (n >= limit)`` with limit=0 returned an empty list."""
    out = tmp_path / "out.mp4"
    out.write_bytes(color_clip.read_bytes())
    vertical = {
        "renderer": "MAGIX AVC/AAC MP4",
        "renderer_name": "MAGIX AVC/AAC MP4",
        "template": "Vertical 1080x1920",
        "width": 1080,
        "height": 1920,
    }

    def handler(method, params):
        if method == "render.list_templates":
            limit = int(params.get("limit") if params.get("limit") is not None else 200)
            if limit == 0:
                return {"ok": True, "templates": [], "count": 0}
            return {"ok": True, "templates": [vertical], "count": 1}
        if method == "render.start":
            return {"ok": True, "job_id": "old", "state": "running", "async": True}
        if method == "render.status":
            return {"ok": True, "job_id": "old", "state": "complete", "percent": 100}
        raise AssertionError(method)

    fake_session(handler)
    result = tools.render_with_preset(str(out), "vertical", wait=True)
    assert result["preset"]["template_name"] == "Vertical 1080x1920"
    assert result["started"]["job_id"] == "old"


def test_sidecar_schema(tmp_path, color_clip):
    dest = tmp_path / "short.mp4"
    dest.write_bytes(color_clip.read_bytes())
    payload = write_sidecar(
        str(dest),
        {
            "length": 27.0,
            "hook_start_time": 142.5,
            "caption_style": "default",
            "zoom_count": 2,
            "music": True,
            "template": "timeline",
        },
    )
    assert payload["ok"] is True
    edit = payload["edit"]
    assert edit["length"] == 27.0
    assert edit["hook_start_time"] == 142.5
    assert edit["caption_style"] == "default"
    assert edit["zoom_count"] == 2
    assert edit["music"] is True
    assert edit["template"] == "timeline"
    verify = verify_output(str(dest), expect_audio=True)
    assert verify["ok"] is True
    assert verify["probe"]["has_audio"] is True


def test_host_unreachable_message(fake_session, monkeypatch):
    host, session = fake_session(lambda m, p: {"ok": True}, reachable=False)

    def boom(*_a, **_k):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(session, "ensure", lambda **k: {"ok": False, "used_mode": "none", "error": "could not launch"})
    monkeypatch.setattr(session.client, "call", boom)
    result = tools.ping()
    assert result["ok"] is False
    assert result["used_mode"] == "none"
