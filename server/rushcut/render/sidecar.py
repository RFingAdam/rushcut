"""Verify a render with ffprobe and write the edit-metadata sidecar JSON."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from ..analysis.probe import probe_media
from ..output_policy import check_output_path

SIDECAR_KEYS = (
    "length",
    "hook_start_time",
    "caption_style",
    "zoom_count",
    "music",
    "template",
)


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


def verify_output(
    path: str,
    *,
    expect_width: int | None = None,
    expect_height: int | None = None,
    expect_audio: bool = True,
    duration_tolerance: float = 0.6,
    expect_duration: float | None = None,
    decode: bool = True,
) -> dict[str, Any]:
    """ffprobe + optional full-decode QA.

    Always returns ``moov_ok``, ``decode_errors`` and ``md5``. ``ok`` is
    false when the moov atom is missing or the decode reports errors, even
    if the other expectation checks pass.
    """
    from .atomic import count_decode_errors, file_md5, has_moov_atom

    probe = probe_media(path)
    md5 = file_md5(path)
    moov_ok = bool(probe.get("ok")) and has_moov_atom(path)
    if probe.get("unfinalized_container") or probe.get("header_duration_missing"):
        moov_ok = False
    decode_errors = 0
    if decode and os.path.isfile(path):
        decode_errors = count_decode_errors(path)
    elif not os.path.isfile(path):
        decode_errors = 1
    problems: list[str] = []
    if not probe.get("ok"):
        if probe.get("error"):
            problems.append(str(probe.get("error")))
        result = dict(probe)
        result["ok"] = False
        result["moov_ok"] = moov_ok
        result["decode_errors"] = decode_errors
        result["md5"] = md5
        result["problems"] = problems
        result["duration_seconds"] = float(
            probe.get("stream_max_duration_seconds") or probe.get("duration_seconds") or 0
        )
        return result
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
    if not moov_ok:
        problems.append("missing moov atom")
    if decode_errors > 0:
        problems.append(f"{decode_errors} decode error(s)")
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
    refused = check_output_path(output_path)
    if refused:
        return refused
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
