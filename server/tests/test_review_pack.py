"""export_review_pack: proxies, QA, chunks, output_roots, idempotence (#40)."""
from __future__ import annotations

import os
from pathlib import Path

from rushcut.cli import main as cli_main
from rushcut.output_policy import OUTSIDE_ROOTS, check_output_path, is_under_root
from rushcut.render.review_pack import export_review_pack, reassemble_review_pack
from rushcut.render.sidecar import file_md5, verify_output


def _small_clip(ffmpeg_bin: str, dest: Path, color: str = "red") -> Path:
    import subprocess

    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:r=24:d=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])
    return dest


def test_export_review_pack_proxy_sheet_qa_matches_verify(tmp_path, ffmpeg_bin):
    a = _small_clip(ffmpeg_bin, tmp_path / "a.mp4", "red")
    b = _small_clip(ffmpeg_bin, tmp_path / "b.mp4", "blue")
    dest = tmp_path / "review"
    result = export_review_pack([str(a), str(b)], str(dest), proxy="720p")
    assert result["ok"] is True
    assert (dest / "a_720p.mp4").is_file()
    assert (dest / "b_720p.mp4").is_file()
    assert (dest / "a_sheet.jpg").is_file()
    assert (dest / "b_sheet.jpg").is_file()
    qa_md = (dest / "QA.md").read_text(encoding="utf-8")
    assert "a.mp4" in qa_md and "b.mp4" in qa_md
    assert qa_md.count("|") >= 8
    qa = result["qa"]["files"]
    assert len(qa) == 2
    for src, row in ((a, qa[0]), (b, qa[1])):
        verified = verify_output(str(src), expect_audio=True)
        assert row["md5"] == verified["md5"]
        assert row["decode_errors"] == verified["decode_errors"]
        assert abs(float(row["duration_seconds"]) - float(verified["duration_seconds"])) <= 0.05
        assert row["integrated_lufs"] is not None
        assert row["true_peak_dbtp"] is not None
    assert cli_main(["export-review-pack", str(a), "--dest", str(tmp_path / "cli-pack")]) == 0


def test_chunk_mode_reassembled_md5_matches_50mb_fixture(tmp_path):
    blob = tmp_path / "fixture.bin"
    blob.write_bytes(os.urandom(50 * 1024 * 1024))
    original_md5 = file_md5(str(blob))
    dest = tmp_path / "pack"
    result = export_review_pack([str(blob)], str(dest), chunk_mb=7)
    assert result["ok"] is True
    rebuilt = reassemble_review_pack(str(dest), str(tmp_path / "out"))
    assert rebuilt["ok"] is True
    assert rebuilt["files"][0]["match"] is True
    assert rebuilt["files"][0]["md5"] == original_md5
    assert file_md5(rebuilt["files"][0]["path"]) == original_md5


def test_export_review_pack_refuses_c_drive_when_not_allowed(tmp_path, ffmpeg_bin):
    clip = _small_clip(ffmpeg_bin, tmp_path / "s.mp4")
    profile = {"output_roots": [r"D:\MediaOut"]}
    refused = export_review_pack([str(clip)], r"C:\review\pack", profile=profile)
    assert refused["ok"] is False
    assert refused["error"] == OUTSIDE_ROOTS
    assert not Path(r"C:\review\pack").exists()
    assert check_output_path(r"C:\out\s.mp4", profile=profile)["error"] == OUTSIDE_ROOTS
    assert is_under_root(r"D:\MediaOut\out\s.mp4", r"D:\MediaOut") is True
    assert is_under_root(r"D:\MediaOut\..\Windows\s.mp4", r"D:\MediaOut") is False
    allowed = tmp_path / "allowed"
    ok = export_review_pack(
        [str(clip)],
        str(allowed / "pack"),
        profile={"output_roots": [str(allowed)]},
    )
    assert ok["ok"] is True
    none = check_output_path(str(tmp_path / "anywhere"), profile={})
    assert none is None


def test_export_review_pack_idempotent_skips_unchanged_md5(tmp_path, ffmpeg_bin):
    clip = _small_clip(ffmpeg_bin, tmp_path / "keep.mp4")
    dest = tmp_path / "review"
    first = export_review_pack([str(clip)], str(dest))
    assert first["ok"] is True
    assert first["skipped"] == []
    proxy = dest / "keep_720p.mp4"
    sheet = dest / "keep_sheet.jpg"
    proxy_mtime = proxy.stat().st_mtime_ns
    sheet_mtime = sheet.stat().st_mtime_ns
    proxy_bytes = proxy.read_bytes()
    second = export_review_pack([str(clip)], str(dest))
    assert second["ok"] is True
    assert any(Path(p).name == "keep.mp4" or p.endswith("keep.mp4") for p in second["skipped"])
    assert proxy.read_bytes() == proxy_bytes
    assert proxy.stat().st_mtime_ns == proxy_mtime
    assert sheet.stat().st_mtime_ns == sheet_mtime
