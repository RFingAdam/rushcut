"""Atomic, verified render outputs (#35)."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from rushcut import tools
from rushcut.analysis.edit_ops import cut_media
from rushcut.analysis.ffmpeg_util import which_ffmpeg
from rushcut.analysis.probe import probe_media
from rushcut.backends import reset_backend
from rushcut.backends.ffmpeg import FfmpegBackend
from rushcut.render.atomic import has_moov_atom, partial_path, qa_path
from rushcut.render.sidecar import verify_output


def _ffmpeg(monkeypatch) -> FfmpegBackend:
    monkeypatch.setenv("DIRECTOR_EDITOR", "ffmpeg")
    backend = FfmpegBackend()
    reset_backend(backend)
    return backend


def _timeline(color_clip):
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(320, 180, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 2.0)


def test_verify_output_fields_on_good_file(color_clip):
    result = verify_output(str(color_clip), expect_audio=True)
    assert result["ok"] is True
    assert result["moov_ok"] is True
    assert result["decode_errors"] == 0
    assert isinstance(result["md5"], str) and len(result["md5"]) == 32
    assert has_moov_atom(str(color_clip))


def test_verify_output_fails_without_moov(tmp_path, color_clip):
    broken = tmp_path / "truncated.mp4"
    data = color_clip.read_bytes()
    # Chop off the end so the moov (faststart or trailer) is gone.
    broken.write_bytes(data[: max(32, len(data) // 8)])
    result = verify_output(str(broken), expect_audio=False)
    assert result["ok"] is False
    assert result["moov_ok"] is False or result["decode_errors"] > 0
    assert "moov_ok" in result
    assert "decode_errors" in result
    assert "md5" in result


def test_verify_output_decode_errors_fail_ok(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a media file at all")
    result = verify_output(str(junk), expect_audio=False)
    assert result["ok"] is False
    assert result["moov_ok"] is False or result["decode_errors"] > 0


def test_cut_media_writes_qa_sidecar(color_clip, tmp_path):
    dest = tmp_path / "cut.mp4"
    result = cut_media(str(color_clip), 0.2, 1.0, str(dest))
    assert result["ok"] is True
    assert dest.is_file()
    assert not Path(partial_path(str(dest))).is_file()
    sidecar = Path(qa_path(str(dest)))
    assert sidecar.is_file()
    text = sidecar.read_text(encoding="utf-8")
    assert "decode_errors" in text
    assert "md5" in text
    assert result["verify"]["moov_ok"] is True
    assert result["verify"]["decode_errors"] == 0


def test_failed_render_does_not_overwrite_good_file(color_clip, tmp_path):
    dest = tmp_path / "keep.mp4"
    dest.write_bytes(color_clip.read_bytes())
    original = dest.read_bytes()
    partial = Path(partial_path(str(dest)))
    partial.write_bytes(b"truncated-no-moov")
    from rushcut.render.atomic import promote_partial

    result = promote_partial(str(partial), str(dest), expect_audio=False)
    assert result["ok"] is False
    assert dest.read_bytes() == original
    assert dest.is_file()
    assert partial.is_file()


def test_ffmpeg_render_promotes_and_writes_qa(monkeypatch, color_clip, tmp_path):
    _ffmpeg(monkeypatch)
    _timeline(color_clip)
    dest = tmp_path / "show.mp4"
    started = tools.render_start(str(dest), async_render=False)
    assert started["ok"] is True, started
    assert dest.is_file()
    assert not Path(partial_path(str(dest))).is_file()
    assert Path(qa_path(str(dest))).is_file()
    probe = probe_media(str(dest))
    assert probe["ok"] is True


def test_kill_ffmpeg_leaves_only_partial(monkeypatch, color_clip, tmp_path):
    real = which_ffmpeg("ffmpeg")
    wrapper = tmp_path / "ffmpeg_wrap.py"
    wrapper.write_text(
        "import os, sys, time\n"
        "args = sys.argv[1:]\n"
        "out = args[-1] if args else ''\n"
        "if out.endswith('.partial.mp4'):\n"
        "    open(out, 'wb').write(b'\\x00' * 2048)\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        f"os.execv({real!r}, [{real!r}, *args])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FFMPEG_BIN", sys.executable)
    # which_ffmpeg checks FFMPEG_BIN is a file; a python script works if we
    # wrap it. Point FFMPEG_BIN at a shell launcher instead.
    launcher = tmp_path / "ffmpeg"
    launcher.write_text(
        f"#!/bin/sh\nexec {sys.executable} {wrapper} \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(launcher))

    _ffmpeg(monkeypatch)
    _timeline(color_clip)
    dest = tmp_path / "killed.mp4"
    started = tools.render_start(str(dest), async_render=True)
    assert started["ok"] is True, started
    job_id = started["job_id"]
    partial = Path(partial_path(str(dest)))
    pid = None
    deadline = time.time() + 15
    while time.time() < deadline:
        status = tools.render_status(job_id)
        pid = status.get("pid")
        if partial.is_file() and pid:
            break
        time.sleep(0.05)
    assert partial.is_file(), "wrapper should have created the .partial file"
    assert pid, status
    os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + 10
    last = {}
    while time.time() < deadline:
        last = tools.render_status(job_id)
        if str(last.get("state") or "").lower() == "interrupted":
            break
        time.sleep(0.05)
    assert str(last.get("state") or "").lower() == "interrupted", last
    assert last.get("ok") is False
    assert not dest.exists()
    leftovers = list(tmp_path.glob("*.mp4"))
    assert leftovers == [partial] or (partial in leftovers and dest not in leftovers)
    assert dest.name not in {p.name for p in leftovers}


def test_vegas_wait_for_render_promotes(fake_session, color_clip, tmp_path):
    dest = tmp_path / "vegas.mp4"

    def handler(method, params):
        if method == "render.start":
            Path(params["output_path"]).write_bytes(color_clip.read_bytes())
            return {"ok": True, "job_id": "v1", "state": "running"}
        if method == "render.status":
            return {"ok": True, "job_id": "v1", "state": "complete", "percent": 100}
        raise AssertionError(method)

    fake_session(handler)
    started = tools.render_start(str(dest), async_render=True)
    assert started["partial_path"].endswith(".partial.mp4")
    done = tools.wait_for_render("v1", timeout_seconds=30, poll_seconds=0.05)
    assert done["ok"] is True, done
    assert dest.is_file()
    assert not Path(partial_path(str(dest))).is_file()
    assert Path(qa_path(str(dest))).is_file()
    assert done["verify"]["moov_ok"] is True
    assert done["verify"]["decode_errors"] == 0


def test_nle_wait_for_render_runs_qa_for_resolve_and_vegas(fake_session, color_clip, tmp_path):
    """VEGAS and Resolve share the tools-layer finalize after wait_for_render."""
    dest = tmp_path / "nle.mp4"

    def handler(method, params):
        if method == "render.start":
            Path(params["output_path"]).write_bytes(color_clip.read_bytes())
            return {"ok": True, "job_id": "nle1", "state": "running", "editor": "resolve"}
        if method == "render.status":
            return {
                "ok": True,
                "job_id": "nle1",
                "state": "complete",
                "percent": 100,
                "editor": "resolve",
            }
        raise AssertionError(method)

    fake_session(handler)
    started = tools.render_start(str(dest), async_render=True)
    assert started["partial_path"].endswith(".partial.mp4")
    done = tools.wait_for_render(started["job_id"], timeout_seconds=30, poll_seconds=0.05)
    assert done["ok"] is True, done
    assert dest.is_file()
    assert Path(qa_path(str(dest))).is_file()
    assert done["verify"]["md5"]
    assert done["verify"]["moov_ok"] is True
