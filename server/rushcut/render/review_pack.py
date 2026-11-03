"""export_review_pack: proxies, contact sheets, QA, optional chunked copies."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.loudness import measure_loudness
from ..analysis.probe import probe_media
from ..output_roots import refuse_output_path
from .sidecar import file_md5, verify_output

PROXY_PRESETS = {
    "720p": {"height": 720, "video_bitrate": "1M"},
    "1080p": {"height": 1080, "video_bitrate": "2M"},
    "480p": {"height": 480, "video_bitrate": "600k"},
}

_REASSEMBLE_PY = '''#!/usr/bin/env python3
"""Reassemble chunked review-pack files from manifest.json."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def reassemble(manifest_path: Path, dest_dir: Path | None = None) -> list[Path]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    dest_dir = dest_dir or base / "reassembled"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in data.get("files") or []:
        name = item["name"]
        out = dest_dir / name
        with out.open("wb") as dest:
            for chunk in item.get("chunks") or []:
                dest.write((base / chunk["name"]).read_bytes())
        got = file_md5(out)
        expect = item.get("md5")
        if expect and got != expect:
            raise SystemExit(f"MD5 mismatch for {name}: {got} != {expect}")
        written.append(out)
    return written


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    manifest = here / "manifest.json"
    if len(sys.argv) > 1:
        manifest = Path(sys.argv[1])
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    for path in reassemble(manifest, dest):
        print(path)
