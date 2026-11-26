"""pick_thumbnail_frames: ranking, blur and overlay rejection, stable output."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from rushcut.analysis.thumbnails import pick_thumbnail_frames  # noqa: E402

# Blur covers 2.0-4.0 s; overlay covers 0.9-2.6 s. Samples land on k / 2 s.
_BLUR_START, _BLUR_END = 2.0, 4.0
_OVERLAY_START, _OVERLAY_END = 0.9, 2.6
_OVERLAY_BOX = [220, 130, 100, 50]


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


@pytest.fixture
def blur_clip(media_dir: Path, ffmpeg_bin: str) -> Path:
    """Busy test pattern, heavily blurred from 2 s to 4 s."""
    out = media_dir / "blur.mp4"
    _ffmpeg(
        [
            ffmpeg_bin, "-y", "-f", "lavfi",
            "-i", f"testsrc2=d=6:s=320x180:r=10,boxblur=8:enable='between(t,{_BLUR_START},{_BLUR_END})'",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out),
        ]
    )
    return out


@pytest.fixture
def overlay_clip(media_dir: Path, ffmpeg_bin: str) -> Path:
    """Moving pattern on a flat background; a white 'notification' box pops up bottom right."""
    out = media_dir / "overlay.mp4"
    x, y, w, h = _OVERLAY_BOX
    _ffmpeg(
        [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "color=c=0x404850:s=320x180:r=10:d=6",
            "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=6",
            "-filter_complex",
            f"[0:v][1:v]overlay=80:30,drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill"
            f":enable='between(t,{_OVERLAY_START},{_OVERLAY_END})'",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out),
        ]
    )
    return out


def test_skips_blurred_frames_and_writes_ranked_pngs(blur_clip: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "thumbs"
    result = pick_thumbnail_frames(str(blur_clip), n=4, min_gap_seconds=0.5, output_dir=str(out_dir))
    assert result["ok"], result
    frames = result["frames"]
    assert len(frames) == 4
    assert result["rejected"]["blurred"] >= 4
    for f in frames:
        assert not (_BLUR_START <= f["t"] < _BLUR_END), f
        assert Path(f["png"]).is_file()
        assert Path(f["png"]).parent == out_dir.resolve()
    assert [f["rank"] for f in frames] == [1, 2, 3, 4]
    scores = [f["score"] for f in frames]
    assert scores == sorted(scores, reverse=True)
    assert set(frames[0]["signals"]) == {"sharpness", "contrast", "subject", "text_area", "face"}


def test_skips_overlay_regions(overlay_clip: Path) -> None:
    result = pick_thumbnail_frames(
        str(overlay_clip), n=6, min_gap_seconds=0.5, skip_regions=[_OVERLAY_BOX], write_png=False
    )
    assert result["ok"], result
    assert result["rejected"]["overlay"] >= 3
    assert result["frames"]
    for f in result["frames"]:
        assert not (_OVERLAY_START <= f["t"] <= _OVERLAY_END), f
        assert "png" not in f


def test_without_skip_regions_overlay_frames_are_eligible(overlay_clip: Path) -> None:
    result = pick_thumbnail_frames(str(overlay_clip), n=12, min_gap_seconds=0.0, write_png=False)
    assert result["ok"], result
    assert result["rejected"]["overlay"] == 0


@pytest.mark.parametrize("clip", ["blur_clip", "overlay_clip"])
def test_output_is_stable_across_runs(clip: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    path = str(request.getfixturevalue(clip))
    kwargs = {"n": 4, "min_gap_seconds": 1.0, "skip_regions": [_OVERLAY_BOX] if clip == "overlay_clip" else None}
    first = pick_thumbnail_frames(path, output_dir=str(tmp_path / "a"), **kwargs)
    second = pick_thumbnail_frames(path, output_dir=str(tmp_path / "b"), **kwargs)
    assert first["ok"] and second["ok"]
    strip = [{k: v for k, v in f.items() if k != "png"} for f in first["frames"]]
    assert strip == [{k: v for k, v in f.items() if k != "png"} for f in second["frames"]]
    for a, b in zip(first["frames"], second["frames"]):
        assert Path(a["png"]).read_bytes() == Path(b["png"]).read_bytes()


def test_min_gap_keeps_picks_apart(blur_clip: Path) -> None:
    result = pick_thumbnail_frames(str(blur_clip), n=6, min_gap_seconds=1.5, write_png=False)
    assert result["ok"], result
    times = sorted(f["t"] for f in result["frames"])
    assert all(b - a >= 1.5 for a, b in zip(times, times[1:]))


def test_bad_inputs(tmp_path: Path, blur_clip: Path) -> None:
    assert not pick_thumbnail_frames(str(tmp_path / "missing.mp4"))["ok"]
    assert not pick_thumbnail_frames(str(blur_clip), n=0)["ok"]
    assert not pick_thumbnail_frames(str(blur_clip), sample_fps=0)["ok"]
    assert not pick_thumbnail_frames(str(blur_clip), start_seconds=5, end_seconds=4)["ok"]
    bad_box = pick_thumbnail_frames(str(blur_clip), skip_regions=["nope"], write_png=False)
    assert not bad_box["ok"] and "skip_regions" in bad_box["error"]


def test_output_dir_outside_roots_is_refused(monkeypatch, blur_clip: Path, tmp_path: Path) -> None:
    from rushcut.output_policy import OUTSIDE_ROOTS, reset_policy_state

    monkeypatch.setenv("RUSHCUT_OUTPUT_ROOTS", str(tmp_path / "allowed"))
    reset_policy_state()
    try:
        outside = tmp_path / "elsewhere"
        result = pick_thumbnail_frames(str(blur_clip), n=2, output_dir=str(outside))
        assert not result["ok"] and result["error"] == OUTSIDE_ROOTS, result
        assert not outside.exists()
        # The default <src>_thumbs/ next to the source is checked too.
        default = pick_thumbnail_frames(str(blur_clip), n=2)
        assert not default["ok"] and default["error"] == OUTSIDE_ROOTS, default
        assert not (blur_clip.parent / f"{blur_clip.stem}_thumbs").exists()
        # write_png=False writes nothing, so it is not refused.
        assert pick_thumbnail_frames(str(blur_clip), n=2, write_png=False)["ok"]
        inside = tmp_path / "allowed" / "thumbs"
        ok = pick_thumbnail_frames(str(blur_clip), n=2, min_gap_seconds=0.5, output_dir=str(inside))
        assert ok["ok"], ok
        assert all(Path(f["png"]).parent == inside.resolve() for f in ok["frames"])
    finally:
        monkeypatch.delenv("RUSHCUT_OUTPUT_ROOTS")
        reset_policy_state()


def _png_level(ffmpeg_bin: str, png: str, x0: int, x1: int, w: int, h: int) -> float:
    """Mean red level of a flat grey patch; equals the full-range grey the scorer sees."""
    import numpy as np

    raw = subprocess.run(
        [ffmpeg_bin, "-v", "error", "-i", png, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout
    img = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
    return float(img[8 : h - 8, x0:x1, 0].mean())


@pytest.mark.parametrize("start", [0.0, 0.3])
def test_png_is_the_scored_frame_at_2997(
    monkeypatch, media_dir: Path, ffmpeg_bin: str, tmp_path: Path, start: float
) -> None:
    """At 29.97 fps no sample lands on a frame; the PNG must still be the frame that was scored."""
    import rushcut.analysis.thumbnails as thumbs

    clip = media_dir / "ntsc.mp4"
    # Left quarter is a flat grey patch whose level encodes the frame number (16 + 3n).
    _ffmpeg(
        [
            ffmpeg_bin, "-y", "-f", "lavfi", "-i", "testsrc2=d=2:s=320x180:r=30000/1001",
            "-vf", "format=yuv420p,geq=lum='if(lt(X,W/4),16+3*N,lum(X,Y))':cb='if(lt(X,W/4),128,cb(X,Y))':cr='if(lt(X,W/4),128,cr(X,Y))'",
            "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", str(clip),
        ]
    )
    scored: dict[float, float] = {}
    real_iter = thumbs._iter_frames

    def recording_iter(*args, **kwargs):
        for t, frame in real_iter(*args, **kwargs):
            scored[round(t, 3)] = float(frame[8:-8, 4:72].mean())
            yield t, frame

    monkeypatch.setattr(thumbs, "_iter_frames", recording_iter)
    result = pick_thumbnail_frames(
        str(clip), n=3, min_gap_seconds=0.0, start_seconds=start, output_dir=str(tmp_path / "png")
    )
    assert result["ok"], result
    assert result["frames"]
    for f in result["frames"]:
        # Samples sit on the first frame at or after each grid point, and t is that frame's own time.
        assert f["t"] >= start
        scored_level = scored[f["t"]]
        png_level = _png_level(ffmpeg_bin, f["png"], 4, 72, 320, 180)
        # Neighbouring frames differ by ~3.5 levels.
        assert abs(png_level - scored_level) < 1.5, (f, scored_level, png_level)


def test_face_cascade_loads_once(monkeypatch, tmp_path: Path) -> None:
    import sys
    import types

    import numpy as np

    import rushcut.analysis.thumbnails as thumbs

    (tmp_path / "haarcascade_frontalface_default.xml").write_text("<x/>")
    loads: list[str] = []

    class FakeClassifier:
        def __init__(self, path: str) -> None:
            loads.append(path)

        def empty(self) -> bool:
            return False

        def detectMultiScale(self, frame, **kwargs):
            return [(0, 0, 10, 10)]

    fake_cv2 = types.SimpleNamespace(
        data=types.SimpleNamespace(haarcascades=str(tmp_path)), CascadeClassifier=FakeClassifier
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    thumbs._face_classifier.cache_clear()
    try:
        frame = np.zeros((90, 160), dtype=np.uint8)
        for _ in range(5):
            score, status = thumbs._face_score(frame, 160, 90)
            assert status == "haar" and score > 0
        assert len(loads) == 1
    finally:
        thumbs._face_classifier.cache_clear()
