"""Unfinalized MKV detect / repair (#29)."""
from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from rushcut.analysis.probe import file_still_writing, probe_media
from rushcut.analysis.session import repair_media
from rushcut.cli import main as cli_main
from rushcut.quality import run_checks
from rushcut.render.sidecar import verify_output


def _ffmpeg(ffmpeg_bin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])
    return proc


def write_unfinalized_mkv(ffmpeg_bin: str, dest: Path, duration: float = 3.0) -> Path:
    """Write a matroska stream to a pipe so the muxer cannot finalize duration/cues."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=160x90:rate=24",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "matroska",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[-800:])
    data = proc.stdout
    # Drop a tail so cues / duration written at close never land, if they did.
    if len(data) > 8000:
        data = data[: int(len(data) * 0.82)]
    dest.write_bytes(data)
    return dest


def _frame_png(ffmpeg_bin: str, src: Path, seconds: float, dest: Path) -> Path:
    _ffmpeg(
        ffmpeg_bin,
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            str(dest),
        ],
    )
    return dest


def _png_luma(ffmpeg_bin: str, path: Path) -> np.ndarray:
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return np.frombuffer(path.read_bytes(), dtype=np.uint8).astype(np.float32)
    return np.frombuffer(proc.stdout, dtype=np.uint8).astype(np.float32)


def test_probe_truncated_mkv_flags_and_estimates_duration(tmp_path, ffmpeg_bin):
    dest = write_unfinalized_mkv(ffmpeg_bin, tmp_path / "live.mkv", duration=3.0)
    probe = probe_media(str(dest))
    assert probe["ok"] is True
    assert probe["header_duration_missing"] is True
    assert probe["unfinalized_container"] is True
    assert probe["cues_missing"] is True
    assert probe["duration_estimated"] is True
    assert abs(float(probe["duration_seconds"]) - 3.0) <= 1.0
    assert "unfinalized_container" in (probe.get("warnings") or [])


def test_repair_media_finalizes_and_leaves_source(tmp_path, ffmpeg_bin):
    src = write_unfinalized_mkv(ffmpeg_bin, tmp_path / "cut.mkv", duration=2.5)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "fixed.mkv"
    repaired = repair_media(str(src), str(out))
    assert repaired["ok"] is True
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    probe = probe_media(str(out))
    assert probe["ok"] is True
    assert probe["header_duration_missing"] is False
    assert probe["unfinalized_container"] is False
    duration = float(probe["duration_seconds"])
    src_len = float(probe_media(str(src))["duration_seconds"] or 0)
    assert abs(duration - src_len) <= (1.0 / 24.0) + 0.08
    span = min(duration, src_len)
    times = [round(span * f, 3) for f in (0.1, 0.3, 0.5, 0.7, 0.85)]
    for i, t in enumerate(times):
        src_png = tmp_path / f"src-{i}.png"
        out_png = tmp_path / f"out-{i}.png"
        # Decode the unrepaired stream from the start (no index), seek the repaired file.
        _ffmpeg(
            ffmpeg_bin,
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-ss",
                f"{t:.3f}",
                "-frames:v",
                "1",
                str(src_png),
            ],
        )
        _frame_png(ffmpeg_bin, out, t, out_png)
        a = _png_luma(ffmpeg_bin, src_png)
        b = _png_luma(ffmpeg_bin, out_png)
        n = min(a.size, b.size)
        mad = float(np.mean(np.abs(a.ravel()[:n] - b.ravel()[:n])))
        assert mad < 20.0
    assert cli_main(["repair-media", str(src), "--output", str(tmp_path / "cli.mkv")]) == 0


def test_file_still_writing_while_appended(tmp_path):
    dest = tmp_path / "grow.bin"
    stop = threading.Event()

    def append() -> None:
        with dest.open("ab") as fh:
            while not stop.is_set():
                fh.write(b"0123456789" * 200)
                fh.flush()
                time.sleep(0.05)

    dest.write_bytes(b"start")
    thread = threading.Thread(target=append)
    thread.start()
    try:
        time.sleep(0.12)
        growing = file_still_writing(str(dest), window_s=0.4, poll_s=0.2)
        assert growing["ok"] is True
        assert growing["still_writing"] is True
    finally:
        stop.set()
        thread.join(timeout=2)
    time.sleep(0.5)
    settled = file_still_writing(str(dest), window_s=0.25, poll_s=0.1)
    assert settled["still_writing"] is False


def test_verify_output_and_quality_gate_fail_unfinalized(tmp_path, ffmpeg_bin):
    dest = write_unfinalized_mkv(ffmpeg_bin, tmp_path / "bad.mkv", duration=2.0)
    verified = verify_output(str(dest), expect_audio=True)
    assert verified["ok"] is False
    detail = " ".join(verified.get("problems") or [])
    assert "unfinalized" in detail
    report = run_checks(target=str(dest), mode="strict")
    assert report["ok"] is False
    ids = {c["id"] for c in report["checks"]}
    assert "verify_output" in ids
