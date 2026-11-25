"""MP4 delivery presets and the full-timeline sidecar round trip (#15)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rushcut import tools
from rushcut.analysis.probe import probe_media
from rushcut.backends import reset_backend
from rushcut.backends.ffmpeg import FfmpegBackend
from rushcut.edit import apply_timeline
from rushcut.edit.schema import load_edit
from rushcut.render.presets import (
    DELIVERY_PRESETS,
    RENDER_PRESETS,
    delivery_ffmpeg_args,
    match_preset,
    preset_expectations,
)
from rushcut.render.sidecar import moov_before_mdat, verify_output, write_sidecar


def _ffmpeg_backend(monkeypatch) -> FfmpegBackend:
    monkeypatch.setenv("DIRECTOR_EDITOR", "ffmpeg")
    backend = FfmpegBackend()
    reset_backend(backend)
    return backend


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def test_delivery_preset_exact_args():
    assert delivery_ffmpeg_args("vertical_30") == [
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
    ]
    sizes = {name: (spec["width"], spec["height"], spec["fps"]) for name, spec in DELIVERY_PRESETS.items()}
    assert sizes == {
        "vertical_30": (1080, 1920, 30),
        "hd_30": (1920, 1080, 30),
        "hd_60": (1920, 1080, 60),
        "uhd": (3840, 2160, 30),
    }
    assert "fps=60" in delivery_ffmpeg_args("hd_60")[1]
    assert "scale=3840:2160" in delivery_ffmpeg_args("uhd")[1]
    assert delivery_ffmpeg_args("hd_30", video_filter=False)[0] == "-c:v"
    with pytest.raises(KeyError):
        delivery_ffmpeg_args("vertical")
    # The size-only presets are unchanged.
    for name, size in {"vertical": (1080, 1920), "1080p": (1920, 1080), "4k": (3840, 2160)}.items():
        spec = RENDER_PRESETS[name]
        assert (spec["width"], spec["height"]) == size
        assert not spec.get("delivery")
        assert preset_expectations(name) == {"width": size[0], "height": size[1]}
    assert preset_expectations("hd_60") == {
        "width": 1920,
        "height": 1080,
        "fps": 60.0,
        "video_codec": "h264",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "sample_rate": 48000,
        "faststart": True,
    }


def test_match_preset_delivery_names_on_vegas_templates():
    templates = [
        {"renderer": "MAGIX AVC/AAC MP4", "template": "Internet HD 1080p 29.97 fps", "width": 1920, "height": 1080},
        {"renderer": "MAGIX AVC/AAC MP4", "template": "Internet HD 1080p 59.94 fps", "width": 1920, "height": 1080},
        {"renderer": "MAGIX AVC/AAC MP4", "template": "Vertical 1080x1920", "width": 1080, "height": 1920},
        {"renderer": "MAGIX AVC/AAC MP4", "template": "Internet 4K 2160p", "width": 3840, "height": 2160},
    ]
    assert match_preset("vertical_30", templates)["template_name"] == "Vertical 1080x1920"
    assert match_preset("hd_60", templates)["template_name"] == "Internet HD 1080p 59.94 fps"
    assert match_preset("hd_30", templates)["template_name"] == "Internet HD 1080p 29.97 fps"
    assert match_preset("uhd", templates)["template_name"] == "Internet 4K 2160p"
    assert match_preset("vertical", templates)["template_name"] == "Vertical 1080x1920"
    bad = match_preset("nope", templates)
    assert bad["ok"] is False and "vertical_30" in bad["error"]


def test_verify_output_preset_checks(tmp_path, ffmpeg_bin, color_clip):
    good = tmp_path / "good.mp4"
    _run(
        [
            ffmpeg_bin, "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=0.5:size=320x180:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
            *delivery_ffmpeg_args("vertical_30"),
            str(good),
        ]
    )
    ok = verify_output(str(good), preset="vertical_30")
    assert ok["ok"] is True, ok["problems"]
    checks = ok["preset_checks"]
    assert set(checks) == {
        "width", "height", "fps", "video_codec", "pix_fmt", "audio_codec", "sample_rate", "faststart",
    }
    assert all(c["ok"] for c in checks.values()), checks
    assert checks["sample_rate"]["actual"] == 48000
    assert checks["fps"]["actual"] == 30.0

    wrong = verify_output(str(good), preset="hd_60", decode=False)
    assert wrong["ok"] is False
    assert wrong["preset_checks"]["fps"] == {"ok": False, "expected": 60.0, "actual": 30.0}
    assert wrong["preset_checks"]["width"]["ok"] is False
    assert wrong["preset_checks"]["video_codec"]["ok"] is True
    assert any(p.startswith("preset fps") for p in wrong["problems"])

    # The fixture clip: 320x180, 24 fps, written without +faststart.
    plain = verify_output(str(color_clip), preset="hd_30", decode=False)
    assert plain["ok"] is False
    assert plain["preset_checks"]["faststart"]["ok"] is (moov_before_mdat(str(color_clip)) is True)
    assert plain["preset_checks"]["fps"]["actual"] == 24.0

    unknown = verify_output(str(good), preset="nope")
    assert unknown["ok"] is False and "vertical_30" in unknown["error"]

    # Size-only presets keep the old width/height-only behavior.
    size_only = verify_output(str(good), preset="vertical", decode=False)
    assert size_only["ok"] is True
    assert set(size_only["preset_checks"]) == {"width", "height"}


def test_render_with_preset_ffmpeg_delivery_and_sidecar_roundtrip(monkeypatch, tmp_path, color_clip):
    _ffmpeg_backend(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 0.8)
    timeline = {
        "schema": "rushcut.edit/v1",
        "output": {"preset": "vertical_30"},
        "join_fade_ms": 0,
        "segments": [{"source": str(color_clip), "in": 0.0, "out": 0.8}],
        "notes": {"take": 3},
    }
    out = tmp_path / "delivery.mp4"
    result = tools.render_with_preset(str(out), "vertical_30", timeline=timeline, wait=True, timeout_seconds=180)
    assert result["ok"] is True, result
    verify = result["verify"]
    assert verify["ok"] is True, verify["problems"]
    assert all(c["ok"] for c in verify["preset_checks"].values()), verify["preset_checks"]
    probe = probe_media(str(out))
    assert (probe["width"], probe["height"]) == (1080, 1920)
    assert abs(float(probe["fps"]) - 30.0) < 0.01

    sidecar_path = Path(result["sidecar"]["sidecar_path"])
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["sidecar_version"] == 2
    assert payload["timeline"]["notes"] == {"take": 3}
    assert payload["timeline"]["output"]["preset"] == "vertical_30"
    assert payload["extra"]["preset"] == "vertical_30"

    again = tmp_path / "again.mp4"
    rerender = apply_timeline(str(sidecar_path), output=str(again))
    assert rerender["ok"] is True, rerender
    assert all(c["ok"] for c in rerender["preset_checks"].values()), rerender["preset_checks"]
    reprobe = probe_media(str(again))
    assert (reprobe["width"], reprobe["height"]) == (probe["width"], probe["height"])
    assert abs(reprobe["duration_seconds"] - probe["duration_seconds"]) <= (1.0 / 30.0) + 0.05


def test_render_with_preset_master_keeps_delivery_audio(monkeypatch, tmp_path, color_clip):
    _ffmpeg_backend(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 1.0)
    out = tmp_path / "mastered.mp4"
    result = tools.render_with_preset(str(out), "hd_30", master=True, wait=True, timeout_seconds=180)
    assert result["master"]["ok"] is True, result["master"]
    assert result["verify"]["ok"] is True, result["verify"]["problems"]
    checks = result["verify"]["preset_checks"]
    assert checks["sample_rate"]["actual"] == 48000
    assert checks["faststart"]["ok"] is True
    # master_audio keeps 48 kHz, so only a faststart remux is needed.
    assert result["master"]["delivery_conform"] == {"ok": True, "changed": True, "audio_reencoded": False}
    assert result["ok"] is True


def test_sidecar_full_timeline_roundtrip_through_apply_timeline(tmp_path, color_clip):
    edit = {
        "schema": "rushcut.edit/v1",
        "output": {"width": 320, "height": 180, "fps": 24},
        "join_fade_ms": 8,
        "segments": [
            {"source": str(color_clip), "in": 0.2, "out": 0.9, "label": "hook"},
            {
                "source": str(color_clip),
                "in": 1.0,
                "out": 1.6,
                "effects": [{"type": "zoom", "scale": 1.3, "center": [0.5, 0.5], "t_in": 0.0, "t_out": 0.4}],
            },
        ],
        "notes": {"source_take": "B"},
    }
    first = tmp_path / "first.mp4"
    rendered = apply_timeline(edit, output=str(first))
    assert rendered["ok"] is True, rendered

    # write_edit_sidecar on the render, with the edit document as given.
    written = write_sidecar(str(first), {"length": rendered["duration_seconds"], "template": "timeline"}, timeline=edit)
    payload = json.loads(Path(written["sidecar_path"]).read_text(encoding="utf-8"))
    assert payload["sidecar_version"] == 2
    timeline = payload["timeline"]
    assert timeline["notes"] == {"source_take": "B"}
    assert timeline["segments"][0]["label"] == "hook"
    assert timeline["segments"][1]["effects"][0]["type"] == "zoom"
    # v1 keys are still there for older readers.
    assert payload["segments"] and payload["output"] and payload["edit"]["template"] == "timeline"
    assert payload["join_fade_ms"] == 8

    loaded = load_edit(written["sidecar_path"])
    assert loaded["notes"] == {"source_take": "B"}
    assert "extra" not in loaded and "written_at" not in loaded

    second = tmp_path / "second.mp4"
    again = apply_timeline(written["sidecar_path"], output=str(second))
    assert again["ok"] is True, again
    a, b = probe_media(str(first)), probe_media(str(second))
    assert (a["width"], a["height"]) == (b["width"], b["height"]) == (320, 180)
    assert abs(a["duration_seconds"] - b["duration_seconds"]) <= (1.0 / 24.0) + 0.04

    # The sidecar apply_timeline wrote for the re-render holds the same
    # document, not a sidecar nested inside a sidecar.
    nested = json.loads(Path(again["sidecar_path"]).read_text(encoding="utf-8"))
    assert nested["timeline"]["notes"] == {"source_take": "B"}
    assert not {"timeline", "extra", "edit", "written_at", "sidecar_version"} & set(nested["timeline"])

    # The bare timeline object renders too.
    third = tmp_path / "third.mp4"
    direct = apply_timeline(edit_json=timeline, output=str(third))
    assert direct["ok"] is True, direct
    assert abs(direct["duration_seconds"] - rendered["duration_seconds"]) <= (1.0 / 24.0) + 0.04


def test_v1_sidecar_still_loads(tmp_path, color_clip):
    sidecar = tmp_path / "old.mp4.edit.json"
    sidecar.write_text(
        json.dumps(
            {
                "ok": True,
                "output_path": str(tmp_path / "old.mp4"),
                "written_at": "2026-01-01T00:00:00+00:00",
                "edit": {"length": 0.5, "template": "timeline"},
                "schema": "rushcut.edit/v1",
                "segments": [{"source": str(color_clip), "in": 0, "out": 0.5}],
                "output": {"width": 320, "height": 180, "fps": 24},
                "join_fade_ms": 8,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_edit(str(sidecar))
    assert loaded["segments"][0]["out"] == 0.5
    assert loaded["output"]["width"] == 320


def test_apply_timeline_delivery_output_preset(tmp_path, color_clip):
    edit = {
        "output": {"preset": "hd_60"},
        "join_fade_ms": 0,
        "segments": [{"source": str(color_clip), "in": 0.0, "out": 0.4}],
    }
    dest = tmp_path / "hd60.mp4"
    result = apply_timeline(edit, output=str(dest))
    assert result["ok"] is True, result
    assert result["preset"] == "hd_60"
    checks = result["preset_checks"]
    assert all(c["ok"] for c in checks.values()), checks
    assert checks["fps"]["actual"] == 60.0


def test_ffmpeg_backend_delivery_burns_captions(monkeypatch, tmp_path, color_clip):
    from rushcut.analysis.captions import generate_captions

    caps = generate_captions(
        path=str(color_clip),
        segments=[{"start_seconds": 0.0, "end_seconds": 0.6, "text": "hello"}],
        output_dir=str(tmp_path),
        style="default",
        group_words=False,
    )
    assert caps["ok"] is True
    backend = _ffmpeg_backend(monkeypatch)
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(color_clip), 0.0, 0.6)
    out = tmp_path / "captioned.mp4"
    result = backend.call(
        "render.start",
        {"output_path": str(out), "async": False, "delivery_preset": "hd_30", "ass_path": caps["ass_path"]},
    )
    assert result["ok"] is True, result
    assert result["delivery_preset"] == "hd_30"
    checked = verify_output(str(out), preset="hd_30", decode=False)
    assert checked["ok"] is True, checked["problems"]
    bad = backend.call("render.start", {"output_path": str(tmp_path / "x.mp4"), "async": False, "delivery_preset": "nope"})
    assert bad["ok"] is False and "vertical_30" in bad["error"]
