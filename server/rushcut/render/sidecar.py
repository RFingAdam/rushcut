"""Verify a render with ffprobe and write the edit-metadata sidecar JSON."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media

SIDECAR_KEYS = (
    "length",
    "hook_start_time",
    "caption_style",
    "zoom_count",
    "music",
    "template",
)
_ERROR_RE = re.compile(r"\b(error|corrupt|invalid data)\b", re.IGNORECASE)


def sidecar_path(output_path: str) -> str:
    return output_path + ".edit.json"


def file_md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _moov_ok(path: str, probe: dict[str, Any]) -> bool:
    if probe.get("unfinalized_container"):
        return False
    name = str(probe.get("format_name") or "").lower()
    if "mp4" in name or "mov" in name or "ismv" in name:
        try:
            with open(path, "rb") as fh:
                head = fh.read(2 * 1024 * 1024)
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 2 * 1024 * 1024))
                tail = fh.read()
        except OSError:
            return False
        return b"moov" in head or b"moov" in tail
    return not probe.get("header_duration_missing")


def count_decode_errors(path: str) -> int:
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-v",
                "error",
                "-i",
                path,
                "-f",
                "null",
                "-",
            ],
            timeout=180,
            check=False,
        )
    except FfmpegError:
        return 1
    text = proc.stderr or proc.stdout or ""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    hits = _ERROR_RE.findall(text)
    if proc.returncode not in (0, None) and not hits:
        return 1
    return len(hits)


def verify_output(
    path: str,
    *,
    expect_width: int | None = None,
    expect_height: int | None = None,
    expect_audio: bool = True,
    duration_tolerance: float = 0.6,
    expect_duration: float | None = None,
) -> dict[str, Any]:
    probe = probe_media(path)
    if not probe.get("ok"):
        return {
            **probe,
            "moov_ok": False,
            "decode_errors": 1,
            "md5": None,
        }
    problems: list[str] = []
    if expect_audio and not probe.get("has_audio"):
        problems.append("no audio stream")
    if expect_width and int(probe.get("width") or 0) != expect_width:
        problems.append(f"width {probe.get('width')} != {expect_width}")
    if expect_height and int(probe.get("height") or 0) != expect_height:
        problems.append(f"height {probe.get('height')} != {expect_height}")
    duration = float(probe.get("stream_max_duration_seconds") or probe.get("duration_seconds") or 0)
    if expect_duration is not None:
        if abs(duration - expect_duration) > duration_tolerance:
            problems.append(f"duration {duration:.2f}s != {expect_duration:.2f}s ±{duration_tolerance}")
    moov_ok = _moov_ok(path, probe)
    decode_errors = count_decode_errors(path)
    md5 = file_md5(path)
    if not moov_ok:
        problems.append("moov atom missing or container unfinalized")
    if decode_errors > 0:
        problems.append(f"decode_errors {decode_errors}")
    return {
        "ok": not problems,
        "path": probe.get("path"),
        "probe": probe,
        "problems": problems,
        "moov_ok": moov_ok,
        "decode_errors": decode_errors,
        "md5": md5,
        "duration_seconds": duration,
    }


def write_sidecar(output_path: str, edit_meta: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = {k: (edit_meta or {}).get(k) for k in SIDECAR_KEYS}
    # Accept a few aliases from callers.
    if meta["length"] is None:
        meta["length"] = (edit_meta or {}).get("length_seconds")
    if meta["music"] is None:
        meta["music"] = bool((edit_meta or {}).get("music_on") if edit_meta and "music_on" in edit_meta else (edit_meta or {}).get("music"))
    payload = {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "edit": meta,
    }
    if extra:
        payload["extra"] = extra
    dest = sidecar_path(output_path)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    payload["sidecar_path"] = dest
    return payload
