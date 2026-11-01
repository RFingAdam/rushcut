"""FFmpeg backend + file-level primitives, using tiny synthetic clips."""
from __future__ import annotations

from pathlib import Path

from rushcut.analysis.captions import generate_captions
from rushcut.analysis.edit_ops import concat_media, cut_media, mix_audio, normalize_audio, reframe_media
from rushcut.analysis.probe import probe_media
from rushcut.backends import create_backend, editor_from_env, list_editors, reset_backend
from rushcut.backends.ffmpeg import FfmpegBackend
from rushcut import tools
from rushcut.edit.helpers import apply_crossfade


def _ffmpeg(monkeypatch) -> FfmpegBackend:
    monkeypatch.setenv("DIRECTOR_EDITOR", "ffmpeg")
    backend = FfmpegBackend()
    reset_backend(backend)
    return backend


def test_ffmpeg_in_editor_list(monkeypatch):
    monkeypatch.delenv("DIRECTOR_EDITOR", raising=False)
    monkeypatch.delenv("VEGAS_DIRECTOR_EDITOR", raising=False)
    listed = list_editors()
    ids = [e["id"] for e in listed["editors"]]
    assert ids == ["ffmpeg", "vegas", "resolve", "otio"]
    assert listed["editor"] == "vegas"
    monkeypatch.setenv("DIRECTOR_EDITOR", "headless")
    assert editor_from_env() == "ffmpeg"
    assert create_backend("ffmpeg").name == "ffmpeg"


def test_cut_media(color_clip, tmp_path):
    out = tmp_path / "cut.mp4"
    result = cut_media(str(color_clip), 0.3, 1.1, str(out))
    assert result["ok"] is True
    probe = probe_media(str(out))
    assert probe["ok"] is True
    assert 0.5 <= probe["duration_seconds"] <= 1.2


def test_concat_media(color_clip, tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    assert cut_media(str(color_clip), 0.0, 0.8, str(a))["ok"]
    assert cut_media(str(color_clip), 0.8, 1.6, str(b))["ok"]
    out = tmp_path / "cat.mp4"
    result = concat_media([str(a), str(b)], str(out))
    assert result["ok"] is True
    probe = probe_media(str(out))
    assert probe["ok"] is True
    assert 1.2 <= probe["duration_seconds"] <= 2.2


def test_reframe_media(color_clip, tmp_path):
    out = tmp_path / "portrait.mp4"
    result = reframe_media(str(color_clip), 180, 320, str(out))
    assert result["ok"] is True
    probe = probe_media(str(out))
    assert probe["width"] == 180
    assert probe["height"] == 320


def test_mix_and_normalize_audio(color_clip, tmp_path):
    mixed = tmp_path / "mix.m4a"
    result = mix_audio([str(color_clip), str(color_clip)], str(mixed))
    assert result["ok"] is True
    probe = probe_media(str(mixed))
    assert probe["has_audio"] is True
    norm = tmp_path / "norm.mp4"
    result = normalize_audio(str(color_clip), str(norm))
    assert result["ok"] is True
    assert probe_media(str(norm))["has_audio"] is True


def test_backend_probe_cut_concat_render(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    assert tools.ping()["editor"] == "ffmpeg"
    caps = tools.editor_capabilities()
    assert caps["methods"]["render"] is True
    assert caps["live"] is False
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(320, 180, 24)
    tools.add_track("video", "V1")
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    cut_media(str(color_clip), 0.0, 0.7, str(first))
    cut_media(str(color_clip), 0.7, 1.4, str(second))
    tools.add_video_event(0, str(first), 0.0, -1)
    tools.add_video_event(0, str(second), 0.7, -1)
    state = tools.get_project_state()
    assert len(state["events"]) == 2
    out = tmp_path / "concat.mp4"
    started = tools.render_start(str(out), async_render=False)
    assert started["ok"] is True, started
    probe = probe_media(str(out))
    assert probe["ok"] is True
    assert probe["has_video"] is True
    assert 1.0 <= probe["duration_seconds"] <= 2.2


def test_backend_trim_and_reframe(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(180, 320, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, -1)
    trimmed = tools.trim_event(0, 0, start_seconds=0.0, length_seconds=0.9, take_offset_seconds=0.2)
    assert trimmed["ok"] is True
    tools.set_motion_keyframes(0, 0, [{"at_seconds": 0.0, "scale": 1.2, "pan_x": 0.0, "pan_y": 0.0}])
    out = tmp_path / "reframe.mp4"
    started = tools.render_start(str(out), async_render=False)
    assert started["ok"] is True, started
    probe = probe_media(str(out))
    assert probe["width"] == 180
    assert probe["height"] == 320
    assert 0.5 <= probe["duration_seconds"] <= 1.4


def test_backend_transition_and_audio_mix(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(320, 180, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 1.0)
    tools.add_video_event(0, str(color_clip), 1.0, 1.0)
    xf = apply_crossfade(0, 0, 1, seconds=0.3)
    assert xf["ok"] is True
    tools.add_track("audio", "A1")
    tools.add_audio_event(1, str(color_clip), 0.0, 1.5)
    tools.set_event_gain(1, 0, normalize=True)
    out = tmp_path / "xfade.mp4"
    started = tools.render_start(str(out), async_render=False)
    assert started["ok"] is True, started
    probe = probe_media(str(out))
    assert probe["has_audio"] is True
    assert probe["has_video"] is True
    # Two 1s clips with 0.3s overlap → ~1.7s
    assert 1.2 <= probe["duration_seconds"] <= 2.4


def test_backend_captions_burn_in(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    caps = generate_captions(
        path=str(color_clip),
        segments=[{"start_seconds": 0.0, "end_seconds": 1.0, "text": "hello"}],
        output_dir=str(tmp_path),
        style="default",
        group_words=False,
    )
    assert caps["ok"] is True
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(320, 180, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 1.2)
    out = tmp_path / "captioned.mp4"
    started = tools.render_start(str(out), async_render=False)
    assert started["ok"] is True, started
    # Standalone burn-in primitive (same ffmpeg path, no NLE).
    burned = tmp_path / "burned.mp4"
    from rushcut.analysis.captions import burn_captions

    result = burn_captions(str(out), caps["ass_path"], str(burned))
    assert result["ok"] is True
    assert Path(burned).is_file()
    assert probe_media(str(burned))["ok"] is True


def test_backend_async_render_status(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 0.8)
    out = tmp_path / "async.mp4"
    started = tools.render_start(str(out), async_render=True)
    assert started["ok"] is True
    assert started.get("job_id")
    done = tools.wait_for_render(started["job_id"], timeout_seconds=60, poll_seconds=0.1)
    assert done.get("state") in {"complete", "completed", "done", "success", "finished"}
    assert Path(out).is_file()


def test_removed_recipe_stubs():
    from rushcut import apply_vertical_short, propose_edit

    assert apply_vertical_short("x", 0, 1)["removed"] is True
    assert propose_edit("x")["removed"] is True
