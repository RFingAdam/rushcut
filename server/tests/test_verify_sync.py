"""verify_sync and tape_audit on synthetic media (FFmpeg only)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

from rushcut.cli import main as cli_main  # noqa: E402
from rushcut.quality.sync import tape_audit, verify_sync  # noqa: E402


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("sync")
    out = d / "source.mkv"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=24",
        "-f", "lavfi", "-i", "anoisesrc=d=24:c=pink:r=48000:seed=7:a=0.5,"
        "volume='0.2+0.8*abs(sin(2*PI*0.7*t))':eval=frame",
        "-c:v", "libx264", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-preset", "veryfast",
        "-c:a", "aac", "-shortest", str(out))
    return out


def test_stream_copy_is_in_sync(source: Path, tmp_path: Path) -> None:
    cut = _copy_cut(source, tmp_path)
    r = verify_sync(str(cut), str(source))
    assert r["picture"]["exact"], r
    # Remuxing may shift both streams together; what matters is that they agree.
    assert abs(r["picture"]["lag_ms"] - r["audio"]["lag_ms"]) < 2.0
    assert abs(r["av_offset_ms"]) < 2.0


def test_delayed_audio_is_measured(source: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.mkv"
    _ff("-i", str(source), "-c:v", "copy", "-af", "adelay=120:all=1", "-c:a", "aac", str(bad))
    r = verify_sync(str(bad), str(source))
    assert r["ok"] is False
    base = verify_sync(str(_copy_cut(source, tmp_path)), str(source))
    delta = r["audio"]["lag_ms"] - base["audio"]["lag_ms"]
    assert delta == pytest.approx(120.0, abs=1.0)
    assert r["av_offset_ms"] < -90
    assert any("A/V offset" in f for f in r["findings"])


def _copy_cut(source: Path, tmp_path: Path) -> Path:
    """Whole-file stream copy: decoded frames are identical to the source."""
    p = tmp_path / "ref_copy.mkv"
    _ff("-i", str(source), "-c", "copy", str(p))
    return p


def test_wrong_source_start_is_reported(source: Path, tmp_path: Path) -> None:
    cut = _copy_cut(source, tmp_path)
    r = verify_sync(str(cut), str(source), source_start=9.0, check_video=False)
    assert r["ok"] is False
    assert r["audio"]["lag_ms"] is None or abs(r["audio"]["lag_ms"]) > 100 or r["findings"]


def test_container_only_without_source(source: Path) -> None:
    r = verify_sync(str(source))
    assert r["ok"] and "container checks only" in r["note"]


def test_cut_is_found_at_its_real_start(source: Path, tmp_path: Path) -> None:
    # Cut with an accurate (re-encoded) video+audio seek so the start is exactly 6 s.
    cut = tmp_path / "cut.mkv"
    _ff("-ss", "6", "-t", "10", "-i", str(source), "-c:v", "libx264", "-g", "30", "-preset", "veryfast",
        "-c:a", "aac", str(cut))
    r = verify_sync(str(cut), str(source), source_start=6.0, points=[1.0, 5.0])
    assert abs(r["audio"]["lag_ms"]) < 25
    assert r["picture"]["points"][0]["method"] in ("exact_frame", "near_frame", "motion_xcorr")


def test_missing_file() -> None:
    assert verify_sync("/nonexistent.mkv")["ok"] is False


def test_tape_audit_flags_short_audio_and_gaps(source: Path, tmp_path: Path) -> None:
    folder = tmp_path / "tapes"
    folder.mkdir()
    shutil.copy(source, folder / "good.mkv")
    # audio ends 3 s early
    _ff("-i", str(source), "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-af", "atrim=0:21", "-c:a", "aac",
        str(folder / "short_audio.mkv"))
    # 0.5 s hole in video timestamps after frame 300
    _ff("-i", str(source), "-vf", "setpts='if(gte(N,300),PTS+0.5/TB,PTS)'", "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "veryfast", "-an", str(folder / "gappy.mkv"))
    r = tape_audit(str(folder), tail_seconds=30)
    by = {x["file"]: x for x in r["results"]}
    assert by["good.mkv"]["flags"] == []
    assert any("ends" in f for f in by["short_audio.mkv"]["flags"])
    assert by["gappy.mkv"]["video_gaps"] == 1
    assert r["ok"] is False and r["flagged"] == 2


def test_tape_audit_bad_folder() -> None:
    assert tape_audit("/nonexistent-folder")["ok"] is False


def test_cli(source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["verify-sync", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli_main(["tape-audit", str(source.parent)]) in (0, 2)