'''


def _stem(path: str) -> str:
    return Path(path).stem


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _proxy_filter(height: int) -> str:
    return f"scale=-2:{int(height)}"


def make_proxy(src: str, dest: str, preset: str = "720p") -> dict[str, Any]:
    spec = PROXY_PRESETS.get(str(preset).lower()) or PROXY_PRESETS["720p"]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        src,
        "-vf",
        _proxy_filter(int(spec["height"])),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        spec["video_bitrate"],
        "-maxrate",
        spec["video_bitrate"],
        "-bufsize",
        "2M",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]
    probe = probe_media(src)
    if probe.get("has_audio"):
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd.append(dest)
    run(cmd, timeout=300)
    return {"ok": True, "path": os.path.abspath(dest), "preset": spec}


def make_contact_sheet(src: str, dest: str, *, columns: int = 4, rows: int = 4) -> dict[str, Any]:
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    tiles = max(1, columns * rows)
    probe = probe_media(src)
    duration = float(probe.get("duration_seconds") or 2.0)
    interval = max(duration / tiles, 0.04)
    vf = f"fps=1/{interval:.4f},scale=160:-2,tile={columns}x{rows}"
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                src,
                "-vf",
                vf,
                "-frames:v",
                "1",
                dest,
            ],
            timeout=120,
        )
    except FfmpegError:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                src,
                "-vf",
                f"select=eq(n\\,0),scale=160:-2",
                "-frames:v",
                "1",
                dest,
            ],
            timeout=60,
        )
    return {"ok": True, "path": os.path.abspath(dest)}


def split_chunks(src: str, dest_dir: str, chunk_mb: float) -> dict[str, Any]:
    size = os.path.getsize(src)
    chunk_bytes = max(1, int(float(chunk_mb) * 1024 * 1024))
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    stem = Path(src).name
    chunks: list[dict[str, Any]] = []
    index = 0
    with open(src, "rb") as fh:
        while True:
            data = fh.read(chunk_bytes)
            if not data:
                break
            name = f"{stem}.part{index:03d}"
            dest = Path(dest_dir) / name
            dest.write_bytes(data)
            chunks.append(
                {
                    "name": name,
                    "path": str(dest),
                    "md5": file_md5(str(dest)),
                    "bytes": len(data),
                    "index": index,
                }
            )
            index += 1
    return {
        "ok": True,
        "source": os.path.abspath(src),
        "source_md5": file_md5(src),
        "source_bytes": size,
        "chunk_bytes": chunk_bytes,
        "chunks": chunks,
    }


def reassemble_chunks(chunks: list[dict[str, Any]], dest: str, *, chunk_dir: str | None = None) -> str:
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        for chunk in chunks:
            raw_path = chunk.get("path") or (
                str(Path(chunk_dir) / chunk["name"]) if chunk_dir else chunk["name"]
            )
            with open(raw_path, "rb") as fh:
                shutil.copyfileobj(fh, out)
    return os.path.abspath(dest)


def _qa_row(src: str, verify: dict[str, Any]) -> dict[str, Any]:
    loud = measure_loudness(src)
    return {
        "source": os.path.abspath(src),
        "name": Path(src).name,
        "duration_seconds": verify.get("duration_seconds"),
        "integrated_lufs": loud.get("integrated_lufs"),
        "true_peak_dbtp": loud.get("true_peak_dbtp"),
        "md5": verify.get("md5"),
        "decode_errors": verify.get("decode_errors"),
        "moov_ok": verify.get("moov_ok"),
        "verify_ok": verify.get("ok"),
    }


def _qa_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Review pack QA",
        "",
        "| file | duration_s | LUFS | TP | MD5 | decode_errors |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for row in rows:
        lines.append(
            "| {name} | {duration} | {lufs} | {tp} | `{md5}` | {err} |".format(
                name=row.get("name") or "",
                duration="" if row.get("duration_seconds") is None else f"{row['duration_seconds']:.3f}",
                lufs="" if row.get("integrated_lufs") is None else f"{row['integrated_lufs']:.3f}",
                tp="" if row.get("true_peak_dbtp") is None else f"{row['true_peak_dbtp']:.3f}",
                md5=row.get("md5") or "",
                err=row.get("decode_errors") if row.get("decode_errors") is not None else "",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _load_state(dest: Path) -> dict[str, Any]:
    state_path = dest / "pack_state.json"
    if not state_path.is_file():
        return {"files": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(data, dict):
        return {"files": {}}
    data.setdefault("files", {})
    return data


def export_review_pack(
    outputs: list[str] | str,
    dest: str,
    *,
    proxy: str = "720p",
    chunk_mb: float | None = None,
    profile: Any = None,
) -> dict[str, Any]:
    """Write 720p proxies, contact sheets, and QA next to optional chunks.

    Finals are only read. ``dest`` must fall under configured output roots.
    Re-running skips files whose source MD5 is unchanged.
    """
    if isinstance(outputs, (str, os.PathLike)):
        outputs = [str(outputs)]
    paths = [str(p) for p in outputs if str(p).strip()]
    if not paths:
        return {"ok": False, "error": "outputs is required"}
    if not dest:
        return {"ok": False, "error": "dest is required"}
    refused = refuse_output_path(dest, profile=profile)
    if refused:
        return refused
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(dest_dir)
    known = dict(state.get("files") or {})
    rows: list[dict[str, Any]] = []
    written: list[dict[str, Any]] = []
    skipped: list[str] = []
    chunk_manifest: list[dict[str, Any]] = []
    chunk_root = dest_dir / "chunks"

    for src in paths:
        if not os.path.isfile(src):
            return {"ok": False, "error": f"file not found: {src}"}
        source_md5 = file_md5(src)
        verify = verify_output(src, expect_audio=False)
        row = _qa_row(src, verify)
        rows.append(row)
        stem = _stem(src)
        proxy_path = dest_dir / f"{stem}_720p.mp4" if str(proxy).lower() == "720p" else dest_dir / f"{stem}_{proxy}.mp4"
        sheet_path = dest_dir / f"{stem}_sheet.jpg"
        prev = known.get(os.path.abspath(src)) or known.get(Path(src).name) or {}
        unchanged = (
            prev.get("md5") == source_md5
            and proxy_path.is_file()
            and sheet_path.is_file()
        )
        entry = {
            "source": os.path.abspath(src),
            "md5": source_md5,
            "proxy": str(proxy_path),
            "sheet": str(sheet_path),
            "skipped": unchanged,
        }
        if unchanged:
            skipped.append(os.path.abspath(src))
        else:
            probe = probe_media(src)
            if probe.get("ok") and probe.get("has_video"):
                make_proxy(src, str(proxy_path), preset=proxy)
                make_contact_sheet(src, str(sheet_path))
            else:
                entry["note"] = probe.get("error") or "no video stream; proxy/sheet skipped"
        if chunk_mb:
            chunk_dir = chunk_root / stem
            need_chunks = True
            if unchanged and prev.get("chunk_md5") == source_md5 and (chunk_dir / f"{Path(src).name}.part000").is_file():
                need_chunks = False
            if need_chunks:
                split = split_chunks(src, str(chunk_dir), float(chunk_mb))
            else:
                split = {
                    "ok": True,
                    "source_md5": source_md5,
                    "chunks": [
                        {
                            "name": p.name,
                            "path": str(p),
                            "md5": file_md5(str(p)),
                            "bytes": p.stat().st_size,
                        }
                        for p in sorted(chunk_dir.glob(f"{Path(src).name}.part*"))
                    ],
                }
            chunk_manifest.append(
                {
                    "name": Path(src).name,
                    "source": os.path.abspath(src),
                    "md5": source_md5,
                    "chunks": [
                        {
                            "name": f"{stem}/{c['name']}" if "/" not in str(c["name"]) else c["name"],
                            "md5": c["md5"],
                            "bytes": c.get("bytes"),
                        }
                        for c in split.get("chunks") or []
                    ],
                }
            )
            # Keep chunk names relative to dest/chunks.
            for item in chunk_manifest[-1]["chunks"]:
                if not str(item["name"]).startswith(stem + "/") and "/" not in str(item["name"]):
                    item["name"] = f"{stem}/{item['name']}"
            entry["chunk_md5"] = source_md5
        written.append(entry)
        known[os.path.abspath(src)] = {
            "md5": source_md5,
            "proxy": str(proxy_path),
            "sheet": str(sheet_path),
            "chunk_md5": source_md5 if chunk_mb else prev.get("chunk_md5"),
        }

    qa = {
        "ok": True,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "files": rows,
    }
    _write_json(dest_dir / "QA.json", qa)
    (dest_dir / "QA.md").write_text(_qa_markdown(rows), encoding="utf-8")
    if chunk_mb:
        manifest = {
            "ok": True,
            "chunk_mb": float(chunk_mb),
            "files": chunk_manifest,
        }
        _write_json(chunk_root / "manifest.json", manifest)
        (chunk_root / "reassemble.py").write_text(_REASSEMBLE_PY, encoding="utf-8")
    state = {
        "ok": True,
        "dest": os.path.abspath(dest),
        "files": known,
    }
    _write_json(dest_dir / "pack_state.json", state)
    return {
        "ok": True,
        "dest": os.path.abspath(dest),
        "files": written,
        "skipped": skipped,
        "qa_json": str(dest_dir / "QA.json"),
        "qa_md": str(dest_dir / "QA.md"),
        "qa": qa,
        "chunk_mb": float(chunk_mb) if chunk_mb else None,
        "chunk_manifest": str(chunk_root / "manifest.json") if chunk_mb else None,
    }


def reassemble_review_pack(dest: str, output_dir: str | None = None) -> dict[str, Any]:
    """Rebuild originals from a pack's chunk manifest and verify MD5."""
    dest_dir = Path(dest)
    manifest_path = dest_dir / "chunks" / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": f"manifest not found: {manifest_path}"}
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_dir = Path(output_dir) if output_dir else dest_dir / "reassembled"
    out_dir.mkdir(parents=True, exist_ok=True)
    rebuilt: list[dict[str, Any]] = []
    for item in data.get("files") or []:
        dest_file = out_dir / item["name"]
        chunks = []
        for chunk in item.get("chunks") or []:
            chunks.append({"path": str((dest_dir / "chunks") / chunk["name"]), "name": chunk["name"]})
        reassemble_chunks(chunks, str(dest_file))
        got = file_md5(str(dest_file))
        rebuilt.append(
            {
                "path": str(dest_file),
                "md5": got,
                "expected_md5": item.get("md5"),
                "match": got == item.get("md5"),
            }
        )
        if got != item.get("md5"):
            return {
                "ok": False,
                "error": f"reassembled MD5 mismatch for {item['name']}",
                "files": rebuilt,
            }
    return {"ok": True, "dest": str(out_dir), "files": rebuilt}
