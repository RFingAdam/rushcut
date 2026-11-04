"""Declarative timeline, effects, overlays, and post-render checks (#4–#9, #16)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rushcut.analysis.pcm import decode_pcm
from rushcut.analysis.probe import probe_media
from rushcut.edit import apply_timeline
from rushcut.edit.apply import apply_edit, blur_media, expected_duration
from rushcut.edit.curves import collect_punches, interpolate_rect, zoom_at
from rushcut.edit.filters import speed_output_length
from rushcut.edit.frames import compose_thumbnail, frame_check, pick_frame
from rushcut.edit.lens import apply_lens_zoom, track_circle
from rushcut.edit.mediaio import extract_frame, mean_luma
from rushcut.edit.schema import EditError, load_edit, schema_document, validate_edit
from rushcut.edit.text import bundled_font_path, make_text_card
from rushcut.edit.vegas_map import apply_edit_host, zoom_keyframes
from rushcut.render.sidecar import verify_output, write_sidecar


def _ff(ffmpeg_bin: str, args: list[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _clip(ffmpeg_bin: str, dest: Path, *, duration: float = 2.0, w: int = 320, h: int = 180, fps: int = 30, color: str = "blue") -> Path:
    _ff(
        ffmpeg_bin,
        [
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(dest),
        ],
    )
    return dest


def test_schema_file_and_named_field_errors():
    doc = schema_document()
    assert doc["required"] == ["segments"]
    with pytest.raises(EditError, match="segments") as exc:
        validate_edit({"output": {"preset": "1080p"}})
    assert exc.value.field == "segments"
    with pytest.raises(EditError, match="segments\\[0\\].out") as exc:
        validate_edit({"segments": [{"source": "a.mp4", "in": 2, "out": 1}]})
    assert exc.value.field == "segments[0].out"
    with pytest.raises(EditError, match="effects\\[0\\].type") as exc:
        validate_edit({"segments": [{"source": "a.mp4", "in": 0, "out": 1, "effects": [{"type": "nope"}]}]})
    assert exc.value.field == "effects[0].type" or "type" in exc.value.field


def test_apply_timeline_duration_jl_cut_and_sidecar(tmp_path, ffmpeg_bin, color_clip):
    other = _clip(ffmpeg_bin, tmp_path / "tone.mp4", duration=1.6, color="red")
    edit = {
        "schema": "rushcut.edit/v1",
        "output": {"width": 320, "height": 180, "fps": 24},
        "join_fade_ms": 8,
        "segments": [
            {"source": str(color_clip), "in": 0.0, "out": 0.8},
            {
                "source": str(color_clip),
                "in": 0.8,
                "out": 1.6,
                "audio_source": str(other),
                "audio_offset": 0.2,
            },
        ],
    }
    dest = tmp_path / "cut.mp4"
    result = apply_timeline(edit, output=str(dest))
    assert result["ok"] is True, result
    expected = expected_duration(load_edit(edit))
    probe = probe_media(str(dest))
    assert abs(probe["duration_seconds"] - expected) <= (1.0 / 24.0) + 0.04
    assert probe["has_audio"] is True
    sidecar = Path(result["sidecar_path"])
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["segments"]
    dest2 = tmp_path / "again.mp4"
    again = apply_timeline(str(sidecar), output=str(dest2))
    assert again["ok"] is True, again
    assert abs(again["duration_seconds"] - result["duration_seconds"]) <= (1.0 / 24.0) + 0.04


def test_fps_conform_av_offset(tmp_path, ffmpeg_bin):
    src = tmp_path / "flash60.mp4"
    # 60 fps black + 1-frame white flash and beep at 0.5 s.
    _ff(
        ffmpeg_bin,
        [
            "-f", "lavfi",
            "-i", "color=c=black:s=160x90:r=60:d=1.2,drawbox=x=0:y=0:w=iw:h=ih:c=white:t=fill:enable='between(t,0.5,0.5167)'",
            "-f", "lavfi",
            "-i", "sine=frequency=2000:sample_rate=48000:duration=1.2,volume=enable='between(t,0.5,0.5167)':volume=0.0001",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(src),
        ],
    )
    dest = tmp_path / "out30.mp4"
    result = apply_edit(
        {
            "segments": [{"source": str(src), "in": 0.0, "out": 1.2}],
            "output": {"width": 160, "height": 90, "fps": 30},
            "join_fade_ms": 0,
        },
        str(dest),
    )
    assert result["ok"] is True, result
    # Brightest video frame vs loudest audio window.
    best_t, best_luma = 0.0, -1.0
    t = 0.0
    while t < 1.15:
        grabbed = extract_frame(str(dest), t)
        if grabbed.get("ok"):
            luma = mean_luma(grabbed["rgb"], grabbed["width"], grabbed["height"])
            if luma > best_luma:
                best_luma, best_t = luma, t
        t += 1.0 / 30.0
    samples, sr, ch = decode_pcm(str(dest), sample_rate=48000, channels=2)
    window = int(0.02 * sr) * ch
    peak_t, peak = 0.0, -1
    for i in range(0, max(1, len(samples) - window), window):
        energy = sum(abs(int(s)) for s in samples[i : i + window])
        if energy > peak:
            peak = energy
            peak_t = (i / ch) / sr
    assert abs(best_t - peak_t) < (1.0 / 30.0) + 0.01


def test_zoom_merged_curve_has_no_dip():
    punches = collect_punches(
        {
            "punches": [
                {"scale": 1.6, "t_in": 0.0, "t_out": 1.2, "ease": 0.2, "center": [0.4, 0.5]},
                {"scale": 1.8, "t_in": 0.8, "t_out": 2.0, "ease": 0.2, "center": [0.6, 0.5]},
            ]
        }
    )
    mid = zoom_at(1.0, punches)[0]
    assert mid >= 1.55
    # Would dip to 1.0 if envelopes were multiplied independently around the join.
    assert zoom_at(1.0, punches)[0] > 1.2


def test_speed_ramp_10s_at_4x(tmp_path, ffmpeg_bin):
    src = _clip(ffmpeg_bin, tmp_path / "ten.mp4", duration=10.0, w=160, h=90, fps=24)
    dest = tmp_path / "fast.mp4"
    result = apply_edit(
        {
            "segments": [
                {
                    "source": str(src),
                    "in": 0.0,
                    "out": 10.0,
                    "effects": [
                        {
                            "type": "speed_ramp",
                            "steps": [{"start": 0, "end": 10, "factor": 4}],
                            "audio": "atempo",
                            "tmix_above": 2,
                        }
                    ],
                }
            ],
            "output": {"width": 160, "height": 90, "fps": 24},
            "join_fade_ms": 0,
        },
        str(dest),
    )
    assert result["ok"] is True, result
    probe = probe_media(str(dest))
    assert abs(probe["duration_seconds"] - 2.5) <= (1.0 / 24.0) + 0.05
    assert abs(speed_output_length(10.0, [{"start": 0, "end": 10, "factor": 4}]) - 2.5) < 1e-6


def test_vegas_mapping_places_trimmed_events(fake_session, color_clip):
    calls = []

    def handler(method, params):
        calls.append((method, params))
        if method == "project.new":
            return {"ok": True}
        if method == "project.set_video":
            return {"ok": True}
        if method == "track.add":
            return {"ok": True, "track_index": 0 if params.get("type") == "video" else 1}
        if method == "event.add_video":
            return {"ok": True, "event_index": 0, "track_index": 0}
        if method == "event.trim":
            return {"ok": True}
        if method == "event.set_fades":
            return {"ok": True}
        if method == "event.set_motion":
            return {"ok": True}
        return {"ok": True}

    fake_session(handler)
    edit = validate_edit(
        {
            "segments": [{"source": str(color_clip), "in": 0.4, "out": 1.4, "effects": [{"type": "zoom", "scale": 1.4, "t_in": 0.1, "t_out": 0.8, "ease": 0.1}]}],
            "output": {"width": 320, "height": 180, "fps": 24},
            "join_fade_ms": 8,
        }
    )
    result = apply_edit_host(edit, "/tmp/unused.mp4", backend="vegas")
    assert result["ok"] is True, result
    methods = [m for m, _ in calls]
    assert "event.add_video" in methods
    assert "event.trim" in methods
    assert "event.set_motion" in methods
    trim = next(p for m, p in calls if m == "event.trim")
    assert abs(trim["take_offset_seconds"] - 0.4) < 1e-6
    keys = zoom_keyframes(edit["segments"][0]["effects"][0], 1.0)
    assert keys[0]["scale"] >= 1.0


def test_track_circle_jitter_and_lens_edge(tmp_path, ffmpeg_bin):
    src = tmp_path / "ring.mp4"
    # Moving dark ring over a mid-gray field (80x80 keeps the Python remap cheap).
    _ff(
        ffmpeg_bin,
        [
            "-f", "lavfi",
            "-i", "color=c=0x808080:s=80x80:r=15:d=0.8",
            "-vf", "geq=r='if(lt(abs(hypot(X-40-4*T,Y-40)-18),2.2),10,128)':g='if(lt(abs(hypot(X-40-4*T,Y-40)-18),2.2),10,128)':b='if(lt(abs(hypot(X-40-4*T,Y-40)-18),2.2),10,128)'",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-an",
            str(src),
        ],
    )
    tracked = track_circle(str(src), start=0.0, duration=0.8, r_min=10, r_max=30, min_inliers=8)
    assert tracked["ok"] is True, tracked
    xs = [f["cx"] for f in tracked["frames"] if f.get("cx") is not None]
    # Residual after subtracting linear motion: std-dev of (cx - 40 - 4*t).
    residuals = []
    for frame in tracked["frames"]:
        if frame.get("cx") is None:
            continue
        expected = 40 + 4 * frame["t"]
        residuals.append(frame["cx"] - expected)
    if residuals:
        mean = sum(residuals) / len(residuals)
        var = sum((r - mean) ** 2 for r in residuals) / len(residuals)
        assert var ** 0.5 < 1.5 or tracked["confidence"] > 0.3
    assert xs, tracked

    # Dark-band test: static ring over a horizontal gradient.
    grad = tmp_path / "grad.mp4"
    _ff(
        ffmpeg_bin,
        [
            "-f", "lavfi",
            "-i", "color=c=black:s=80x80:r=12:d=0.4",
            "-vf", "geq=r='X*3':g='X*3':b='X*3',drawbox=x=28:y=28:w=24:h=24:c=black@1:t=3",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-an",
            str(grad),
        ],
    )
    # Synthetic track at the box centre.
    track = {
        "frames": [
            {"t": i / 12.0, "cx": 40.0, "cy": 40.0, "r": 16.0, "inliers": 20, "ok": True}
            for i in range(6)
        ]
    }
    dest = tmp_path / "lens.mp4"
    lensed = apply_lens_zoom(str(grad), str(dest), track=track, scale=1.6, inset_ratio=0.2, feather_px=2, ease=0.0, unsharp=False)
    assert lensed["ok"] is True, lensed
    grabbed = extract_frame(str(dest), 0.1)
    assert grabbed["ok"]
    # Sample just inside the ring (r=16, inset 0.2 → inner ~12.8). Compare to the
    # un-ringed gradient at the same output pixel (should not be near-black).
    w, rgb = grabbed["width"], grabbed["rgb"]
    x, y = 40, 40 - 11
    i = (y * w + x) * 3
    luma = (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) / 1000
    assert luma > 20, luma


def test_crop_path_and_layouts(tmp_path, ffmpeg_bin):
    keys = [
        {"t": 0.0, "x": 0, "y": 0, "w": 100, "h": 80},
        {"t": 2.0, "x": 40, "y": 20, "w": 100, "h": 80},
    ]
    r0 = interpolate_rect(keys, 0.0)
    r1 = interpolate_rect(keys, 1.0)
    r2 = interpolate_rect(keys, 2.0)
    assert r0 == (0.0, 0.0, 100.0, 80.0)
    assert abs(r1[0] - 20.0) < 1e-6
    assert r2[1] == 20.0

    src = _clip(ffmpeg_bin, tmp_path / "wide.mp4", duration=0.4, w=1920, h=1080, fps=12, color="green")
    for layout, dest_name in (("blur_fill", "bf.mp4"), ("center", "c.mp4"), ("square", "sq.mp4"), ("split", "sp.mp4")):
        dest = tmp_path / dest_name
        effect = {
            "type": "compose_layout",
            "layout": layout,
            "main": [200, 100, 1520, 880],
            "inset": {"rect": [20, 20, 400, 300], "x": 40, "y": 1400, "width": 360, "height": 200, "border": 6, "border_color": "white"},
            "drop_empty_inset": True,
        }
        result = apply_edit(
            {
                "segments": [{"source": str(src), "in": 0.0, "out": 0.35, "effects": [effect]}],
                "output": {"width": 1080, "height": 1920, "fps": 12},
                "join_fade_ms": 0,
            },
            str(dest),
        )
        assert result["ok"] is True, (layout, result)
        probe = probe_media(str(dest))
        assert probe["width"] == 1080
        assert probe["height"] == 1920
        frame = extract_frame(str(dest), 0.05)
        assert frame["ok"]
        # Main crop is a bright-green field in the centre third.
        luma = mean_luma(frame["rgb"], frame["width"], frame["height"], (200, 700, 680, 400))
        assert luma > 30, (layout, luma)


def test_blur_regions_outside_identical(tmp_path, ffmpeg_bin):
    src = tmp_path / "src.mkv"
    _ff(
        ffmpeg_bin,
        [
            "-f", "lavfi", "-i", "testsrc=duration=0.4:size=160x90:rate=12",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=0.4",
            "-pix_fmt", "gbrp", "-c:v", "ffv1", "-c:a", "pcm_s16le",
            str(src),
        ],
    )
    dest = tmp_path / "blur.mkv"
    result = blur_media(
        str(src),
        [{"rect": [20, 10, 40, 30], "start": 0, "end": 1, "mode": "blur", "strength": 8, "feather": 0}],
        str(dest),
        lossless=True,
    )
    assert result["ok"] is True, result
    a = extract_frame(str(src), 0.0)
    b = extract_frame(str(dest), 0.0)
    assert a["ok"] and b["ok"]
    aw, ah, ar = a["width"], a["height"], a["rgb"]
    br = b["rgb"]
    # Inside the rect must change; outside must match.
    changed = False
    for y in range(ah):
        for x in range(aw):
            i = (y * aw + x) * 3
            same = ar[i : i + 3] == br[i : i + 3]
            inside = 20 <= x < 60 and 10 <= y < 40
            if inside:
                if not same:
                    changed = True
            else:
                assert same, (x, y)
    assert changed


def test_text_card_and_overlay(tmp_path, ffmpeg_bin, color_clip):
    assert Path(bundled_font_path()).is_file()
    card = tmp_path / "card.png"
    result = make_text_card(
        ["HELLO", "WORLD"],
        str(card),
        style={"size": 64, "colors": ["#FFFFFF", "#FFD60A"], "stroke": 5, "box_color": "#000000CC", "max_width": 900},
        width=1080,
        height=400,
    )
    assert result["ok"] is True, result
    from PIL import Image

    img = Image.open(card).convert("RGBA")
    # Sample the rounded box padding, not the glyph itself.
    px = img.getpixel((img.size[0] // 2, 24))
    dark = next(
        (img.getpixel((x, y)) for y in range(20, img.size[1] - 20, 8)
         for x in range(40, img.size[0] - 40, 40)
         if img.getpixel((x, y))[3] > 80 and img.getpixel((x, y))[0] < 80),
        None,
    )
    assert dark is not None, px
    dest = tmp_path / "ov.mp4"
    overlayed = apply_edit(
        {
            "segments": [
                {
                    "source": str(color_clip),
                    "in": 0.0,
                    "out": 0.8,
                    "effects": [
                        {"type": "overlay", "path": str(card), "anchor": "safe_title", "start": 0, "end": 0.8, "fade": 0.05}
                    ],
                }
            ],
            "output": {"width": 320, "height": 180, "fps": 24},
            "join_fade_ms": 0,
        },
        str(dest),
    )
    assert overlayed["ok"] is True, overlayed


def test_pick_frame_thumbnail_and_frame_check(tmp_path, ffmpeg_bin):
    src = tmp_path / "mix.mp4"
    # Blurred 0.4 s then a sharp testsrc 0.4 s.
    blur = tmp_path / "b.mp4"
    sharp = tmp_path / "s.mp4"
    _ff(ffmpeg_bin, ["-f", "lavfi", "-i", "testsrc=duration=0.5:size=160x90:rate=12", "-vf", "boxblur=8:2", "-c:v", "libx264", "-preset", "ultrafast", "-an", str(blur)])
    _ff(ffmpeg_bin, ["-f", "lavfi", "-i", "testsrc=duration=0.5:size=160x90:rate=12", "-c:v", "libx264", "-preset", "ultrafast", "-an", str(sharp)])
    _ff(ffmpeg_bin, ["-i", str(blur), "-i", str(sharp), "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(src)])
    picked = pick_frame(str(src), 0.0, 0.95, step=0.08)
    assert picked["ok"] is True, picked
    assert picked["t"] >= 0.4

    f1 = tmp_path / "f1.png"
    f2 = tmp_path / "f2.png"
    assert extract_frame(str(sharp), 0.1, dest=str(f1))["ok"]
    assert extract_frame(str(blur), 0.1, dest=str(f2))["ok"]
    thumb = tmp_path / "thumb.png"
    composed = compose_thumbnail([str(f1), str(f2)], str(thumb), layout="split", texts=["A", "B"], size="1280x720", arrows=True)
    assert composed["ok"] is True
    from PIL import Image

    im = Image.open(thumb)
    assert im.size == (1280, 720)
    vert = tmp_path / "vthumb.png"
    assert compose_thumbnail(str(f1), str(vert), layout="single", texts=["TITLE"], size="1080x1920")["ok"]
    assert Image.open(vert).size == (1080, 1920)

    sheet_dir = tmp_path / "review"
    checked = frame_check(str(src), times=[{"t": 0.2, "label": "cold open"}, {"t": 0.7, "label": "payoff"}], output_dir=str(sheet_dir))
    assert checked["ok"] is True, checked
    assert Path(checked["sheet"]).is_file()
    assert Path(checked["json_path"]).is_file()
    labels = {f["label"] for f in checked["frames"]}
    assert "cold open" in labels and "payoff" in labels and "first" in labels and "last" in labels


def test_verify_output_new_fields_and_duration_fail(color_clip, tmp_path):
    ok = verify_output(
        str(color_clip),
        expect_audio=True,
        expect_duration=2.2,
        duration_tolerance=0.5,
        measure_loudness=True,
        measure_head_black=True,
    )
    assert ok["ok"] is True, ok
    assert ok["fps_exact"]
    assert ok["frame_count"]
    assert ok["av_offset_ms"] is not None
    assert "lufs" in ok
    assert "true_peak" in ok
    assert ok["head_black_s"] is not None
    bad = verify_output(str(color_clip), expect_duration=9.0, duration_tolerance=0.2, measure_loudness=False, measure_head_black=False)
    assert bad["ok"] is False
    assert any("duration" in p for p in bad["problems"])


def test_write_sidecar_roundtrip_shape(tmp_path, color_clip):
    dest = tmp_path / "out.mp4"
    dest.write_bytes(color_clip.read_bytes())
    timeline = {
        "schema": "rushcut.edit/v1",
        "segments": [{"source": str(color_clip), "in": 0, "out": 1}],
        "output": {"width": 320, "height": 180, "fps": 24},
        "join_fade_ms": 8,
    }
    payload = write_sidecar(str(dest), {"length": 1.0, "template": "timeline"}, timeline=timeline)
    loaded = load_edit(payload["sidecar_path"])
    assert loaded["segments"][0]["out"] == 1.0


def test_legacy_audio_effect_still_works(tmp_path, ffmpeg_bin):
    src = tmp_path / "a.wav"
    _ff(ffmpeg_bin, ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.6", "-ac", "2", "-ar", "48000", str(src)])
    result = apply_timeline(
        "bleep",
        path=str(src),
        output_path=str(tmp_path / "b.wav"),
        spans=[{"start": 0.1, "end": 0.2, "word": "x"}],
        mode="mute",
    )
    assert result["ok"] is True, result
    assert result["effect"] == "bleep"
